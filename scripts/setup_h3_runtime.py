#!/usr/bin/env python3
"""Install the pinned inference core into Odysseus's private environment.

Does not install a ComfyUI frontend, start a server, install custom nodes, or
download model weights. Existing dirty or different core checkouts are kept.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REVISION = "5c460d8172fe30761ff67c0df3d5643bb74e0d70"
REPOSITORY = "https://github.com/Comfy-Org/ComfyUI.git"
PROJECT = Path(__file__).resolve().parents[1]


def command(*args, cwd=None):
    return subprocess.run(list(args), cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def setup(runtime, install=True):
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Use Odysseus's private venv/bin/python to run setup; global Python is not modified")
    runtime = runtime.resolve()
    if runtime.exists() and any(runtime.iterdir()):
        if not (runtime / ".git").exists():
            raise RuntimeError(f"Runtime directory already contains non-Git files: {runtime}")
        revision = command("git", "rev-parse", "HEAD", cwd=runtime)
        changes = command("git", "status", "--porcelain", "--untracked-files=no", cwd=runtime)
        if revision != REVISION or changes:
            raise RuntimeError("Existing inference core differs from the pinned clean revision; keep it and choose another --runtime directory")
    else:
        runtime.mkdir(parents=True, exist_ok=True)
        command("git", "init", str(runtime))
        command("git", "remote", "add", "origin", REPOSITORY, cwd=runtime)
        command("git", "fetch", "--depth", "1", "origin", REVISION, cwd=runtime)
        command("git", "checkout", "--detach", "FETCH_HEAD", cwd=runtime)
    if install:
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(PROJECT / "requirements-h3.txt")], check=True)
    print(f"MiniMax H3 inference core ready: {runtime}\nRevision: {REVISION}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, default=PROJECT / "runtimes/minimax-h3/ComfyUI")
    parser.add_argument("--skip-dependencies", action="store_true")
    args = parser.parse_args()
    setup(args.runtime, not args.skip_dependencies)


if __name__ == "__main__":
    main()
