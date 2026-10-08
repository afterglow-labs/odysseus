"""A client's omitted destination uses the saved Local download choice."""

import json
import os
import shlex
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.cookbook_routes as routes
from routes.cookbook_helpers import load_stored_local_download_dir


@pytest.fixture
def download_client(tmp_path, monkeypatch):
    state = tmp_path / "cookbook_state.json"
    state.write_text(json.dumps({"env": {"servers": [
        {"name": "Remote", "host": "tester@gpu-box", "downloadDir": "/remote/hub"},
        {"name": "Local", "host": "", "downloadDir": "/mnt/e/AI/huggingface/hub",
         "modelDirs": ["/private/hub", "/mnt/e/AI/huggingface/hub"]},
    ]}}))
    process = AsyncMock()
    process.returncode = 0
    monkeypatch.setattr(routes, "require_admin", lambda _: None)
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    monkeypatch.setattr(routes, "is_wsl", lambda: False)
    monkeypatch.setattr(routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(routes, "COOKBOOK_STATE_FILE", str(state))
    monkeypatch.setattr(routes, "load_stored_hf_token", lambda **_: "")
    monkeypatch.setattr(routes, "_binary_available", AsyncMock(return_value=True))
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_shell", launch)
    monkeypatch.setenv("HF_HOME", "/private")
    monkeypatch.setenv("HF_HUB_CACHE", "/private/hub")
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", "/stale/global/hub")
    app = FastAPI()
    app.include_router(routes.setup_cookbook_routes())
    with TestClient(app) as client:
        yield client, state, launch


def _runner_exports(script):
    exports = "\n".join(line for line in script.splitlines() if line.startswith(
        ("export HF_HOME=", "export HF_HUB_CACHE=", "export HUGGINGFACE_HUB_CACHE=")
    ))
    # Evaluate only the generated environment, never the downloader or SSH.
    command = exports + "\n" + shlex.quote(sys.executable) + " -c 'import os,json; print(json.dumps([os.environ[k] for k in [\"HF_HOME\",\"HF_HUB_CACHE\",\"HUGGINGFACE_HUB_CACHE\"]]))'"
    result = subprocess.run(["bash", "-c", command], env=dict(os.environ),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.parametrize("fields,expected", [
    ({}, "/mnt/e/AI/huggingface/hub"),
    ({"local_dir": None}, "/mnt/e/AI/huggingface/hub"),
    ({"local_dir": ""}, "/mnt/e/AI/huggingface/hub"),
    ({"local_dir": "/explicit/models/hub"}, "/explicit/models/hub"),
    ({"local_dir": "/explicit/models"}, "/explicit/models/hub"),
])
def test_saved_local_default_reaches_runner_and_response(download_client, fields, expected):
    client, state, launch = download_client
    response = client.post("/api/model/download", json={"repo_id": "example/model", "layout": "cache", **fields})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["cache_dir"] == expected
    script = (state.parent / (response.json()["session_id"] + ".sh")).read_text()
    assert _runner_exports(script) == [expected.rsplit("/", 1)[0], expected, expected]
    launch.assert_awaited_once()


@pytest.mark.parametrize("platform", ["linux", "windows"])
def test_remote_download_does_not_inherit_saved_local_directory(download_client, platform):
    client, state, _ = download_client
    response = client.post("/api/model/download", json={
        "repo_id": "example/model", "layout": "cache", "remote_host": "tester@gpu-box", "platform": platform,
    })
    assert response.status_code == 200
    assert response.json()["cache_dir"] is None
    suffix = "_run.ps1" if platform == "windows" else "_run.sh"
    runner = (state.parent / (response.json()["session_id"] + suffix)).read_text()
    assert "/mnt/e/AI/huggingface/hub" not in runner
    assert "/private/hub" not in runner
    assert "export HF_HUB_CACHE=" not in runner
    assert "$env:HF_HUB_CACHE =" not in runner


def test_local_default_is_read_fresh_and_does_not_change_scan_default(download_client):
    client, state, _ = download_client
    saved = json.loads(state.read_text())
    saved["env"]["servers"][1]["downloadDir"] = "/mnt/f/New cache/hub"
    saved["env"]["localDownloadDir"] = "/stale/client/hub"
    state.write_text(json.dumps(saved))
    result = client.post("/api/model/download", json={"repo_id": "example/model"}).json()
    assert result["cache_dir"] is None
    assert result["download_dir"] == "/mnt/f/New cache/hub/example/model"
    metadata = client.get("/api/cookbook/state").json()["env"]
    assert metadata["localDownloadDir"] == "/mnt/f/New cache/hub"
    assert metadata["localHfCacheDir"] == "/private/hub"
    assert client.post("/api/cookbook/state", json={"env": metadata}).json()["ok"]
    assert "localDownloadDir" not in json.loads(state.read_text())["env"]


def test_invalid_saved_destination_fails_before_spawning(download_client):
    client, state, launch = download_client
    saved = json.loads(state.read_text())
    saved["env"]["servers"][1]["downloadDir"] = "/mnt/e/bad;touch pwned"
    state.write_text(json.dumps(saved))
    response = client.post("/api/model/download", json={"repo_id": "example/model"})
    assert response.status_code == 400
    launch.assert_not_called()


def test_missing_saved_default_uses_private_model_library(download_client, monkeypatch):
    client, state, _ = download_client
    monkeypatch.setenv("ODYSSEUS_MODEL_DIR", str(state.parent / "models"))
    state.write_text(json.dumps({"env": {"servers": [{"host": "tester@gpu-box", "downloadDir": "/remote/hub"}]}}))
    result = client.post("/api/model/download", json={"repo_id": "example/model"}).json()
    assert result["cache_dir"] is None
    assert result["download_dir"] == str(state.parent / "models/example/model")


def test_legacy_saved_default_is_normalized_to_private_cache(tmp_path, monkeypatch):
    state = tmp_path / "cookbook_state.json"
    state.write_text(json.dumps({"env": {"servers": [{"host": "", "downloadDir": "~/.cache/huggingface/hub"}]}}))
    monkeypatch.setenv("HF_HUB_CACHE", "/private/hub")
    assert load_stored_local_download_dir(state_path=state) == "/private/hub"


@pytest.mark.parametrize("directory", [None, "/chosen/Models", "/chosen/Models/hub"])
def test_new_downloads_use_readable_repo_folders_without_extra_hub(download_client, directory):
    client, state, _ = download_client
    fields = {"local_dir": directory} if directory else {}
    result = client.post("/api/model/download", json={
        "repo_id": "example/model", "include": "weights/*Q4*", **fields,
    }).json()
    root = directory or "/mnt/e/AI/huggingface/hub"
    assert result["layout"] == "directory"
    assert result["model_root"] == root
    assert result["download_dir"] == root + "/example/model"
    assert result["cache_dir"] is None
    runner = (state.parent / (result["session_id"] + ".sh")).read_text()
    assert '--local-dir "$ODYSSEUS_HF_LOCAL_DIR"' in runner
    assert "export HF_HUB_CACHE=" not in runner
    assert "--include 'weights/*Q4*'" in runner
    assert "--exclude '.odysseus-model*'" in runner
    subprocess.run(["bash", "-n"], input=runner, text=True, check=True)
    metadata = json.loads((state.parent / (result["session_id"] + ".download.json")).read_text())
    assert metadata["download_dir"] == result["download_dir"]
    assert "hf_token" not in metadata


@pytest.mark.parametrize("platform,root,expected", [
    ("linux", "~/Models", "~/Models/example/model"),
    ("linux", None, "~/models/example/model"),
    ("windows", r"E:\AI Models", "E:/AI Models/example/model"),
    ("windows", None, "~/models/example/model"),
])
def test_remote_readable_directory_is_expanded_on_remote_host(download_client, platform, root, expected):
    client, state, _ = download_client
    result = client.post("/api/model/download", json={
        "repo_id": "example/model", "remote_host": "tester@gpu-box",
        "platform": platform, "local_dir": root,
    }).json()
    assert result["download_dir"] == expected
    suffix = "_run.ps1" if platform == "windows" else "_run.sh"
    runner = (state.parent / (result["session_id"] + suffix)).read_text()
    assert "/mnt/e/AI/huggingface/hub" not in runner
    if platform == "windows":
        assert '--local-dir "$env:ODYSSEUS_HF_LOCAL_DIR"' in runner
        assert "local_dir=os.path.expanduser(os.environ['ODYSSEUS_HF_LOCAL_DIR'])" in runner
        assert "ignore_patterns=['.odysseus-model*']" in runner
        if root is None:
            assert "Join-Path $HOME" in runner
    else:
        assert 'export ODYSSEUS_HF_LOCAL_DIR="$HOME/' in runner
        subprocess.run(["bash", "-n"], input=runner, text=True, check=True)


def test_invalid_layout_is_rejected_before_spawning(download_client):
    client, _, launch = download_client
    response = client.post("/api/model/download", json={"repo_id": "example/model", "layout": "invalid"})
    assert response.status_code == 422
    launch.assert_not_called()


@pytest.mark.parametrize("repo,include,usage,h3", [
    ("Abiray/Minimax-H3-nvfp4-INT4", None, None, True),
    ("sakamakismile/Qwen3-VL-MiniMax-H3-NVFP4", None, None, True),
    ("Alissonerdx/BFS-Best-Face-Swap-Video", "h3/*.safetensors", None, True),
    ("Alissonerdx/BFS-Best-Face-Swap-Video", "ltx-2/*.safetensors", None, False),
    ("Alissonerdx/BFS-Best-Face-Swap-Video", None, None, False),
    ("org/unknown-component", None, "h3", True),
    ("org/MiniMax-H3", None, "general", False),
    ("org/Qwen-27B-GGUF", None, None, False),
])
@pytest.mark.parametrize("sends_default", [False, True])
def test_h3_storage_policy_keeps_workflow_components_on_linux(download_client, monkeypatch, repo, include, usage, h3, sends_default):
    client, _, _ = download_client
    monkeypatch.setenv("ODYSSEUS_H3_MODEL_DIR", "/linux/models")
    fields = {"local_dir": "/mnt/e/AI/huggingface/hub"} if sends_default else {}
    result = client.post("/api/model/download", json={
        "repo_id": repo, "include": include, "usage": usage, **fields,
    }).json()
    assert result["model_root"] == ("/linux/models" if h3 else "/mnt/e/AI/huggingface/hub")


def test_h3_storage_policy_keeps_explicit_custom_and_remote_destinations(download_client, monkeypatch):
    client, _, _ = download_client
    monkeypatch.setenv("ODYSSEUS_H3_MODEL_DIR", "/linux/models")
    for fields, expected in [
        ({"local_dir": "/chosen/models"}, "/chosen/models"),
        ({"remote_host": "tester@gpu-box"}, "~/models"),
        ({"remote_host": "tester@gpu-box", "local_dir": "/remote/models"}, "/remote/models"),
    ]:
        result = client.post("/api/model/download", json={"repo_id": "org/MiniMax-H3", **fields}).json()
        assert result["model_root"] == expected


def test_h3_destination_metadata_is_server_owned(download_client, monkeypatch):
    client, state, _ = download_client
    monkeypatch.setenv("ODYSSEUS_H3_MODEL_DIR", "/linux/models")
    metadata = client.get("/api/cookbook/state").json()["env"]
    assert metadata["localH3DownloadDir"] == "/linux/models"
    assert client.post("/api/cookbook/state", json={"env": metadata}).json()["ok"]
    assert "localH3DownloadDir" not in json.loads(state.read_text())["env"]


@pytest.mark.parametrize("layout", ["directory", "cache"])
def test_unmounted_wsl_default_drive_stops_before_runner_creation(download_client, monkeypatch, layout):
    client, state, launch = download_client
    monkeypatch.setattr(routes, "is_wsl", lambda: True)
    monkeypatch.setattr(routes.os.path, "ismount", lambda path: False)
    sessions = state.parent / "sessions"
    monkeypatch.setattr(routes, "TMUX_LOG_DIR", sessions)
    response = client.post("/api/model/download", json={"repo_id": "org/model", "layout": layout})
    assert response.status_code == 409
    assert "E: is not mounted at /mnt/e" in response.json()["detail"]
    assert "No download was started" in response.json()["detail"]
    assert not sessions.exists()
    launch.assert_not_called()


def test_mounted_wsl_default_drive_records_runner_recheck(download_client, monkeypatch):
    client, state, launch = download_client
    monkeypatch.setattr(routes, "is_wsl", lambda: True)
    checked = []
    monkeypatch.setattr(routes.os.path, "ismount", lambda path: checked.append(path) or path == "/mnt/e")
    result = client.post("/api/model/download", json={"repo_id": "org/model"}).json()
    assert result["ok"]
    assert checked == ["/mnt/e"]
    runner = (state.parent / (result["session_id"] + ".sh")).read_text()
    assert "export ODYSSEUS_REQUIRED_DOWNLOAD_MOUNT=/mnt/e" in runner
    launch.assert_awaited_once()


@pytest.mark.parametrize("fields,root", [
    ({"local_dir": "/home/test/models"}, "/home/test/models"),
    ({"local_dir": "/mnt/custom-library"}, "/mnt/custom-library"),
    ({"local_dir": "/mnt/z/custom-linux-directory"}, "/mnt/z/custom-linux-directory"),
    ({"repo_id": "org/MiniMax-H3"}, "/linux/models"),
    ({"remote_host": "test@remote", "local_dir": "/mnt/e/Models"}, "/mnt/e/Models"),
])
def test_mount_guard_preserves_linux_h3_custom_and_remote_destinations(download_client, monkeypatch, fields, root):
    client, _, launch = download_client
    monkeypatch.setattr(routes, "is_wsl", lambda: True)
    monkeypatch.setenv("ODYSSEUS_H3_MODEL_DIR", "/linux/models")
    def unexpected_check(path):
        raise AssertionError("An unrelated Linux or remote folder was checked as a Windows drive")
    monkeypatch.setattr(routes.os.path, "ismount", unexpected_check)
    result = client.post("/api/model/download", json={"repo_id": "org/model", **fields}).json()
    assert result["ok"]
    assert result["model_root"] == root
    launch.assert_awaited_once()
