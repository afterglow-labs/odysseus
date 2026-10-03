"""Exercise the smoke probe's success/failure behavior without starting Odysseus."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import threading

import pytest


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "smoke_runtime.py"
_SPEC = importlib.util.spec_from_file_location("smoke_runtime_under_test", _PATH)
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


@contextmanager
def _server(ready):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/health":
                payload = {"status": "healthy"}
            else:
                payload = {"ready": ready}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_accepts_a_live_and_ready_server():
    with _server(ready=True) as url:
        smoke.check_endpoints(url, timeout=2)


def test_liveness_does_not_hide_broken_storage():
    with _server(ready=False) as url:
        with pytest.raises(RuntimeError, match="unexpected response.*ready"):
            smoke.check_endpoints(url, timeout=0.1)


def test_fails_immediately_when_the_child_exits():
    class Exited:
        returncode = 19

        def poll(self):
            return self.returncode

    with pytest.raises(RuntimeError, match="server exited with status 19"):
        smoke.check_endpoints("http://127.0.0.1:1", timeout=30, process=Exited())
