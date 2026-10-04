"""Run bundled MCP servers without re-entering the desktop application."""

import asyncio
import os
import sys


def builtin_server_command(server_id: str, script_path: str) -> tuple[str, list[str]]:
    if getattr(sys, "frozen", False):
        # The windowed executable has no stdin/stdout pipes, even when spawned
        # by an MCP client. The console bootloader shares the same bundled code
        # and dependencies; the MCP SDK starts it with CREATE_NO_WINDOW.
        worker = os.path.join(os.path.dirname(sys.executable), "Odysseus-worker.exe")
        return worker, ["--mcp-server", server_id]
    return sys.executable, [script_path]


def run_builtin_server(server_id: str) -> None:
    # Explicit imports let PyInstaller collect each server and its dependencies.
    if server_id == "image_gen":
        from mcp_servers.image_gen_server import run
    elif server_id == "memory":
        from mcp_servers.memory_server import run
    elif server_id == "rag":
        from mcp_servers.rag_server import run
    elif server_id == "email":
        from mcp_servers.email_server import run
    else:
        raise SystemExit(f"Unknown built-in MCP server: {server_id}")
    asyncio.run(run())
