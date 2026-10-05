import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

import pytest

pytest.importorskip("fcntl")
from src import cookbook_stop as stop


@pytest.fixture
def stubborn_job(tmp_path, monkeypatch):
    tmux = shutil.which("tmux")
    if not tmux:
        pytest.skip("tmux is required for real process shutdown tests")
    socket = "odysseus-stop-test-" + uuid.uuid4().hex
    wrapper = tmp_path / "tmux"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(tmux) + " -L " + socket + ' "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    script = tmp_path / "worker.py"
    script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
mode = sys.argv[1]
if mode != "parent": os.setsid()
for sig in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM): signal.signal(sig, signal.SIG_IGN)
Path(__file__).with_name(mode + ".pid").write_text(str(os.getpid()))
if mode != "grandchild": subprocess.Popen([sys.executable, __file__, "child" if mode == "parent" else "grandchild"])
while True: time.sleep(0.1)
''')
    sid = "serve-fixture"
    command = "export ODYSSEUS_COOKBOOK_SESSION=" + sid + "; exec " + shlex.join([sys.executable, str(script), "parent"])
    subprocess.run([str(wrapper), "new-session", "-d", "-s", sid, command], check=True)
    subprocess.run([str(wrapper), "new-session", "-d", "-s", sid + "-unrelated", "sleep 60"], check=True)
    pids = []
    identities = {}
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if all((tmp_path / (mode + ".pid")).exists() for mode in ("parent", "child", "grandchild")):
                pids = [int((tmp_path / (mode + ".pid")).read_text()) for mode in ("parent", "child", "grandchild")]
                break
            time.sleep(0.05)
        assert len(pids) == 3, "All three test processes must start"
        identities = {pid: stop._snapshot()[pid][1] for pid in pids}
        yield sid, pids, wrapper, tmp_path / "records"
    finally:
        for pid in pids:
            if stop._snapshot().get(pid, (0, None))[1] == identities.get(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        subprocess.run([tmux, "-L", socket, "kill-server"], capture_output=True)


@pytest.mark.parametrize("terminal_gone", [False, True])
def test_stop_kills_owned_detached_grandchildren_but_not_neighbor(stubborn_job, terminal_gone):
    sid, pids, tmux, records = stubborn_job
    if terminal_gone:
        subprocess.run([str(tmux), "kill-session", "-t", "=" + sid], check=True)
        assert all(pid in stop._snapshot() for pid in pids), "Reproduce processes surviving tmux shutdown"
    result = stop.stop_job(sid, records, .1, .1, 1)
    assert result["ok"] is True, result
    assert result["remaining_pids"] == []
    assert all(pid not in stop._snapshot() for pid in pids)
    assert subprocess.run([str(tmux), "has-session", "-t", "=" + sid + "-unrelated"], capture_output=True).returncode == 0
    assert stop.stop_job(sid, records)["ok"] is True  # retry after terminal disappeared


def test_survivors_are_not_reported_stopped_and_can_be_retried(stubborn_job, monkeypatch):
    sid, pids, tmux, records = stubborn_job
    with monkeypatch.context() as patch:
        patch.setattr(stop.os, "kill", lambda *args: None)
        result = stop.stop_job(sid, records, .01, .01, .01)
    assert result["ok"] is False
    assert set(pids) <= set(result["remaining_pids"])
    # The saved identities survive losing both the terminal and env markers.
    subprocess.run([str(tmux), "kill-session", "-t", "=" + sid], check=True)
    monkeypatch.setattr(stop, "_marked", lambda *args: set())
    assert stop.stop_job(sid, records, .01, .01, 1)["ok"] is True
    assert all(pid not in stop._snapshot() for pid in pids)


def test_missing_legacy_terminal_is_not_proof_of_process_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(stop.shutil, "which", lambda _: "/usr/bin/tmux")
    monkeypatch.setattr(stop, "_tmux", lambda *args: None)
    result = stop.stop_job("serve-never-recorded", tmp_path)
    assert result["ok"] is False
    assert "verify shutdown" in result["error"]


def test_reused_pid_is_not_signalled(tmp_path, monkeypatch):
    (tmp_path / "serve-old.processes.json").write_text(json.dumps({"5000": "old-start"}))
    monkeypatch.setattr(stop, "_snapshot", lambda: {5000: (1, "new-start")})
    monkeypatch.setattr(stop, "_marked", lambda *args: set())
    monkeypatch.setattr(stop, "_tmux", lambda *args: None)
    monkeypatch.setattr(stop.shutil, "which", lambda _: "/usr/bin/tmux")
    kill = Mock(side_effect=AssertionError("Must not kill a reused PID"))
    monkeypatch.setattr(stop.os, "kill", kill)
    assert stop.stop_job("serve-old", tmp_path)["ok"] is True
    kill.assert_not_called()


def request(*, admin=True, configured=True, body=None):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=configured, is_admin=lambda user: admin))),
        state=SimpleNamespace(current_user="test-user"),
        headers={"sec-fetch-site": "same-origin"},
        json=AsyncMock(return_value=body or {}),
    )


@pytest.fixture
def api(tmp_path, monkeypatch):
    import routes.cookbook_routes as routes
    path = tmp_path / "cookbook.json"
    path.write_text(json.dumps({"env": {"keep": True}, "tasks": [
        {"sessionId": "serve-one", "status": "running", "type": "serve"},
        {"sessionId": "serve-two", "status": "running", "type": "serve"}]}))
    monkeypatch.setattr(routes, "COOKBOOK_STATE_FILE", str(path))
    monkeypatch.setattr(routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    router = routes.setup_cookbook_routes()
    endpoints = {route.path: route.endpoint for route in router.routes}
    return endpoints["/api/cookbook/tasks/{session_id}/stop"], endpoints["/api/cookbook/state"], path, routes


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"admin": False}, {"configured": False}])
async def test_stop_requires_admin_before_process_work(api, monkeypatch, options):
    from fastapi import HTTPException
    monkeypatch.setattr(stop, "stop_job", Mock(side_effect=AssertionError("Must not signal processes")))
    with pytest.raises(HTTPException) as error:
        await api[0]("serve-one", request(**options))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_stop_route_only_persists_verified_success_and_preserves_concurrent_edits(api, monkeypatch):
    endpoint, save, path, _ = api
    initial = path.read_text()
    worker = Mock(return_value={"ok": False, "error": "Worker survived", "remaining_pids": [9999]})
    monkeypatch.setattr(stop, "stop_job", worker)
    assert (await endpoint("serve-one", request()))["ok"] is False
    assert path.read_text() == initial

    def success(*args):
        current = json.loads(path.read_text())
        current["env"]["another-device"] = True
        path.write_text(json.dumps(current))
        return {"ok": True, "status": "stopped", "remaining_pids": []}
    worker.side_effect = success
    assert (await endpoint("serve-one", request()))["ok"] is True
    latest = json.loads(path.read_text())
    assert latest["env"]["another-device"] is True
    assert latest["tasks"][0]["_stopVerifiedAtMs"] > 0
    assert latest["tasks"][1]["status"] == "running"
    await save(request(body=json.loads(initial)))
    assert json.loads(path.read_text())["tasks"][0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_remote_stop_uses_saved_target_and_payload_fallback(api, monkeypatch):
    endpoint, _, path, routes = api
    state = json.loads(path.read_text())
    state["tasks"][0]["payload"] = {"remote_host": "tester@remote", "ssh_port": "2223", "platform": "darwin"}
    path.write_text(json.dumps(state))
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "status": "stopped", "remaining_pids": []})))
    monkeypatch.setattr(routes.subprocess, "run", run)
    result = await endpoint("serve-one", request(body={"host": "wrong-host", "pid": 1000}))
    assert result["ok"] is True
    command = run.call_args.args[0]
    assert command[-2] == "tester@remote"
    assert command[command.index("-p") + 1] == "2223"
    assert command[-1].endswith(" serve-one")
    assert "wrong-host" not in command
