"""Readable downloads must validate the chosen directory, never old Hub blobs."""

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from routes.cookbook_output import (
    HF_CACHE_MATCHING_FILES_PROBE, HF_DIRECTORY_COMPLETE_PROBE,
    HF_DIRECTORY_INCOMPLETE_PROBE, HF_DOWNLOAD_SPACE_PROBE,
)
from src.hf_directory_download import (
    prepare_directory_download, finish_directory_download,
    directory_download_complete, directory_download_incomplete,
)
from src.hf_download_space import check_download_space
from src.model_library import write_model_metadata, read_model_metadata


def test_directory_identity_keeps_migration_provenance(tmp_path):
    write_model_metadata(tmp_path, "org/model", {"weights.gguf": {
        "source_path": "/old/hub/blob", "revision": "a" * 40,
    }})
    prepare_directory_download("org/model", tmp_path, "*.gguf")
    (tmp_path / "weights.gguf").write_bytes(b"model")
    assert finish_directory_download("org/model", tmp_path, "*.gguf") == 0
    marker = read_model_metadata(tmp_path)
    assert marker["files"]["weights.gguf"]["source_path"] == "/old/hub/blob"
    assert directory_download_complete("org/model", tmp_path, "*.gguf")
    assert not directory_download_complete("org/model", tmp_path, "*Q8*")


def test_completion_requires_success_of_current_attempt(tmp_path):
    (tmp_path / "model.gguf").write_bytes(b"weights")
    assert not directory_download_complete("org/model", tmp_path, "*.gguf")
    prepare_directory_download("org/model", tmp_path, "*.gguf")
    assert directory_download_incomplete("org/model", tmp_path, "*.gguf")
    assert finish_directory_download("org/model", tmp_path, "*.gguf") == 0
    assert directory_download_complete("org/model", tmp_path, "*.gguf")
    prepare_directory_download("org/model", tmp_path, "*.gguf")
    assert not directory_download_complete("org/model", tmp_path, "*.gguf")


@pytest.mark.parametrize("expected", [{}, {"new-model.gguf": 7}, {"model.gguf": 100}])
def test_postflight_rejects_empty_selection_stale_files_and_wrong_sizes(tmp_path, expected):
    (tmp_path / "model.gguf").write_bytes(b"weights")
    prepare_directory_download("org/model", tmp_path, "*.gguf", expected)
    assert finish_directory_download("org/model", tmp_path, "*.gguf") == 1
    assert not directory_download_complete("org/model", tmp_path, "*.gguf")


def test_receipt_detects_deleted_or_truncated_files(tmp_path):
    file = tmp_path / "weights.gguf"
    file.write_bytes(b"complete model")
    prepare_directory_download("org/model", tmp_path, "*.gguf", {file.name: file.stat().st_size})
    assert finish_directory_download("org/model", tmp_path, "*.gguf") == 0
    file.write_bytes(b"short")
    assert not directory_download_complete("org/model", tmp_path, "*.gguf")
    file.unlink()
    assert not directory_download_complete("org/model", tmp_path, "*.gguf")


def test_selected_empty_upstream_files_are_valid(tmp_path):
    prepare_directory_download("org/model", tmp_path, "", {"__init__.py": 0, "model.gguf": 5})
    (tmp_path / "__init__.py").touch()
    (tmp_path / "model.gguf").write_bytes(b"model")
    assert finish_directory_download("org/model", tmp_path, "") == 0


def test_hidden_upstream_folders_and_lockfiles_are_verified_without_private_metadata(tmp_path):
    upstream = {".github/workflows/test.yml": b"workflow", ".hidden/config.json": b"{}",
                ".cache/upstream/data.bin": b"model", "uv.lock": b"dependencies"}
    prepare_directory_download("org/model", tmp_path, "", {name: len(data) for name, data in upstream.items()})
    for name, data in {**upstream, ".cache/huggingface/download/model.metadata": b"metadata"}.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    assert finish_directory_download("org/model", tmp_path, "") == 0
    from src.hf_directory_download import directory_download_files
    assert directory_download_files(tmp_path) == {name: len(data) for name, data in upstream.items()}
    assert directory_download_complete("org/model", tmp_path, "")


def test_runner_rejects_lost_mount_before_hf_metadata_or_directory_creation(tmp_path):
    destination = tmp_path / "models/org/model"
    # The preflight must fail before importing HF or creating local-dir metadata.
    (tmp_path / "huggingface_hub.py").write_text("raise AssertionError('HF must not run before the mount check')\n")
    result = subprocess.run([sys.executable, "-c", HF_DOWNLOAD_SPACE_PROBE, "org/model", ""],
        env={**os.environ, "PYTHONPATH": str(tmp_path), "ODYSSEUS_REQUIRED_DOWNLOAD_MOUNT": str(tmp_path),
             "ODYSSEUS_HF_LOCAL_DIR": str(destination)}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 72
    assert "DOWNLOAD_MOUNT_UNAVAILABLE:" in result.stdout
    assert "HF must not run" not in result.stderr
    assert not destination.exists()


def test_readable_space_preflight_uses_destination_instead_of_global_cache(tmp_path, monkeypatch):
    seen = {}
    def snapshot_download(*, repo_id, local_dir, allow_patterns, ignore_patterns, dry_run):
        seen.update(local_dir=local_dir, allow_patterns=allow_patterns, ignore_patterns=ignore_patterns)
        return [SimpleNamespace(will_download=True, file_size=200_000_000)]
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    from src import hf_download_space
    monkeypatch.setattr(hf_download_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=100_000_000))
    target = tmp_path / "library/org/model"
    assert check_download_space("org/model", "*.gguf", "/unrelated/hub", local_dir=str(target)) == 28
    assert seen == {"local_dir": str(target), "allow_patterns": ["*.gguf"], "ignore_patterns": [".odysseus-model*"]}
    assert not target.exists()


def test_hub_cached_file_still_requires_space_for_copy_to_library(tmp_path, monkeypatch):
    def snapshot_download(*, repo_id, local_dir, allow_patterns, ignore_patterns, dry_run):
        return [SimpleNamespace(will_download=False, file_size=1_000_000_000, filename="model.gguf")]
    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(snapshot_download=snapshot_download))
    from src import hf_download_space
    monkeypatch.setattr(hf_download_space.shutil, "disk_usage", lambda _: SimpleNamespace(free=100_000_000))
    assert check_download_space("org/model", "*.gguf", "/already-cached/hub", local_dir=str(tmp_path)) == 28
    with (tmp_path / "model.gguf").open("wb") as stream:
        stream.truncate(1_000_000_000)
    assert check_download_space("org/model", "*.gguf", "/already-cached/hub", local_dir=str(tmp_path)) == 0


def test_portable_probes_ignore_old_cached_copy_and_metadata(tmp_path):
    hub = tmp_path / "hub"
    old = hub / "models--org--model/snapshots/old"
    old.mkdir(parents=True)
    (old / "model.gguf").write_bytes(b"old cached weights")
    target = tmp_path / "models/org/model"
    prepare_directory_download("org/model", target, "*.gguf")
    env = {**os.environ, "HF_HUB_CACHE": str(hub), "ODYSSEUS_HF_LOCAL_DIR": str(target)}
    def run(probe, *args):
        return subprocess.run([sys.executable, "-c", probe, "org/model", *args],
                              env=env, capture_output=True, text=True, timeout=10)
    assert run(HF_CACHE_MATCHING_FILES_PROBE, "*.gguf").returncode == 1
    assert run(HF_DIRECTORY_INCOMPLETE_PROBE, str(target), "*.gguf").returncode == 0
    assert run(HF_DIRECTORY_COMPLETE_PROBE, str(target), "*.gguf").returncode == 1
    (target / "model.gguf").write_bytes(b"new named weights")
    assert run(HF_CACHE_MATCHING_FILES_PROBE, "*.gguf").returncode == 0
    assert run(HF_DIRECTORY_COMPLETE_PROBE, str(target), "*.gguf").returncode == 0


def test_preflight_and_postflight_preserve_exact_upstream_selection(tmp_path):
    stub = tmp_path / "huggingface_hub.py"
    stub.write_text("from types import SimpleNamespace\n"
        "def snapshot_download(*, repo_id, local_dir, allow_patterns, ignore_patterns, dry_run):\n"
        " return [SimpleNamespace(will_download=True,file_size=5,filename='nested/model.gguf')]\n")
    target = tmp_path / "library/org/model"
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "ODYSSEUS_HF_LOCAL_DIR": str(target)}
    def run(probe):
        return subprocess.run([sys.executable, "-c", probe, "org/model", "*.gguf"],
                              env=env, capture_output=True, text=True, timeout=10)
    assert run(HF_DOWNLOAD_SPACE_PROBE).returncode == 0
    (target / "old.gguf").write_bytes(b"old")
    assert run(HF_CACHE_MATCHING_FILES_PROBE).returncode == 1
    (target / "nested").mkdir()
    (target / "nested/model.gguf").write_bytes(b"model")
    assert run(HF_CACHE_MATCHING_FILES_PROBE).returncode == 0
