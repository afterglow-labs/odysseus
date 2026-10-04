#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo 'Usage: scripts/test.sh <simulator-UDID> [xcodebuild test options]' >&2
  exit 2
fi
simulator_id=$1
shift

# This port is shared by the integration and UI test targets. Do not attach to
# an unknown process, and never point these tests at a real Odysseus profile.
python3 - <<'PY'
import socket
with socket.socket() as listener:
    try:
        listener.bind(('127.0.0.1', 18766))
    except OSError:
        raise SystemExit('Port 18766 is in use. Stop your previous test fixture before running this script.')
PY
mkdir -p build
python3 scripts/fixture_server.py > build/fixture.log 2>&1 &
fixture_pid=$!
trap 'kill "$fixture_pid" 2>/dev/null || true' EXIT
python3 - <<'PY'
import time, urllib.request
for attempt in range(30):
    try:
        urllib.request.urlopen('http://127.0.0.1:18766/api/auth/status', timeout=1)
        break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit('The test fixture did not start. See build/fixture.log.')
PY
python3 scripts/generate_project.py
xcodebuild -project Odysseus.xcodeproj -scheme Odysseus \
  -configuration Debug -destination "platform=iOS Simulator,id=$simulator_id" \
  -derivedDataPath build -resultBundlePath "build/tests-$(date +%Y%m%d-%H%M%S).xcresult" \
  -parallel-testing-enabled NO -collect-test-diagnostics never \
  CODE_SIGN_IDENTITY=- DEVELOPMENT_TEAM="${ODYSSEUS_DEVELOPMENT_TEAM:-}" "$@" test
