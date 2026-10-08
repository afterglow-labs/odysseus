"""Readable HF repositories stay distinct and launch from their real folders."""

import json
import os
from pathlib import Path
import subprocess
import sys

from routes.cookbook_helpers import _cached_model_scan_script


def named(root, repo, files, *, revision="rev1", source_paths=None):
    directory = root / repo
    directory.mkdir(parents=True, exist_ok=True)
    metadata = {}
    for relative, data in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        metadata[relative] = {"revision": revision, "size": len(data)}
        if source_paths and relative in source_paths:
            metadata[relative]["source_path"] = str(source_paths[relative])
    (directory / ".odysseus-model.json").write_text(json.dumps({
        "format": "odysseus-model", "version": 1, "repository": repo, "files": metadata,
    }))
    return directory


def scan(tmp_path, *roots, default=None):
    script = _cached_model_scan_script([str(root) for root in roots])
    # Exercise the standalone remote program without touching live caches or
    # starting Ollama; the inputs are only this test's temporary directories.
    script = script.replace("scan_ollama_api()\nscan_ollama()", "")
    env = {**os.environ, "HF_HUB_CACHE": str(default or tmp_path / "empty-cache")}
    process = subprocess.run([sys.executable, "-I", "-"], input=script,
                             text=True, capture_output=True, check=True, cwd=tmp_path, env=env)
    return json.loads(process.stdout)


def test_named_repos_preserve_owner_boundaries_and_internal_gguf_paths(tmp_path):
    root = tmp_path / "models"
    first = named(root, "author/vision", {
        "Q4/model-00001-of-00002.gguf": b"one",
        "Q4/model-00002-of-00002.gguf": b"two",
        "vision/mmproj-F16.gguf": b"projector",
    })
    second = named(root, "author/text", {"config.json": b"{}", "model.safetensors": b"weights"})
    rows = scan(tmp_path, root, first)
    assert {row["repo_id"] for row in rows} == {"author/vision", "author/text"}
    assert len(rows) == 2, "Overlapping roots must not duplicate a marked repo"
    by_repo = {row["repo_id"]: row for row in rows}
    assert by_repo["author/text"]["model_path"] == str(second)
    vision = by_repo["author/vision"]
    assert vision["model_path"] == str(first)
    assert vision["path"] == str(root)
    assert vision["is_local_dir"] and vision["named_layout"]
    assert [(file["rel_path"], file["role"]) for file in vision["gguf_files"]] == [
        ("Q4/model-00001-of-00002.gguf", "model"), ("vision/mmproj-F16.gguf", "projector")]
    assert vision["gguf_files"][0]["parts"] == 2
    assert vision["gguf_files"][0]["size_bytes"] == 6


def test_named_diffusers_and_partial_downloads_under_default_root(tmp_path):
    root = tmp_path / "hub"
    repo = named(root, "author/diffusion", {
        "model_index.json": b'{"_class_name":"FluxPipeline"}',
        "transformer/diffusion_pytorch_model.safetensors": b"weights",
    })
    partial = repo / ".cache/huggingface/download/other.abcdef.incomplete"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    row, = scan(tmp_path, default=root)
    assert row["repo_id"] == "author/diffusion"
    assert row["is_diffusion"]
    assert row["has_incomplete"]
    assert row["nb_files"] == 2, "HF download metadata is not model data"


def test_pending_download_receipt_is_visible_before_hf_creates_partial_files(tmp_path):
    root = tmp_path / "models"
    repo = named(root, "author/model", {})
    receipt = repo / ".cache/odysseus/downloads/pattern.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"repository": "author/model", "status": "pending"}))
    row, = scan(tmp_path, root)
    assert row["has_incomplete"] and row["nb_files"] == 0


def test_named_library_keeps_separate_local_folders_with_same_leaf(tmp_path):
    roots = [tmp_path / "disk1", tmp_path / "disk2"]
    for root in roots:
        (root / "plain").mkdir(parents=True)
        (root / "plain/model.gguf").write_bytes(b"gguf")
    rows = scan(tmp_path, *roots)
    assert len(rows) == 2
    assert {row["model_path"] for row in rows} == {str(root / "plain") for root in roots}


def test_named_copy_hides_only_fully_covered_legacy_revisions(tmp_path):
    root, legacy = tmp_path / "models", tmp_path / "legacy"
    snapshot = legacy / "models--author--vision/snapshots/rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "model-Q4.gguf").write_bytes(b"four")
    named(root, "author/vision", {"model-Q4.gguf": b"four"})
    rows = scan(tmp_path, legacy, root)
    assert len(rows) == 1 and rows[0]["named_layout"]
    (snapshot / "model-Q8.gguf").write_bytes(b"eight")
    rows = scan(tmp_path, legacy, root)
    assert len(rows) == 2, "An uncopied quant must stay accessible"
    (snapshot / "model-Q8.gguf").unlink()
    other = snapshot.parent / "rev2"
    other.mkdir()
    (other / "model-Q4.gguf").write_bytes(b"new!")
    assert len(scan(tmp_path, legacy, root)) == 2, "Same filename and size are not enough to discard another revision"


def test_readme_only_legacy_blobs_are_not_models_but_partial_weights_stay_visible(tmp_path):
    root, legacy = tmp_path / "models", tmp_path / "legacy"
    named(root, "author/vision", {"model.safetensors": b"weights"})
    repo = legacy / "models--author--vision"
    blobs = repo / "blobs"
    blobs.mkdir(parents=True)
    readme = blobs / ("a" * 40)
    readme.write_bytes(b"model card")
    snapshot = repo / "snapshots/rev1"
    snapshot.mkdir(parents=True)
    (snapshot / "README.md").symlink_to(readme)
    (snapshot / ".gitattributes").write_text("attributes")
    (snapshot / "config.json").write_text("{}")
    rows = scan(tmp_path, legacy, root)
    assert len(rows) == 1 and rows[0]["named_layout"], "A metadata-only legacy cache is not another ready model"
    partial = blobs / ("b" * 64 + ".incomplete")
    partial.touch()
    rows = scan(tmp_path, legacy, root)
    assert len(rows) == 2
    pending = next(row for row in rows if not row.get("named_layout"))
    assert pending["has_incomplete"], "Keep a zero-byte paused transfer visible"
    partial.unlink()
    (snapshot / "uncopied.gguf").write_bytes(b"another quant")
    rows = scan(tmp_path, legacy, root)
    assert len(rows) == 2, "The original remains visible while unique weights await migration"


def test_split_named_repository_keeps_both_locations_and_all_legacy_variants(tmp_path):
    linux, external = tmp_path / "linux", tmp_path / "external"
    named(linux, "author/workflows", {"h3/model.safetensors": b"h3"})
    named(external, "author/workflows", {"ltx/model.safetensors": b"ltx"})
    rows = scan(tmp_path, linux, external)
    assert len(rows) == 2
    assert {row["path"] for row in rows} == {str(linux), str(external)}


def test_invalid_marker_does_not_supply_repository_or_paths(tmp_path):
    root = tmp_path / "models"
    repo = named(root, "author/model", {"model.gguf": b"gguf"})
    (repo / ".odysseus-model.json").write_text(json.dumps({
        "format": "odysseus-model", "version": 1, "repository": "../../elsewhere"}))
    rows = scan(tmp_path, root)
    assert all(not row.get("named_layout") for row in rows)
    assert all(".." not in row["repo_id"] for row in rows)
