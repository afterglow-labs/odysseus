#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${ODYSSEUS_DEVELOPMENT_TEAM:?Set ODYSSEUS_DEVELOPMENT_TEAM to your Apple development team ID}"
mkdir -p build
python3 scripts/generate_project.py
xcodebuild -project Odysseus.xcodeproj -scheme Odysseus \
  -configuration Release -destination 'generic/platform=iOS' \
  -derivedDataPath device-build -archivePath build/Odysseus.xcarchive \
  -allowProvisioningUpdates DEVELOPMENT_TEAM="$ODYSSEUS_DEVELOPMENT_TEAM" archive
python3 - <<'PY'
import os, plistlib
from pathlib import Path
Path('build/ExportOptions.plist').write_bytes(plistlib.dumps({
    'method': 'debugging', 'signingStyle': 'automatic',
    'teamID': os.environ['ODYSSEUS_DEVELOPMENT_TEAM'],
    'destination': 'export', 'stripSwiftSymbols': True,
}))
PY
xcodebuild -exportArchive -archivePath build/Odysseus.xcarchive \
  -exportOptionsPlist build/ExportOptions.plist -exportPath build/export \
  -allowProvisioningUpdates
