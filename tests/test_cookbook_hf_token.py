"""Cookbook HF token persistence and lookup."""

import json
import os
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_helpers import ModelDownloadRequest, ServeRequest, load_stored_hf_token
from src.secret_storage import encrypt


@pytest.mark.asyncio
@pytest.mark.parametrize("subsequent_token", [None, ""])
async def test_state_save_encrypts_token_and_preserves_it_after_reload(
    tmp_path, monkeypatch, subsequent_token,
):
    import src.secret_storage as secrets

    state_path = tmp_path / "cookbook_state.json"
    monkeypatch.setattr(cookbook_routes, "COOKBOOK_STATE_FILE", str(state_path))
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(secrets, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secrets, "_fernet", None)

    def endpoint(method):
        return next(
            route.endpoint for route in cookbook_routes.setup_cookbook_routes().routes
            if route.path == "/api/cookbook/state" and method in route.methods
        )

    def request(body):
        return Request(
            {"type": "http", "method": "POST", "path": "/api/cookbook/state", "headers": []},
            receive=AsyncMock(return_value={"type": "http.request", "body": json.dumps(body).encode()}),
        )

    token = "hf_test_persistent_token_1234567890"
    settings = {"servers": [{"name": "Local", "host": ""}], "hfToken": token}
    assert (await endpoint("POST")(request({"env": settings})))["ok"]
    saved = json.loads(state_path.read_text())
    assert saved["env"]["hfToken"].startswith("enc:")
    assert token not in state_path.read_text()
    assert load_stored_hf_token(state_path=state_path) == token

    # A fresh router simulates restarting the server; GET exposes status only.
    client = await endpoint("GET")(request({}))
    assert client["env"]["hfTokenConfigured"] is True
    assert "hfToken" not in client["env"]
    assert token not in json.dumps(client)
    if subsequent_token is not None:
        client["env"]["hfToken"] = subsequent_token
    assert (await endpoint("POST")(request(client)))["ok"]
    assert load_stored_hf_token(state_path=state_path) == token

    # A rejected replacement must leave the working token intact.
    client["env"]["hfToken"] = "invalid token\n"
    assert not (await endpoint("POST")(request(client)))["ok"]
    assert load_stored_hf_token(state_path=state_path) == token


def test_load_stored_hf_token_reads_encrypted_state(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    state_path = tmp_path / "cookbook_state.json"
    state_path.write_text(
        json.dumps({"env": {"hfToken": encrypt("hf_test_token_12345")}}),
        encoding="utf-8",
    )
    assert load_stored_hf_token() == "hf_test_token_12345"
    assert load_stored_hf_token(state_path=state_path) == "hf_test_token_12345"


def test_load_stored_hf_token_falls_back_to_env_when_state_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "hf_from_env")
    assert load_stored_hf_token() == "hf_from_env"


def test_load_stored_hf_token_prefers_state_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "hf_from_env")
    state_path = tmp_path / "cookbook_state.json"
    state_path.write_text(
        json.dumps({"env": {"hfToken": encrypt("hf_from_state")}}),
        encoding="utf-8",
    )
    assert load_stored_hf_token() == "hf_from_state"


@pytest.mark.asyncio
@pytest.mark.parametrize("remote,platform", [(None, ""), ("tester@gpu-box", ""), ("tester@gpu-box", "windows")])
async def test_download_runners_enable_visible_hf_progress(tmp_path, monkeypatch, remote, platform):
    # Capture the real generated runner without starting a download or SSH.
    process = AsyncMock()
    process.returncode = 1
    process.stderr.read.return_value = b"launch intentionally stopped"
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook_routes, "COOKBOOK_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cookbook_routes, "load_stored_hf_token", lambda **kwargs: "")
    monkeypatch.setattr(cookbook_routes, "_binary_available", AsyncMock(return_value=True))
    monkeypatch.setattr(cookbook_routes.asyncio, "create_subprocess_shell", AsyncMock(return_value=process))
    endpoint = next(
        route.endpoint for route in cookbook_routes.setup_cookbook_routes().routes
        if route.path == "/api/model/download" and "POST" in route.methods
    )
    response = await endpoint(
        Request({"type": "http", "method": "POST", "path": "/api/model/download", "headers": []}),
        ModelDownloadRequest(repo_id="example/model", remote_host=remote, platform=platform),
    )
    assert response["error"] == "launch intentionally stopped"
    runner = next(tmp_path.glob("*.ps1" if platform == "windows" else "*.sh")).read_text()
    # Successful HF CLI exit alone is insufficient: a zero-match include
    # can return a nonexistent snapshot path. Validate once after retries.
    assert "DOWNLOAD_EMPTY:" in runner
    assert runner.index("DOWNLOAD_EMPTY:") < runner.index('DOWNLOAD_OK')
    if platform == "windows":
        assert '$env:HF_HUB_DISABLE_PROGRESS_BARS = "0"' in runner
        assert '$env:TQDM_DISABLE = "0"' in runner
    else:
        assert "export HF_HUB_DISABLE_PROGRESS_BARS=0" in runner
        assert "export TQDM_DISABLE=0" in runner


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [None, "tester@gpu-box"])
@pytest.mark.parametrize("hf_token", ["", "hf_test_runner_token"])
@pytest.mark.parametrize("pip_install", [True, False])
async def test_serve_runner_hf_status_only_for_models(
    monkeypatch, tmp_path, remote, hf_token, pip_install,
):
    # Stop after writing the runner: no tmux, SSH, installs, or endpoint changes.
    process = AsyncMock()
    process.returncode = 1
    process.stderr.read.return_value = b"launch intentionally stopped"
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", False)
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook_routes, "load_stored_hf_token", lambda **kwargs: "")
    monkeypatch.setattr(cookbook_routes, "_binary_available", AsyncMock(return_value=True))
    monkeypatch.setattr(
        cookbook_routes.asyncio, "create_subprocess_shell", AsyncMock(return_value=process),
    )
    endpoint = next(
        route.endpoint for route in cookbook_routes.setup_cookbook_routes().routes
        if route.path == "/api/model/serve" and "POST" in route.methods
    )
    response = await endpoint(
        Request({"type": "http", "method": "POST", "path": "/api/model/serve", "headers": []}),
        ServeRequest(
            repo_id="playwright" if pip_install else "example/model",
            cmd="python3 -m pip install playwright" if pip_install else "vllm serve example/model",
            remote_host=remote,
            hf_token=hf_token,
        ),
    )

    assert response["error"] == "launch intentionally stopped"
    runner = next(tmp_path.glob("serve-*_run.sh")).read_text(encoding="utf-8")
    assert ("[odysseus] HF token:" in runner) is (not pip_install)
    assert ("export HF_TOKEN=" in runner) is bool(hf_token)
    if hf_token:
        assert f"export HF_TOKEN='{hf_token}'" in runner
