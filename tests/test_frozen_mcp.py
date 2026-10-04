"""Portable tools must speak MCP over pipes without starting another desktop."""

import os
from pathlib import Path
import subprocess
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from src.frozen_mcp import builtin_server_command


def test_source_command_uses_current_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert builtin_server_command("memory", "memory_server.py") == (
        sys.executable, ["memory_server.py"],
    )


def test_frozen_command_uses_bundled_stdio_worker(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Odysseus.exe"))
    assert builtin_server_command("memory", "memory_server.py") == (
        str(tmp_path / "Odysseus-worker.exe"), ["--mcp-server", "memory"],
    )


@pytest.mark.parametrize("server_id", ["image_gen", "memory", "rag", "email"])
async def test_worker_initializes_over_stdio_without_starting_app(tmp_path, server_id):
    launcher = Path(__file__).resolve().parent.parent / "launcher.py"
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(launcher), "--mcp-server", server_id],
        env={**os.environ, "ODYSSEUS_DATA_DIR": str(tmp_path)},
        cwd=str(tmp_path),
    )
    with anyio.fail_after(20):
        with open(os.devnull, "w") as stderr:
            async with stdio_client(params, errlog=stderr) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    result = await session.initialize()
                    assert result.serverInfo.name == server_id
                    assert (await session.list_tools()).tools
    assert not (tmp_path / "logs" / "app.log").exists()


def test_worker_rejects_unknown_server(tmp_path):
    launcher = Path(__file__).resolve().parent.parent / "launcher.py"
    result = subprocess.run(
        [sys.executable, str(launcher), "--mcp-server", "unknown"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "Unknown built-in MCP server" in result.stderr
