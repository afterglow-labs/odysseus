"""Completed directory downloads must survive warnings and missing tmux panes."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook
from src.hf_directory_download import prepare_directory_download, finish_directory_download

WARNING = "Could not set the permissions on the file 'old.incomplete'. Error: [Errno 1] Operation not permitted.\nContinuing without setting permissions."


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,alive,output,expected", [
    ("saved-success", False, WARNING + "\nDOWNLOAD_OK", "completed"),
    ("receipt-success", False, WARNING, "completed"),
    ("pending", False, WARNING, "stopped"),
    ("live-warning", True, WARNING, "running"),
    ("live-success", True, WARNING + "\nDOWNLOAD_OK", "completed"),
    ("live-failure", True, WARNING + "\nDOWNLOAD_FAILED (exit 1)", "error"),
    ("saved-failure", False, "DOWNLOAD_FAILED (exit 1)", "error"),
])
async def test_download_status_uses_completion_evidence(tmp_path, monkeypatch, scenario, alive, output, expected):
    root = tmp_path / "model"
    root.mkdir()
    (root / "model.gguf").write_bytes(b"weights")
    partial = root / ".cache/huggingface/download/old-attempt.incomplete"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"old partial")
    prepare_directory_download("org/model", root, "*.gguf", {"model.gguf": 7})
    if scenario == "receipt-success":
        assert finish_directory_download("org/model", root, "*.gguf") == 0
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"tasks": [{
        "sessionId": "cookbook-download", "type": "download", "status": "running",
        "remoteHost": "", "output": output,
        "payload": {"repo_id": "org/model", "layout": "directory", "download_dir": str(root), "include": "*.gguf"},
    }]}))
    monkeypatch.setattr(cookbook, "COOKBOOK_STATE_FILE", str(state))
    monkeypatch.setattr(cookbook, "TMUX_LOG_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cookbook, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook, "IS_WINDOWS", False)
    actual_run = subprocess.run

    def run(args, **kwargs):
        if args[0] == "python3":
            return actual_run([sys.executable, *args[1:]], **kwargs)
        if args[:2] == ["tmux", "has-session"]:
            return SimpleNamespace(returncode=0 if alive else 1, stdout="", stderr="")
        if args[:2] == ["tmux", "capture-pane"]:
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        raise AssertionError(f"Unexpected command: {args[:2]}")

    monkeypatch.setattr(cookbook.subprocess, "run", run)
    handler = next(r.endpoint for r in cookbook.setup_cookbook_routes().routes if r.path == "/api/cookbook/tasks/status")
    result = await handler(Request({"type": "http"}))
    task = result["tasks"][0]
    assert task["status"] == expected
    assert output.splitlines()[-1] in task["output_tail"]
    assert partial.exists()
