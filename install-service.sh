#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/odysseus-ui.service"
VENV_PY="$SCRIPT_DIR/venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  echo "Error: create venv with the CPython version in .python-version and install requirements.lock first."
  exit 1
fi
"$VENV_PY" "$SCRIPT_DIR/src/python_runtime.py"
if [ "${1:-}" = "--check-python" ]; then exit 0; fi

if [ ! -f "$SERVICE_FILE" ]; then
  echo "Error: odysseus-ui.service not found in $SCRIPT_DIR"
  exit 1
fi

echo "Installing Odysseus UI service..."
echo "Make sure you've edited odysseus-ui.service with your username and paths first!"
echo ""

sudo cp "$SERVICE_FILE" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable odysseus-ui
sudo systemctl start odysseus-ui
sudo systemctl status odysseus-ui
