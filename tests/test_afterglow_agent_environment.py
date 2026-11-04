"""Afterglow's server credential must not ride into agent child processes."""

import os
import subprocess
import sys

import pytest

from src.agent_environment import agent_subprocess_env, protect_server_process_secrets
from src.agent_tools import subprocess_tools
from src import bg_jobs


def test_agent_environment_removes_only_afterglow_server_secrets():
    source = {
        "AFTERGLOW_TOKEN": "token-sentinel",
        "afterglow_password": "password-sentinel",
        "AFTERGLOW_URL": "http://127.0.0.1:8787",
        "SAFE_MARKER": "available",
    }
    assert agent_subprocess_env(source) == {
        "AFTERGLOW_URL": "http://127.0.0.1:8787",
        "SAFE_MARKER": "available",
    }
    assert source["AFTERGLOW_TOKEN"] == "token-sentinel"


def test_process_protection_is_inactive_without_afterglow_credentials():
    assert protect_server_process_secrets({"SAFE_MARKER": "available"}) is False


@pytest.mark.skipif(sys.platform != "linux", reason="Linux prctl and /proc are required")
def test_linux_agent_cannot_read_credential_bearing_server_environment():
    probe_code = (
        "import sys\n"
        "try:\n"
        "    with open(f'/proc/{sys.argv[1]}/environ', 'rb') as handle:\n"
        "        handle.read(1)\n"
        "    print('readable')\n"
        "except OSError:\n"
        "    print('blocked')\n"
    )
    server_code = f"""
import os
import subprocess
import sys
from src.agent_environment import protect_server_process_secrets

probe_code = {probe_code!r}
assert protect_server_process_secrets()
result = subprocess.run([sys.executable, '-c', probe_code, str(os.getpid())],
                        capture_output=True, text=True, check=True)
assert result.stdout.strip() == 'blocked', result.stdout
print('protected')
"""
    environment = {**os.environ, "AFTERGLOW_TOKEN": "test-only-sentinel"}
    result = subprocess.run([sys.executable, "-c", server_code], env=environment,
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "protected"


@pytest.mark.asyncio
async def test_agent_bash_and_python_do_not_inherit_gateway_credential(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tool_execution.agent_cwd", lambda: str(tmp_path))
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "AFTERGLOW_TOKEN": "token-sentinel",
        "AFTERGLOW_PASSWORD": "password-sentinel",
        "SAFE_MARKER": "available",
    }
    bash = await subprocess_tools.BashTool().execute(
        'test -z "${AFTERGLOW_TOKEN-}" && test -z "${AFTERGLOW_PASSWORD-}" && test "$SAFE_MARKER" = available',
        {"subproc_env": child_env, "session_id": None},
    )
    python = await subprocess_tools.PythonTool().execute(
        'import os, sys; sys.exit(0 if not os.getenv("AFTERGLOW_TOKEN") and not os.getenv("AFTERGLOW_PASSWORD") and os.getenv("SAFE_MARKER") == "available" else 1)',
        {"subproc_env": child_env},
    )
    assert bash["exit_code"] == 0
    assert python["exit_code"] == 0


def test_background_agent_job_does_not_inherit_gateway_credential(tmp_path, monkeypatch):
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "jobs.json")
    monkeypatch.setenv("AFTERGLOW_TOKEN", "token-sentinel")
    monkeypatch.setenv("AFTERGLOW_PASSWORD", "password-sentinel")
    monkeypatch.setenv("SAFE_MARKER", "available")
    captured = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(_argv, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(bg_jobs.subprocess, "Popen", fake_popen)
    bg_jobs.launch("true", session_id="test-session", cwd=str(tmp_path))
    assert "AFTERGLOW_TOKEN" not in captured["env"]
    assert "AFTERGLOW_PASSWORD" not in captured["env"]
    assert captured["env"]["SAFE_MARKER"] == "available"


@pytest.mark.asyncio
async def test_tmux_agent_shell_starts_with_clean_environment(monkeypatch):
    calls = []

    async def has_session(_name):
        return bool(calls)

    async def fake_exec(*args, **_kwargs):
        calls.append(args)
        return "", "", 0

    monkeypatch.setattr(subprocess_tools, "_tmux_has_session", has_session)
    monkeypatch.setattr(subprocess_tools, "_run_exec", fake_exec)
    await subprocess_tools._ensure_tmux_session(
        "ody-agent-test", "/tmp",
        {"AFTERGLOW_TOKEN": "token-sentinel", "AFTERGLOW_PASSWORD": "password-sentinel", "SAFE_MARKER": "available"},
    )
    new_session = calls[0]
    assert new_session[0:2] == ("tmux", "new-session")
    assert new_session[7:9] == ("env", "-i")
    assert "SAFE_MARKER=available" in new_session
    assert not any(arg.startswith(("AFTERGLOW_TOKEN=", "AFTERGLOW_PASSWORD=")) for arg in new_session)


@pytest.mark.asyncio
async def test_mcp_stdio_does_not_inherit_gateway_credential(monkeypatch):
    from mcp.client import stdio
    from src.mcp_manager import McpManager

    captured = {}
    monkeypatch.setenv("AFTERGLOW_TOKEN", "token-sentinel")
    monkeypatch.setenv("AFTERGLOW_PASSWORD", "password-sentinel")

    class StopBeforeSpawn:
        def __init__(self, params):
            captured["env"] = params.env

        async def __aenter__(self):
            raise RuntimeError("fixture stopped MCP before spawn")

        async def __aexit__(self, _type, _value, _traceback):
            return False

    monkeypatch.setattr(stdio, "stdio_client", StopBeforeSpawn)
    with pytest.raises(RuntimeError, match="fixture stopped MCP before spawn"):
        await McpManager()._connect_stdio(
            "fixture", "Fixture", "unused", [],
            {"SAFE_MARKER": "available", "AFTERGLOW_TOKEN": "explicit-token-sentinel"},
        )
    assert "AFTERGLOW_TOKEN" not in captured["env"]
    assert "AFTERGLOW_PASSWORD" not in captured["env"]
    assert captured["env"]["SAFE_MARKER"] == "available"
