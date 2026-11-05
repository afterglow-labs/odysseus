#!/usr/bin/env python3
"""Compatibility shortcuts for the independent APTEX server."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def read_env(path: Path) -> dict[str, str]:
    """Read the simple KEY=value entries used by both local Compose files."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw[1:-1]
        elif raw.startswith("'") and raw.endswith("'"):
            value = raw[1:-1]
        else:
            value = raw.split(" #", 1)[0].strip()
        values[key] = value
    return values



def backend_root() -> Path:
    settings = read_env(ROOT / ".env")
    configured = (os.getenv("APTEX_ROOT") or os.getenv("AFTERGLOW_COMPOSE_ROOT")
                  or settings.get("APTEX_ROOT") or settings.get("AFTERGLOW_COMPOSE_ROOT"))
    candidate = Path(configured).expanduser() if configured else ROOT.parent / "afterglow-security" / "afterglow-cybersecurity-gateway"
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = candidate.resolve(strict=True)
    for name in ("scripts/aptex.py", "docker-compose.yml"):
        if not (candidate / name).is_file():
            raise ValueError(f"APTEX checkout is missing {candidate / name}")
    return candidate


def main() -> int:
    default = "stop" if Path(sys.argv[0]).name.endswith("-stop") else "start"
    args = sys.argv[1:]
    action = args.pop(0) if args and args[0] in ("start", "stop", "client", "install") else default
    try:
        script = backend_root() / "scripts" / "aptex.py"
        os.execv(sys.executable, [sys.executable, str(script), action, *args])
    except (OSError, ValueError) as exc:
        print(f"APTEX shortcut: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
