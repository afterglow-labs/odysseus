"""Execute the displayed download command against a local, non-network Hub stub."""

import fnmatch
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def preview_command(*, download_dir="", local_cache="/private/cache/huggingface/hub",
                    local_download="", servers=None, remote=False, model=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the download preview regression")
    source = (ROOT / "static/js/cookbookDownload.js").read_text()
    functions = source.split("// ── Command builder: download ──", 1)[1].split(
        "// ── Panel rendering helpers ──", 1
    )[0].replace("export function", "function")
    state = {"remoteHost": "remote.test" if remote else "", "localHfCacheDir": local_cache,
             "localDownloadDir": local_download, "servers": servers or []}
    model = model or {"name": "author/model"}
    backend = "llamacpp" if model.get("gguf_sources") else "diffusers"
    script = (
        f"const _envState = {json.dumps(state)};\n"
        f"const _serverByVal = () => ({json.dumps({'downloadDir': download_dir})});\n"
        "const _isWindows = () => false; const _buildEnvPrefix = () => '';\n"
        "const _sshCmd = (host, command) => command; const _getPort = () => '';\n"
        + functions
        + f"\nconsole.log(JSON.stringify(_buildDownloadCmd({json.dumps(model)}, {json.dumps(backend)})));"
    )
    result = subprocess.run([node, "-e", script], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def run_preview(command, tmp_path):
    (tmp_path / "tqdm").mkdir()
    (tmp_path / "tqdm/__init__.py").write_text("")
    (tmp_path / "tqdm/auto.py").write_text("")
    (tmp_path / "huggingface_hub").mkdir()
    (tmp_path / "huggingface_hub/utils.py").write_text("")
    (tmp_path / "huggingface_hub/__init__.py").write_text(
        "import json\n"
        "def snapshot_download(repo_id, **kwargs):\n"
        " print('CAPTURE ' + json.dumps({'repo_id': repo_id, **kwargs}))\n"
        " return '/stub/download'\n"
    )
    env = {**os.environ, "PYTHONPATH": str(tmp_path), "BFS_PREVIEW_EXPANSION": "WRONG"}
    result = subprocess.run(["bash", "-c", command], cwd=tmp_path, env=env, check=True,
                            text=True, capture_output=True)
    return json.loads(next(line.removeprefix("CAPTURE ") for line in result.stdout.splitlines()
                           if line.startswith("CAPTURE ")))


@pytest.mark.parametrize("chosen,expected", [
    ("/private/cache/huggingface/hub", "/private/cache/huggingface/hub"),
    ("/private/cache/huggingface/hub/", "/private/cache/huggingface/hub"),
    ("/private/cache/huggingface", "/private/cache/huggingface/hub"),
    ("/", "/hub"),
    ("", "/private/cache/huggingface/hub"),
])
def test_preview_uses_hub_cache_not_repo_export(tmp_path, chosen, expected):
    captured = run_preview(preview_command(download_dir=chosen), tmp_path)
    assert captured == {"repo_id": "author/model", "cache_dir": expected}


def test_preview_does_not_expand_shell_characters_in_paths_or_patterns(tmp_path):
    dangerous = "cache owner's $BFS_PREVIEW_EXPANSION $(touch injected) `touch injected2`/hub"
    pattern = "weights owner's $BFS_PREVIEW_EXPANSION $(touch injected3).gguf"
    model = {"name": "author/model", "gguf_sources": [{"repo": "author/model", "file": pattern}]}
    captured = run_preview(preview_command(download_dir=dangerous, model=model), tmp_path)
    assert captured == {"repo_id": "author/model", "cache_dir": dangerous, "allow_patterns": [pattern]}
    assert not any((tmp_path / name).exists() for name in ["injected", "injected2", "injected3"])


def test_remote_preview_uses_remote_target_and_never_local_default(tmp_path):
    captured = run_preview(preview_command(remote=True), tmp_path)
    assert captured == {"repo_id": "author/model"}


def test_remote_preview_expands_remote_home_at_execution(tmp_path):
    captured = run_preview(preview_command(remote=True, download_dir="~/remote models"), tmp_path)
    assert captured == {"repo_id": "author/model", "cache_dir": os.path.expanduser("~/remote models/hub")}


@pytest.mark.parametrize("servers,local_download,download_dir,expected", [
    ([], "/mnt/e/AI/huggingface/hub", "", "/mnt/e/AI/huggingface/hub"),
    ([{"host": "", "downloadDir": "/mnt/e/AI/huggingface/hub"}], "", "",
     "/mnt/e/AI/huggingface/hub"),
    ([], "/mnt/e/AI/huggingface/hub", "/explicit/hub", "/explicit/hub"),
])
def test_preview_uses_chosen_download_drive_not_scan_root(
        tmp_path, servers, local_download, download_dir, expected):
    captured = run_preview(preview_command(
        servers=servers, local_download=local_download, download_dir=download_dir), tmp_path)
    assert captured["cache_dir"] == expected


def test_remote_preview_never_inherits_saved_local_drive(tmp_path):
    captured = run_preview(preview_command(
        remote=True, local_download="/mnt/e/AI/huggingface/hub",
        servers=[{"host": "", "downloadDir": "/mnt/e/AI/huggingface/hub"}]), tmp_path)
    assert "cache_dir" not in captured


def test_gemma_qat_recipe_downloads_real_gguf_instead_of_display_quantization(tmp_path):
    """QAT-INT4 is a recipe label, not text present in Google's filename."""
    catalog = json.loads((ROOT / "services/hwfit/data/hf_models.json").read_text())
    recipe = next(model for model in catalog
                  if model["name"] == "google/gemma-4-26B-A4B-it-qat-q4_0-gguf")
    # The hardware-fit result exposes the catalog's quantization as `quant`.
    model = {**recipe, "quant": recipe["quantization"]}
    captured = run_preview(preview_command(model=model), tmp_path)
    assert captured["repo_id"] == recipe["name"]
    assert captured["allow_patterns"] == ["gemma-4-26B*.gguf"]
    # Actual Google repository manifest: both files are needed for vision.
    filenames = [".gitattributes", "README.md", "gemma-4-26B-it-mmproj.gguf",
                 "gemma-4-26B_q4_0-it.gguf"]
    selected = [name for name in filenames if any(
        fnmatch.fnmatchcase(name, pattern) for pattern in captured["allow_patterns"])]
    assert selected == ["gemma-4-26B-it-mmproj.gguf", "gemma-4-26B_q4_0-it.gguf"]
    # The serve preview must still get an exact main model, never the glob or projector.
    assert recipe["gguf_sources"][0]["file"] == "gemma-4-26B_q4_0-it.gguf"
