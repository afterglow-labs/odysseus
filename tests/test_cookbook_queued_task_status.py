"""Unlaunched placeholders are queue entries, not failed tmux sessions."""

import json
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", ["", "user@server"])
async def test_queued_placeholder_is_not_probed_as_process_or_cached_download(tmp_path, monkeypatch, remote):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"tasks": [{
        "sessionId": "queue-waiting", "type": "download", "status": "queued",
        "remoteHost": remote, "payload": {"repo_id": "org/model", "include": "*Q4*"},
    }]}))
    monkeypatch.setattr(cookbook, "COOKBOOK_STATE_FILE", str(state))
    monkeypatch.setattr(cookbook, "require_admin", lambda request: None)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        raise AssertionError("An unlaunched queue entry must not be probed")
    monkeypatch.setattr(cookbook.subprocess, "run", run)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes
                   if r.path == "/api/cookbook/tasks/status")
    result = await handler(Request({"type": "http"}))
    assert calls == []
    task = result["tasks"][0]
    assert task["session_id"] == "queue-waiting"
    assert task["status"] == "queued"
    assert task["progress"] == "Waiting to start"
    assert task["diagnosis"] is None
    assert task["exit_code"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("session_id,status", [
    ("cookbook-launched", "running"), ("queue-no-longer-waiting", "running"),
])
async def test_launched_unreachable_task_still_reports_stopped(tmp_path, monkeypatch, session_id, status):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"tasks": [{
        "sessionId": session_id, "type": "serve", "status": status,
    }]}))
    monkeypatch.setattr(cookbook, "COOKBOOK_STATE_FILE", str(state))
    monkeypatch.setattr(cookbook, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook, "IS_WINDOWS", False)
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(cookbook.subprocess, "run", run)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes
                   if r.path == "/api/cookbook/tasks/status")
    result = await handler(Request({"type": "http"}))
    assert ["tmux", "has-session", "-t", session_id] in calls
    assert result["tasks"][0]["status"] == "stopped"
