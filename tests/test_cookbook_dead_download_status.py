"""Behavioral guards for dead-session download classification (issue #4017).

A download whose tmux pane is gone must not be reported as stopped when its
retained output carries DOWNLOAD_OK, or when the files landed in a custom
download dir. The runner exports HF_HOME=<local_dir>, so the cache lives
under <local_dir>/hub — the probe only finds it if the task's dir is passed
in explicitly rather than read from the probe process's environment.
"""
import os
import json
import subprocess
import sys
from pathlib import Path

import pytest

from routes.cookbook_output import (
    classify_dead_download,
    HF_CACHE_COMPLETE_PROBE,
    HF_CACHE_INCOMPLETE_PROBE,
    HF_CACHE_MATCHING_FILES_PROBE,
)

REPO = "org/some-model-GGUF"


# ── Marker classification ──


def test_download_ok_resolves_completed():
    snap = "Fetching 4 files: 100%|####| 4/4\nDownload complete\n\nDOWNLOAD_OK\n$"
    assert classify_dead_download(snap) == ("completed", False)


def test_download_failed_resolves_error():
    snap = "some progress\n\nDOWNLOAD_FAILED (exit 1 after 3 attempts)"
    assert classify_dead_download(snap) == ("error", False)


def test_download_ok_with_zero_files_resolves_error():
    # A DOWNLOAD_OK from a run that matched no files (bad include/quant
    # pattern) is still a failure — same guard as the live-session branch.
    snap = "Fetching 0 files: 0it [00:00, ?it/s]\n\nDOWNLOAD_OK"
    assert classify_dead_download(snap) == ("error", True)


def test_download_empty_marker_overrides_cli_success():
    snap = "path=/cache/models--google--gemma/snapshots/rev\nDOWNLOAD_EMPTY: No matching files were downloaded.\nDOWNLOAD_FAILED (exit 1 after 1 attempts)"
    assert classify_dead_download(snap) == ("error", True)


@pytest.mark.parametrize("pattern,filename,complete", [
    ("*QAT-INT4*", "gemma-4-26B_q4_0-it.gguf", False),
    ("*q4_0*", "gemma-4-26B_q4_0-it.gguf", True),
    ("*mmproj*.gguf", "gemma-4-26B-it-mmproj.gguf", True),
    ("*.gguf", "weights.gguf.incomplete", False),
    ("", "model.gguf", True),
])
def test_matching_files_probe_accepts_only_materialized_matches(tmp_path, pattern, filename, complete):
    hub = tmp_path / "hub"
    repo = hub / "models--org--some-model-GGUF"
    snap = repo / "snapshots" / "revision"
    snap.mkdir(parents=True)
    (snap / filename).write_bytes(b"model data")
    result = subprocess.run(
        [sys.executable, "-c", HF_CACHE_MATCHING_FILES_PROBE, REPO, pattern],
        env={**os.environ, "HF_HUB_CACHE": str(hub)}, capture_output=True, text=True,
        timeout=10,
    )
    assert (result.returncode == 0) is complete
    assert ("DOWNLOAD_EMPTY:" in result.stdout) is not complete


def test_matching_files_probe_rejects_metadata_only_repo_and_stale_snapshot(tmp_path):
    hub = tmp_path / "hub"
    repo = hub / "models--org--some-model-GGUF"
    old = repo / "snapshots" / "old"
    old.mkdir(parents=True)
    (old / "model.gguf").write_bytes(b"old cached quant")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("new-revision")
    (repo / "trees").mkdir()
    (repo / "trees" / "new-revision.json").write_text('{"format_version":1,"files":{}}')
    result = subprocess.run(
        [sys.executable, "-c", HF_CACHE_MATCHING_FILES_PROBE, REPO, "*.gguf"],
        env={**os.environ, "HF_HUB_CACHE": str(hub)}, capture_output=True, text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "No matching files were downloaded" in result.stdout


def test_installed_hf_cli_zero_match_success_is_rejected_offline(tmp_path):
    """Real new HF CLI prints a path and exits 0 even without any file match.

    Seed its tree cache with immutable revision metadata, so neither the CLI
    nor the validation can contact the network or download model weights.
    """
    hf = pytest.importorskip("huggingface_hub._snapshot_download")
    if not hasattr(hf, "read_tree_cache"):
        pytest.skip("This HF version uses the older cache without tree listings")
    cli = Path(sys.executable).with_name("hf")
    if not cli.exists():
        pytest.skip("HF CLI unavailable in this test environment")
    repo_id = "google/gemma-4-26B-A4B-it-qat-q4_0-gguf"
    revision = "d1c082be9cf3c8a514acf63b8761f4b41935842e"
    repo = tmp_path / ("models--" + repo_id.replace("/", "--"))
    tree = repo / "trees" / f"{revision}.json"
    tree.parent.mkdir(parents=True)
    tree.write_text(json.dumps({"format_version": 1, "files": {
        "gemma-4-26B_q4_0-it.gguf": {"size": 14439363584, "blob_id": "dd8728aa225128f4cfc9c21c0d41550ee5171bea"},
        "gemma-4-26B-it-mmproj.gguf": {"size": 1194828160, "blob_id": "29193ce4193b6a24c5f0fa7555905464a6cb46cc"},
    }}))
    env = {**os.environ, "HF_HUB_CACHE": str(tmp_path), "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1"}
    result = subprocess.run(
        [str(cli), "download", repo_id, "--revision", revision, "--include", "*QAT-INT4*"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert str(repo / "snapshots" / revision) in result.stdout
    assert not (repo / "snapshots" / revision).exists()
    checked = subprocess.run(
        [sys.executable, "-c", HF_CACHE_MATCHING_FILES_PROBE, repo_id, "*QAT-INT4*"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert checked.returncode == 1
    assert classify_dead_download(result.stdout + checked.stdout + "\nDOWNLOAD_FAILED (exit 1)") == ("error", True)


def test_no_marker_returns_none():
    # Mid-download tail with no terminal marker — caller must fall back to
    # the cache probe.
    assert classify_dead_download("Downloading model.gguf:  42%") is None
    assert classify_dead_download("") is None


def test_ollama_pull_output_resolves_completed():
    snap = "pulling manifest\npulling 8f39d1c3...: 100%\nsuccess\n\nDOWNLOAD_OK"
    assert classify_dead_download(snap) == ("completed", False)


# ── Cache probe scripts ──


def _make_cache(root, repo=REPO, incomplete=False, empty_snapshot=False):
    d = os.path.join(root, "hub", "models--" + repo.replace("/", "--"))
    snap = os.path.join(d, "snapshots", "abc123")
    os.makedirs(snap)
    if not empty_snapshot:
        with open(os.path.join(snap, "model.gguf"), "w") as f:
            f.write("x")
    if incomplete:
        blobs = os.path.join(d, "blobs")
        os.makedirs(blobs)
        with open(os.path.join(blobs, "deadbeef.incomplete"), "w") as f:
            f.write("x")


def _run_probe(probe, repo, cache_root, env=None):
    # Strip the HF cache vars so the probe can't accidentally find a real
    # cache on the machine running the tests.
    full_env = {k: v for k, v in os.environ.items()
                if k not in ("HF_HOME", "HUGGINGFACE_HUB_CACHE", "HF_HUB_CACHE")}
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-c", probe, repo, cache_root],
        env=full_env, capture_output=True, timeout=30,
    ).returncode


def test_complete_probe_finds_custom_dir_cache(tmp_path):
    # Model materialized under <local_dir>/hub — found only via the explicit
    # cache_root argument (issue #4017).
    root = str(tmp_path)
    _make_cache(root)
    assert _run_probe(HF_CACHE_COMPLETE_PROBE, REPO, root) == 0


def test_complete_probe_misses_without_cache_root(tmp_path):
    # Same on-disk layout, but without the cache_root argument the probe
    # falls back to the default cache and misses it.
    _make_cache(str(tmp_path))
    assert _run_probe(HF_CACHE_COMPLETE_PROBE, REPO, "") == 1


def test_complete_probe_rejects_incomplete_blobs(tmp_path):
    root = str(tmp_path)
    _make_cache(root, incomplete=True)
    assert _run_probe(HF_CACHE_COMPLETE_PROBE, REPO, root) == 1


def test_complete_probe_rejects_empty_snapshot(tmp_path):
    root = str(tmp_path)
    _make_cache(root, empty_snapshot=True)
    assert _run_probe(HF_CACHE_COMPLETE_PROBE, REPO, root) == 1


def test_complete_probe_env_fallback_still_works(tmp_path):
    # No custom dir on the task — the probe must keep honoring the standard
    # HF env vars so default-cache downloads classify as before.
    root = str(tmp_path)
    _make_cache(root)
    hub = os.path.join(root, "hub")
    assert _run_probe(HF_CACHE_COMPLETE_PROBE, REPO, "", env={"HUGGINGFACE_HUB_CACHE": hub}) == 0


def test_incomplete_probe_sees_custom_dir_partials(tmp_path):
    root = str(tmp_path)
    _make_cache(root, incomplete=True)
    assert _run_probe(HF_CACHE_INCOMPLETE_PROBE, REPO, root) == 0
    # Clean cache → no resumable partials.
    assert _run_probe(HF_CACHE_INCOMPLETE_PROBE, "org/other-model", root) == 1
