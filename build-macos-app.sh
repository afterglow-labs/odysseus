#!/bin/bash
# Build a downloadable macOS launcher app + .dmg for Odysseus.
#
#   ./build-macos-app.sh
#
# Produces:
#   dist/Odysseus.app   — double-click: starts the local server (using this
#                         repo's venv) and opens the UI in an app-style window.
#   dist/Odysseus.dmg   — drag-to-Applications disk image (the downloadable).
#
# This is a *launcher* wrapper: it drives the venv we set up in this repo, it
# does not bundle Python. The install path is baked into the app at build time,
# so rebuild if you move the repo. Override the port with ODYSSEUS_PORT.
# Set ODYSSEUS_SERVER_URL to build a remote client that needs no local runtime.
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Odysseus"
INSTALL_DIR="$REPO_DIR"
PORT="${ODYSSEUS_PORT:-7860}"
DIST="$REPO_DIR/dist"
APP="$DIST/$APP_NAME.app"

VENV_PY="$REPO_DIR/venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
  echo "Odysseus needs its supported Python environment first. Run ./start-macos.sh."
  exit 1
fi
SERVER_URL="${ODYSSEUS_SERVER_URL:-}"
if [ -n "$SERVER_URL" ]; then
  SERVER_URL="$("$VENV_PY" - "$SERVER_URL" <<'PY'
import ipaddress
import re
import sys
from urllib.parse import urlsplit, urlunsplit

try:
    raw = sys.argv[1]
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("whitespace and control characters are not allowed")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError("use an absolute http:// or https:// server URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("credentials must not be included in the URL")
    if "?" in raw or "#" in raw or parsed.path not in {"", "/"}:
        raise ValueError("use the server origin without a path, query, or fragment")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    if ":" in host:
        if not re.fullmatch(r"\[[^\]]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("invalid IPv6 server authority")
        host = "[" + str(ipaddress.IPv6Address(host)) + "]"
    elif len(host) > 253 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.rstrip(".").split(".")
    ):
        raise ValueError("invalid server hostname")
    port = parsed.port
    if port is not None:
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        host += f":{port}"
    elif parsed.netloc.endswith(":"):
        raise ValueError("port is empty")
    print(urlunsplit((parsed.scheme, host, "", "", "")))
except (ValueError, UnicodeError) as exc:
    raise SystemExit(f"Invalid ODYSSEUS_SERVER_URL: {exc}")
PY
)" || exit 2
fi
if [ "${1:-}" = "--check-server-url" ]; then
  [ -n "$SERVER_URL" ] || { echo "Set ODYSSEUS_SERVER_URL first." >&2; exit 2; }
  printf '%s\n' "$SERVER_URL"
  exit 0
fi
"$VENV_PY" "$REPO_DIR/src/python_runtime.py"
if [ "${1:-}" = "--check-python" ]; then exit 0; fi

echo "Building $APP_NAME.app"
echo "  install dir: $INSTALL_DIR"
echo "  port:        $PORT"
[ -z "$SERVER_URL" ] || echo "  server:      $SERVER_URL"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── Icon (best effort) — center-crop the branding image to a square .icns ──
if [ -f "$REPO_DIR/assets/branding/odysseus.jpg" ] && command -v sips >/dev/null 2>&1; then
  TMPIMG="$(mktemp -d)"
  # Center-crop to a square, scale to 512 (sips' icns encoder caps at 512), and
  # let sips emit the .icns directly — more robust across macOS versions than
  # building an .iconset by hand.
  sips -c 720 720 "$REPO_DIR/assets/branding/odysseus.jpg" --out "$TMPIMG/sq.png" >/dev/null 2>&1 || cp "$REPO_DIR/assets/branding/odysseus.jpg" "$TMPIMG/sq.png"
  sips -z 512 512 "$TMPIMG/sq.png" --out "$TMPIMG/icon.png" >/dev/null 2>&1
  if sips -s format icns "$TMPIMG/icon.png" --out "$APP/Contents/Resources/odysseus.icns" >/dev/null 2>&1; then
    echo "  icon:        odysseus.icns"
  else
    echo "  icon:        (skipped — conversion failed)"
  fi
  rm -rf "$TMPIMG"
else
  echo "  icon:        (skipped — no assets/branding/odysseus.jpg)"
fi

# ── Info.plist ──
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$APP_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$APP_NAME</string>
    <key>CFBundleIdentifier</key>      <string>com.odysseus.launcher</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>$APP_NAME</string>
    <key>CFBundleIconFile</key>        <string>odysseus</string>
    <key>LSMinimumSystemVersion</key>  <string>11.0</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>LSUIElement</key>             <false/>
</dict>
</plist>
PLIST

# ── Launcher executable (placeholders filled below) ──
cat > "$APP/Contents/MacOS/$APP_NAME.tmpl" <<'LAUNCHER'
#!/bin/bash
# Odysseus.app — start the local server and open the UI in an app window.
INSTALL_DIR=__INSTALL_DIR__
PORT=__PORT__
SERVER_URL=__SERVER_URL__
URL="${SERVER_URL:-http://127.0.0.1:${PORT}}"

# Open the UI in a chrome-less app window (Chromium browsers), else default browser.
open_ui() {
  local b base exe bin
  for b in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium"; do
    for base in "/Applications" "$HOME/Applications"; do
      if [ -d "$base/$b.app" ]; then
        exe="$(/usr/bin/defaults read "$base/$b.app/Contents/Info" CFBundleExecutable 2>/dev/null)"
        bin="$base/$b.app/Contents/MacOS/$exe"
        if [ -x "$bin" ]; then
          "$bin" --app="$URL" --new-window >/dev/null 2>&1 &
          return 0
        fi
      fi
    done
  done
  /usr/bin/open "$URL"
}

if [ -n "$SERVER_URL" ]; then
  open_ui
  exit $?
fi

# Local mode alone owns a Python server and its log directory.
export APP_PORT="$PORT"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
VENV_PY="$INSTALL_DIR/venv/bin/python"
LOG="$INSTALL_DIR/logs/odysseus-app.log"
notify() { /usr/bin/osascript -e "display notification \"$1\" with title \"Odysseus\"" >/dev/null 2>&1; }
die_gui() {
  /usr/bin/osascript -e "display dialog \"$1\" with title \"Odysseus\" buttons {\"OK\"} default button 1 with icon stop" >/dev/null 2>&1
  exit 1
}
[ -x "$VENV_PY" ] || die_gui "Odysseus isn't set up yet. Open Terminal and run:

cd $INSTALL_DIR
./start-macos.sh"
RUNTIME_STATUS="$("$VENV_PY" "$INSTALL_DIR/src/python_runtime.py" 2>&1)" || die_gui "$RUNTIME_STATUS"

mkdir -p "$INSTALL_DIR/logs"

# Already running? Just open the UI.
if /usr/bin/curl -s -o /dev/null --max-time 2 "$URL"; then
  open_ui
  exit 0
fi

notify "Starting…"
cd "$INSTALL_DIR" || die_gui "Install folder not found: $INSTALL_DIR"
if [ "$(uname -m)" = "arm64" ]; then
  arch -arm64 "$VENV_PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
else
  "$VENV_PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
fi
SERVER_PID=$!

# Quitting the app stops the server it started.
trap 'kill $SERVER_PID 2>/dev/null; exit 0' TERM INT

# Wait for readiness (first run downloads an embedding model — allow ~2 min).
READY=0
for i in $(seq 1 120); do
  /usr/bin/curl -s -o /dev/null --max-time 2 "$URL" && { READY=1; break; }
  kill -0 "$SERVER_PID" 2>/dev/null || die_gui "Odysseus failed to start. Log:
$LOG"
  sleep 1
done

if [ "$READY" = "1" ]; then
  open_ui
else
  notify "Odysseus is taking a while — open $URL once it finishes starting."
fi
wait "$SERVER_PID"
LAUNCHER

"$VENV_PY" - "$APP/Contents/MacOS/$APP_NAME" "$INSTALL_DIR" "$PORT" "$SERVER_URL" <<'PY'
from pathlib import Path
import shlex
import sys

target = Path(sys.argv[1])
template = target.with_suffix(".tmpl").read_text(encoding="utf-8")
for token, value in zip(("__INSTALL_DIR__", "__PORT__", "__SERVER_URL__"), sys.argv[2:]):
    template = template.replace(token, shlex.quote(value))
target.write_text(template, encoding="utf-8")
PY
rm -f "$APP/Contents/MacOS/$APP_NAME.tmpl"
chmod +x "$APP/Contents/MacOS/$APP_NAME"

# Refresh Finder's icon cache for the new bundle.
touch "$APP"

# ── .dmg (drag-to-Applications) ──
echo "Packaging dist/$APP_NAME.dmg"
STAGE="$(mktemp -d)/dmg"
mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DIST/$APP_NAME.dmg"
hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DIST/$APP_NAME.dmg" >/dev/null
rm -rf "$STAGE"

echo ""
echo "Done:"
echo "  $APP"
echo "  $DIST/$APP_NAME.dmg"
echo ""
echo "Run it:        open '$APP'"
echo "Install:       open '$DIST/$APP_NAME.dmg'  (drag Odysseus to Applications)"
