"""Private-cache paths agree across downloads, scans, metadata, and probes."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import routes.cookbook_routes as routes
from routes.cookbook_helpers import ModelDownloadRequest, _cached_model_scan_script
from routes.cookbook_output import HF_CACHE_COMPLETE_PROBE, HF_CACHE_INCOMPLETE_PROBE
from src.hf_cache import download_cache_paths, effective_hf_cache


def test_cache_env_precedence_and_xdg_default(tmp_path):
    env = {"HF_HUB_CACHE": str(tmp_path / "direct"),
           "HUGGINGFACE_HUB_CACHE": str(tmp_path / "old-direct"),
           "HF_HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "xdg")}
    for key, expected in (("HF_HUB_CACHE", "direct"), ("HUGGINGFACE_HUB_CACHE", "old-direct"),
                          ("HF_HOME", "home/hub"), ("XDG_CACHE_HOME", "xdg/huggingface/hub")):
        assert effective_hf_cache(env) == str(tmp_path / expected)
        env.pop(key)


@pytest.mark.parametrize("selected,home,hub", [
    ("/private/cache/huggingface/hub", "/private/cache/huggingface", "/private/cache/huggingface/hub"),
    ("/private/cache/huggingface", "/private/cache/huggingface", "/private/cache/huggingface/hub"),
    ("~/models/hub/", "~/models", "~/models/hub"),
    (r"E:\AI Models\hub", "E:/AI Models", "E:/AI Models/hub"),
    ("/", "/", "/hub"),
])
def test_download_accepts_hf_home_or_exact_hub(selected, home, hub):
    assert download_cache_paths(selected) == (home, hub)


@pytest.mark.asyncio
@pytest.mark.parametrize("remote,platform,selected", [
    (None, "", None), (None, "", "/custom/models/hub"), (None, "", "/custom/models"),
    ("tester@gpu-box", "", "~/models/hub"),
    ("tester@gpu-box", "windows", r"E:\AI Models\hub"),
])
async def test_real_download_runner_pins_exact_cache(tmp_path, monkeypatch, remote, platform, selected):
    process = AsyncMock()
    process.returncode = 1
    process.stderr.read.return_value = b"deliberately stopped before launching"
    monkeypatch.setattr(routes, "require_admin", lambda _: None)
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    monkeypatch.setattr(routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(routes, "COOKBOOK_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(routes, "load_stored_hf_token", lambda **_: "")
    monkeypatch.setattr(routes, "_binary_available", AsyncMock(return_value=True))
    monkeypatch.setattr(routes.asyncio, "create_subprocess_shell", AsyncMock(return_value=process))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "private-hf"))
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "private-hf/hub"))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", "/stale/global/hub")
    endpoint = next(r.endpoint for r in routes.setup_cookbook_routes().routes
                    if r.path == "/api/model/download" and "POST" in r.methods)
    await endpoint(Request({"type": "http", "method": "POST", "path": "/api/model/download", "headers": []}),
                   ModelDownloadRequest(repo_id="example/model", remote_host=remote,
                                        platform=platform, local_dir=selected, layout="cache"))
    script = next(tmp_path.glob("*.ps1" if platform == "windows" else "*.sh")).read_text()
    home, hub = download_cache_paths(selected)
    if platform == "windows":
        assert f"$env:HF_HOME = '{home}'" in script
        assert f"$env:HF_HUB_CACHE = '{hub}'" in script
        assert f"$env:HUGGINGFACE_HUB_CACHE = '{hub}'" in script
    else:
        # Evaluate only cache exports against stale values to prove tmux's
        # inherited environment cannot send the download to a global cache.
        exports = "\n".join(line for line in script.splitlines()
                            if line.startswith(("export HF_HOME=", "export HF_HUB_CACHE=", "export HUGGINGFACE_HUB_CACHE=")))
        env = dict(os.environ, HF_HOME="/stale/home", HF_HUB_CACHE="/stale/hub", HUGGINGFACE_HUB_CACHE="/stale/hub")
        command = exports + "\n" + shlex.quote(sys.executable) + " -c 'import os,json; print(json.dumps([os.environ[k] for k in [\"HF_HOME\",\"HF_HUB_CACHE\",\"HUGGINGFACE_HUB_CACHE\"]]))'"
        result = subprocess.run(["bash", "-c", command], env=env, capture_output=True, text=True, check=True)
        assert json.loads(result.stdout) == [os.path.expanduser(home), os.path.expanduser(hub), os.path.expanduser(hub)]
    assert "/hub/hub" not in script


@pytest.mark.asyncio
async def test_client_cache_metadata_comes_from_server_not_saved_state(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"env": {"localHfCacheDir": "/stale/client/hub"}}))
    monkeypatch.setattr(routes, "COOKBOOK_STATE_FILE", str(state_path))
    monkeypatch.setattr(routes, "require_admin", lambda _: None)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "actual/hub"))
    endpoint = next(r.endpoint for r in routes.setup_cookbook_routes().routes
                    if r.path == "/api/cookbook/state" and "GET" in r.methods)
    result = await endpoint(Request({"type": "http", "method": "GET", "path": "/api/cookbook/state", "headers": []}))
    assert result["env"]["localHfCacheDir"] == str(tmp_path / "actual/hub")
    assert result["env"]["localLegacyHfCacheDir"] == str(Path.home() / ".cache/huggingface/hub")


@pytest.mark.asyncio
async def test_stale_client_cannot_restore_implicit_global_cache(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    private = str(tmp_path / "private/hub")
    legacy = str(Path.home() / ".cache/huggingface/hub")
    alias = "~/.cache/huggingface/hub"
    local = {"host": "", "modelDir": alias, "downloadDir": alias,
             "modelDirs": [alias, private, "/mnt/e/models", legacy]}
    remote = {"host": "tester@gpu-box", "modelDirs": [alias], "downloadDir": alias}
    body = {"env": {"servers": [local, remote], "localHfCacheDir": "/stale/client/hub"}}
    state_path.write_text(json.dumps(body))
    monkeypatch.setattr(routes, "COOKBOOK_STATE_FILE", str(state_path))
    monkeypatch.setattr(routes, "require_admin", lambda _: None)
    monkeypatch.setenv("HF_HUB_CACHE", private)

    def endpoint(method):
        return next(r.endpoint for r in routes.setup_cookbook_routes().routes
                    if r.path == "/api/cookbook/state" and method in r.methods)

    request = Request({"type": "http", "method": "GET", "path": "/api/cookbook/state", "headers": []})
    client = await endpoint("GET")(request)
    expected = {"host": "", "modelDir": private, "downloadDir": private,
                "modelDirs": [private, "/mnt/e/models", legacy]}
    assert client["env"]["servers"] == [expected, remote]
    # Simulate an old tab POSTing the original state after the corrected GET.
    request = Request({"type": "http", "method": "POST", "path": "/api/cookbook/state", "headers": []},
                      receive=AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()}))
    assert (await endpoint("POST")(request))["ok"]
    stored = json.loads(state_path.read_text())
    assert stored["env"]["servers"] == [expected, remote]
    assert "localHfCacheDir" not in stored["env"]


def _model(cache, name):
    snapshot = cache / f"models--test--{name}" / "snapshots/revision"
    snapshot.mkdir(parents=True)
    (snapshot / "weights.gguf").write_bytes(b"model")


def _scan(monkeypatch, capsys, directories):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    def offline(*args, **kwargs):
        raise OSError("No Ollama service")
    monkeypatch.setattr(urllib.request, "urlopen", offline)
    exec(compile(_cached_model_scan_script(directories), "<cache-scan>", "exec"), {})
    return json.loads(capsys.readouterr().out)


def test_scan_uses_effective_cache_without_adding_global_home(tmp_path, monkeypatch, capsys):
    private = tmp_path / "private/hub"
    legacy = tmp_path / ".cache/huggingface/hub"
    stale = tmp_path / "old/hub"
    for cache, name in ((private, "private"), (legacy, "global"), (stale, "stale")):
        _model(cache, name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HF_HUB_CACHE", str(private))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(stale))
    monkeypatch.setenv("HF_HOME", str(legacy.parent))
    assert [row["repo_id"] for row in _scan(monkeypatch, capsys, [])] == ["test/private"]
    assert {row["repo_id"] for row in _scan(monkeypatch, capsys, [str(legacy)])} == {"test/private", "test/global"}


def test_nested_hub_is_a_container_not_one_giant_model(tmp_path, monkeypatch, capsys):
    cache = tmp_path / "huggingface/hub"
    _model(cache, "outer")
    _model(cache / "hub", "nested")
    for wrapper in ("xet", "blobs"):
        path = cache / wrapper / "chunks"
        path.mkdir(parents=True)
        (path / "weights.safetensors").write_bytes(b"cache-chunk")
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    models = _scan(monkeypatch, capsys, [str(cache), str(cache.parent)])
    assert {row["repo_id"] for row in models} == {"test/outer", "test/nested"}
    assert len(models) == 2
    assert next(row for row in models if row["repo_id"] == "test/nested")["path"] == str(cache / "hub")


@pytest.mark.parametrize("probe,incomplete", [(HF_CACHE_COMPLETE_PROBE, False), (HF_CACHE_INCOMPLETE_PROBE, True)])
def test_completion_probes_accept_exact_hub_and_modern_env(tmp_path, probe, incomplete):
    cache = tmp_path / "private/hub"
    _model(cache, "probe")
    if incomplete:
        blobs = cache / "models--test--probe/blobs"
        blobs.mkdir()
        (blobs / "weights.incomplete").write_bytes(b"partial")
    env = dict(os.environ, HF_HUB_CACHE=str(cache), HUGGINGFACE_HUB_CACHE="/stale/hub", HF_HOME="/stale/home")
    for selected in (str(cache), str(cache.parent), ""):
        result = subprocess.run([sys.executable, "-c", probe, "test/probe", selected], env=env, capture_output=True)
        assert result.returncode == 0, result.stderr
