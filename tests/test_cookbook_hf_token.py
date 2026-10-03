"""Cookbook HF token persistence and lookup."""

import json
import os
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
from routes.cookbook_helpers import ServeRequest, load_stored_hf_token
from src.secret_storage import encrypt


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
