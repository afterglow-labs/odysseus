#!/usr/bin/env python3
"""Regenerate or check the shared, hash-verified Python dependency locks.

Requires uv on PATH. Existing pins are preserved unless --upgrade is passed.
The optional and build locks are constrained by the core lock so extras cannot silently
replace the versions used to validate the application.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without changing tracked locks")
    parser.add_argument("--upgrade", action="store_true", help="Refresh all dependency versions")
    args = parser.parse_args()
    if args.check and args.upgrade:
        parser.error("--check and --upgrade cannot be combined")
    uv = shutil.which("uv")
    if not uv:
        parser.error("Install uv to regenerate locks; normal installs only require pip")
    version = (ROOT / ".python-version").read_text().strip()
    with tempfile.TemporaryDirectory(prefix="odysseus-locks-") as scratch:
        for source, lock in (
            ("requirements.txt", "requirements.lock"),
            ("requirements-optional.txt", "requirements-optional.lock"),
            ("requirements-build.txt", "requirements-build.lock"),
        ):
            committed = ROOT / lock
            output = Path(scratch) / lock if args.check else committed
            if args.check:
                if not committed.is_file():
                    print(f"Missing {lock}; run python scripts/lock_dependencies.py")
                    return 1
                shutil.copyfile(committed, output)
            command = [
                uv, "pip", "compile", source, "--universal", "--python-version", version,
                "--generate-hashes", "--no-strip-markers",
                "--custom-compile-command", "python scripts/lock_dependencies.py",
                "--output-file", str(output), "--quiet",
            ]
            if source != "requirements.txt":
                command += ["--constraint", "requirements.lock"]
            if args.upgrade:
                command.append("--upgrade")
            result = subprocess.run(command, cwd=ROOT)
            if result.returncode:
                return result.returncode
            # Git may check out text files as CRLF on Windows; line endings do
            # not change the resolved requirements or their artifact hashes.
            if args.check and output.read_text(encoding="utf-8") != committed.read_text(encoding="utf-8"):
                print(f"{lock} is stale; run python scripts/lock_dependencies.py")
                return 1
            print(f"{lock}: {'current' if args.check else 'updated'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
