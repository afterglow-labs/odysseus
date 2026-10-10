#!/bin/bash
# Build the repository's macOS launcher, then open its browser-based UI.
set -euo pipefail

usage() {
  echo "Usage: $0 [--verify|--verify-existing|--logs]"
  echo "  --verify  Check the repository server and HTTP/storage readiness."
  echo "  --verify-existing  Check the running app without rebuilding or restarting."
  echo "  --logs    Follow logs/odysseus-app.log after opening the app."
  echo "Set ODYSSEUS_PORT to override the default port (7860)."
  echo "Set ODYSSEUS_SERVER_URL to open a remote Odysseus client instead."
}

MODE="${1:-run}"
if [ "$#" -gt 1 ]; then usage >&2; exit 2; fi
case "$MODE" in
  run|--verify|--verify-existing|--logs) ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if [ "$(uname -s)" != Darwin ]; then
  echo "This launcher requires macOS." >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
APP_BUNDLE="$ROOT_DIR/dist/Odysseus.app"
APP_EXECUTABLE="$APP_BUNDLE/Contents/MacOS/Odysseus"
VENV_PY="$ROOT_DIR/venv/bin/python"
LOG="$ROOT_DIR/logs/odysseus-app.log"
if [ -n "${ODYSSEUS_SERVER_URL:-}" ]; then
  URL="$("$ROOT_DIR/build-macos-app.sh" --check-server-url)"
  if [ "$MODE" = --logs ]; then
    echo "Remote mode has no local server log. Use --verify to check the server." >&2
    exit 2
  fi
  verify_remote() {
    "$VENV_PY" - "$URL" <<'PY'
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

url = sys.argv[1]
origin = urllib.parse.urlsplit(url)

class SameServerRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        target = urllib.parse.urlsplit(new_url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("Remote server redirected to another origin")
        return super().redirect_request(request, fp, code, message, headers, new_url)

# Default HTTPS verification stays enabled. This probe carries no cookies.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SameServerRedirect())
def fetch(path):
    with opener.open(url + path, timeout=15) as response:
        return response.read(4 * 1024 * 1024).decode("utf-8")

try:
    if json.loads(fetch("/api/health")).get("status") != "healthy":
        raise ValueError("Remote server is not healthy")
    status = json.loads(fetch("/api/auth/status"))
    if any(type(status.get(key)) is not bool for key in
           ("configured", "authenticated", "is_admin", "signup_enabled")):
        raise ValueError("Remote authentication status is invalid")
    if status["authenticated"] or status["is_admin"] or status.get("username") is not None:
        raise ValueError("Remote server unexpectedly authenticated an anonymous request")
    page = fetch("/")
    if 'id="authForm"' not in page and 'id="chat-container"' not in page:
        raise ValueError("Remote Odysseus login or application page is unavailable")
except (OSError, ValueError) as exc:
    raise SystemExit(f"Remote verification failed: {exc}")
print(f"Verified remote Odysseus health and login/application page: {url}")
PY
  }
  if [ "$MODE" = --verify-existing ]; then
    verify_remote
    exit 0
  fi
  ODYSSEUS_SERVER_URL="$URL" "$ROOT_DIR/build-macos-app.sh"
  /usr/bin/open -n "$APP_BUNDLE"
  echo "Opened remote Odysseus client ($URL)"
  if [ "$MODE" = --verify ]; then verify_remote; fi
  exit 0
fi

PORT="${ODYSSEUS_PORT:-7860}"
if [[ ! "$PORT" =~ ^[0-9]{1,5}$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  echo "ODYSSEUS_PORT must be a port number between 1 and 65535." >&2
  exit 2
fi
PORT="$((10#$PORT))"
URL="http://127.0.0.1:$PORT"

# Check the environment before interrupting a working instance.
"$ROOT_DIR/build-macos-app.sh" --check-python

listener_pids() {
  /usr/sbin/lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

is_repo_server() {
  local command cwd
  command="$(/bin/ps -p "$1" -o command= 2>/dev/null)" || return 1
  case "$command" in *" -m uvicorn app:app "*) ;; *) return 1 ;; esac
  cwd="$(/usr/sbin/lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')" || return 1
  [ "$cwd" = "$ROOT_DIR" ]
}

is_repo_launcher() {
  local command
  command="$(/bin/ps -p "$1" -o command= 2>/dev/null)" || return 1
  case "$command" in
    "$APP_EXECUTABLE"|"/bin/bash $APP_EXECUTABLE"|"bash $APP_EXECUTABLE") return 0 ;;
    *) return 1 ;;
  esac
}

launcher_pids() {
  /bin/ps -axo pid=,command= | while read -r pid command; do
    case "$command" in
      "$APP_EXECUTABLE"|"/bin/bash $APP_EXECUTABLE"|"bash $APP_EXECUTABLE") echo "$pid" ;;
    esac
  done
}

verify_running() {
  local server_pids pid attempt
  for ((attempt = 0; attempt < 120; attempt++)); do
    server_pids="$(listener_pids)"
    [ -n "$server_pids" ] && break
    sleep 1
  done
  [ -n "$server_pids" ] || { echo "No listening server found after launch." >&2; return 1; }
  for pid in $server_pids; do
    is_repo_server "$pid" || { echo "Listener $pid is not this repository's server." >&2; return 1; }
  done
  # The public login flow must work with authentication left enabled. Run the
  # protected storage check locally only after proving this server belongs here.
  "$VENV_PY" - "$ROOT_DIR" "$URL" <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

root, url = sys.argv[1:]
os.chdir(root)
sys.path.insert(0, root)
from dotenv import load_dotenv
load_dotenv(Path(root) / ".env", encoding="utf-8-sig")
from src.owner_identity import auth_disabled

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch(path):
    try:
        response = opener.open(url + path, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read().decode("utf-8")

deadline = time.monotonic() + 120
while True:
    try:
        code, body = fetch("/api/health")
        if code == 200 and json.loads(body).get("status") == "healthy":
            break
    except (OSError, ValueError):
        pass
    if time.monotonic() >= deadline:
        raise SystemExit("The repository server did not become healthy within 120 seconds.")
    time.sleep(0.5)

code, body = fetch("/api/auth/status")
status = json.loads(body)
if code != 200 or any(type(status.get(key)) is not bool for key in
                       ("configured", "authenticated", "is_admin", "signup_enabled")):
    raise SystemExit("The authentication status response is invalid.")
if status["authenticated"] or status.get("username") is not None or status["is_admin"]:
    raise SystemExit("An unauthenticated verification request unexpectedly has a user session.")

disabled = auth_disabled()
code, body = fetch("/" if disabled else "/login")
if code != 200 or (not disabled and 'id="authForm"' not in body):
    raise SystemExit("The app's login/setup page is not reachable.")
code, body = fetch("/api/ready")
if disabled:
    if code != 200 or json.loads(body).get("ready") is not True:
        raise SystemExit("HTTP storage readiness failed.")
elif code != 401:
    raise SystemExit(f"Protected readiness should require authentication; received HTTP {code}.")

from src.readiness import check_readiness
result = check_readiness()
if result.get("ready") is not True:
    raise SystemExit(f"Local storage readiness failed: {result['checks']}")
screen = "app" if disabled else ("login" if status["configured"] else "first-run setup")
print(f"Verified HTTP health, {screen} page, authentication policy, and local database/data readiness.")
PY
  echo "Verified repository server PID(s): $server_pids"
}

if [ "$MODE" = --verify-existing ]; then
  verify_running
  exit 0
fi

# Never stop a process merely because it owns our desired port.
for pid in $(listener_pids); do
  if ! is_repo_server "$pid"; then
    echo "Port $PORT belongs to another process (PID $pid); leaving it running." >&2
    echo "Choose another port with ODYSSEUS_PORT=7900 $0 $MODE" >&2
    exit 1
  fi
done

STOP_PIDS="$( { listener_pids; launcher_pids; } | sort -nu)"
for pid in $STOP_PIDS; do
  # Recheck ownership immediately before sending a signal.
  if is_repo_launcher "$pid" || is_repo_server "$pid"; then
    echo "Stopping repository Odysseus process ${pid}..."
    kill -TERM "$pid" 2>/dev/null || true
  fi
done
for pid in $STOP_PIDS; do
  for ((attempt = 0; attempt < 20; attempt++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Process $pid did not exit; stopping before rebuilding. No force-kill was sent." >&2
    exit 1
  fi
done
if [ -n "$(listener_pids)" ]; then
  echo "Port $PORT is still occupied; stopping before launch." >&2
  exit 1
fi

ODYSSEUS_PORT="$PORT" "$ROOT_DIR/build-macos-app.sh"
/usr/bin/open -n "$APP_BUNDLE"
echo "Opened $APP_BUNDLE ($URL)"

case "$MODE" in
  --verify)
    verify_running
    ;;
  --logs)
    mkdir -p "$ROOT_DIR/logs"
    touch "$LOG"
    exec /usr/bin/tail -n 60 -F "$LOG"
    ;;
esac
