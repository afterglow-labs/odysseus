#!/usr/bin/env bash
# Docker and native dependency installs share the same verified source patches.
# Usage: build-realesrgan-wheels.sh [OUTPUT_DIR]   (default: /wheels)
set -euo pipefail

OUT="${1:-/wheels}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILDER="$SCRIPT_DIR/build_realesrgan_wheels.py"
if [[ ! -f "$BUILDER" ]]; then
  BUILDER="$SCRIPT_DIR/../scripts/build_realesrgan_wheels.py"
fi
exec "${ODYSSEUS_PYTHON:-python}" "$BUILDER" --output-dir "$OUT"
