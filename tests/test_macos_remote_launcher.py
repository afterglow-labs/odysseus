"""Exercise the generated client and run wrapper without opening real applications."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time

import pytest


REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt", reason="macOS shell launcher")


def executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout's & $literal"
    root.mkdir()
    for relative in ("build-macos-app.sh", "script/build_and_run.sh", "src/python_runtime.py", ".python-version"):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, destination)
    python = root / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    commands = tmp_path / "commands"
    executable(commands / "hdiutil", "#!/bin/bash\ntouch \"${!#}\"\n")
    executable(commands / "uname", "#!/bin/sh\necho Darwin\n")
    env = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ.get("PATH", ""),
               HOME=str(tmp_path / "home"), TMPDIR=str(tmp_path))
    env.pop("ODYSSEUS_SERVER_URL", None)
    return root, env


def run(root, env, script="build-macos-app.sh", *args):
    return subprocess.run(["bash", str(root / script), *args], cwd=root, env=env,
                          capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "https://user:secret@example.test", "https://example.test?x=1",
    "https://example.test#fragment", "https://example.test/path", "https://example.test:0",
    "https://example.test:65536", "https://example.test:", "https://example.test\n",
    "https://example.test/$(touch injected)", "https://example.test\\@elsewhere.test",
    "https://[::1]unexpected",
])
def test_invalid_remote_url_fails_before_build(checkout, url):
    root, env = checkout
    result = run(root, dict(env, ODYSSEUS_SERVER_URL=url), "build-macos-app.sh", "--check-server-url")
    assert result.returncode != 0
    assert "Invalid ODYSSEUS_SERVER_URL" in result.stderr
    assert not (root / "dist").exists()


@pytest.mark.parametrize("url,expected", [
    ("https://EXAMPLE.test/", "https://example.test"),
    ("http://127.0.0.1:7860", "http://127.0.0.1:7860"),
    ("https://[::1]:7860/", "https://[::1]:7860"),
])
def test_remote_origin_validation(checkout, url, expected):
    root, env = checkout
    result = run(root, dict(env, ODYSSEUS_SERVER_URL=url), "build-macos-app.sh", "--check-server-url")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_remote_bundle_runs_without_checkout_and_opens_app_window(checkout, tmp_path):
    root, env = checkout
    url = "https://example.test:8443"
    result = run(root, dict(env, ODYSSEUS_SERVER_URL=url))
    assert result.returncode == 0, result.stdout + result.stderr
    launcher = root / "dist/Odysseus.app/Contents/MacOS/Odysseus"
    generated = launcher.read_text()
    # Replace only OS/browser integrations in the fixture. The generated shell,
    # quoting and remote/local branches execute unchanged.
    browser = tmp_path / "Applications/Google Chrome.app/Contents/MacOS/client"
    args_file = tmp_path / "opened"
    executable(browser, '#!/bin/sh\nprintf "%s\\n" "$@" > "$OPENED"\n')
    defaults = tmp_path / "defaults"
    executable(defaults, "#!/bin/sh\necho client\n")
    generated = generated.replace('"/Applications"', json.dumps(str(tmp_path / "Applications")))
    generated = generated.replace("/usr/bin/defaults", str(defaults))
    fallback = tmp_path / "unexpected-browser-fallback"
    executable(fallback, "#!/bin/sh\nexit 93\n")
    generated = generated.replace("/usr/bin/open", str(fallback))
    executable(tmp_path / "client-launcher", generated)
    # The source checkout and its Python are unavailable on the client machine.
    root.rename(root.with_name("moved-checkout"))
    result = subprocess.run(["bash", str(tmp_path / "client-launcher")], env=dict(env, OPENED=str(args_file)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 2
    while not args_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert args_file.read_text().splitlines() == [f"--app={url}", "--new-window"]
    assert not root.exists()


def test_remote_wrapper_does_not_check_or_stop_local_runtime(checkout, tmp_path):
    root, env = checkout
    calls = tmp_path / "calls"
    executable(root / "build-macos-app.sh", """#!/bin/sh
printf 'build:%s\n' "$*" >> "$CALLS"
if [ "$1" = --check-server-url ]; then echo https://example.test; fi
""")
    opener = tmp_path / "open-app"
    executable(opener, '#!/bin/sh\nprintf "open:%s\\n" "$*" >> "$CALLS"\n')
    wrapper = root / "script/build_and_run.sh"
    wrapper.write_text(wrapper.read_text().replace("/usr/bin/open", str(opener)))
    result = run(root, dict(env, CALLS=str(calls), ODYSSEUS_SERVER_URL="https://example.test",
                            ODYSSEUS_PORT="not-a-local-port"), "script/build_and_run.sh")
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == ["build:--check-server-url", "build:",
                                             f"open:-n {root}/dist/Odysseus.app"]
    assert not (root / "logs").exists()


@pytest.mark.parametrize("valid_ui", [True, False])
def test_remote_verify_checks_actual_ui_without_local_data(checkout, valid_ui):
    root, env = checkout
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            if self.path == "/api/health":
                body = '{"status":"healthy"}'
            elif self.path == "/api/auth/status":
                body = json.dumps(dict(configured=True, authenticated=False, is_admin=False, signup_enabled=False))
            else:
                body = '<form id="authForm"></form>' if valid_ui else "unrelated application"
            self.wfile.write(body.encode())

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            result = run(root, dict(env, ODYSSEUS_SERVER_URL=f"http://127.0.0.1:{server.server_port}",
                                   ODYSSEUS_PORT="invalid"), "script/build_and_run.sh", "--verify-existing")
        finally:
            server.shutdown()
            worker.join()
    assert (result.returncode == 0) is valid_ui, result.stdout + result.stderr
    assert requests == ["/api/health", "/api/auth/status", "/"]
    assert not (root / "data").exists()
    assert not (root / "dist").exists()
