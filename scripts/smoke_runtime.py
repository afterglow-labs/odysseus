#!/usr/bin/env python3
"""Boot a real, isolated installation or probe an already-running container.

This intentionally runs outside pytest so missing dependencies cannot be hidden
by tests/conftest.py's optional import stubs. No model/provider is required.
"""

import argparse
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def check_endpoints(base_url: str, timeout: float, process=None) -> None:
    """Wait for startup, then require both HTTP liveness and storage readiness."""
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = "server did not respond"
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"server exited with status {process.returncode}")
        try:
            for path, key, expected in (
                ("/api/health", "status", "healthy"),
                ("/api/ready", "ready", True),
            ):
                with opener.open(base_url.rstrip("/") + path, timeout=2) as response:
                    payload = json.load(response)
                if payload.get(key) != expected:
                    raise ValueError(f"{path}: unexpected response {payload!r}")
            print("Runtime smoke passed: HTTP health and SQLite/data readiness.")
            return
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last_error = str(exc)
            time.sleep(0.25)
    raise RuntimeError(f"runtime did not become ready within {timeout:g}s: {last_error}")


def boot_and_check(timeout: float) -> None:
    # These include the compiled wheels most likely to lag a new Python release.
    # Importing the app alone is insufficient: vector features degrade when the
    # imports fail, which should not hide a broken default installation in CI.
    for name in ("numpy", "bcrypt", "cryptography", "chromadb", "fastembed"):
        importlib.import_module(name)

    with tempfile.TemporaryDirectory(prefix="odysseus-smoke-") as temp:
        workspace = Path(temp)
        data_dir = workspace / "data"
        data_dir.mkdir()
        env = os.environ.copy()
        env.update({
            "ODYSSEUS_DATA_DIR": str(data_dir),
            "DATABASE_URL": f"sqlite:///{(data_dir / 'app.db').as_posix()}",
            "AUTH_ENABLED": "false",
            "DEBUG": "false",
            "PYTHON_DOTENV_DISABLED": "1",
            "ODYSSEUS_STARTUP_WARMUPS": "false",
            "HF_HUB_OFFLINE": "1",
            "CHROMADB_HOST": "127.0.0.1",
            "CHROMADB_PORT": "1",
        })
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        env["APP_PORT"] = str(port)
        log_path = workspace / "server.log"
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(ROOT),
                 "--host", "127.0.0.1", "--port", str(port)],
                cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            try:
                check_endpoints(f"http://127.0.0.1:{port}", timeout, process)
            except Exception:
                log.flush()
                print(log_path.read_text(encoding="utf-8", errors="replace")[-16000:],
                      file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", help="Probe an existing server instead of starting one")
    parser.add_argument("--timeout", type=float, default=90)
    args = parser.parse_args()
    if args.url:
        check_endpoints(args.url, args.timeout)
    else:
        boot_and_check(args.timeout)


if __name__ == "__main__":
    main()
