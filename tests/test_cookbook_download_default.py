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
    response = client.post("/api/model/download", json={"repo_id": "example/model", **fields})
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
        "repo_id": "example/model", "remote_host": "tester@gpu-box", "platform": platform,
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
    assert result["cache_dir"] == "/mnt/f/New cache/hub"
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


def test_missing_saved_default_uses_private_hf_environment(download_client):
    client, state, _ = download_client
    state.write_text(json.dumps({"env": {"servers": [{"host": "tester@gpu-box", "downloadDir": "/remote/hub"}]}}))
    result = client.post("/api/model/download", json={"repo_id": "example/model"}).json()
    assert result["cache_dir"] == "/private/hub"


def test_legacy_saved_default_is_normalized_to_private_cache(tmp_path, monkeypatch):
    state = tmp_path / "cookbook_state.json"
    state.write_text(json.dumps({"env": {"servers": [{"host": "", "downloadDir": "~/.cache/huggingface/hub"}]}}))
    monkeypatch.setenv("HF_HUB_CACHE", "/private/hub")
    assert load_stored_local_download_dir(state_path=state) == "/private/hub"
