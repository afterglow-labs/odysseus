#!/usr/bin/env python3
"""Install the user-selected SeedScout source into Odysseus's private runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess


def install(source, destination):
    source, destination = Path(source).expanduser().resolve(), Path(destination).resolve()
    files = ("nodes.py", "taehv.py", "LICENSE", "README.md")
    if any(not (source / name).is_file() for name in files):
        raise ValueError("Choose a complete ComfyUI-H3-SeedScout checkout")
    revision = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    hashes = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
    receipt = destination / "odysseus-install.json"
    if destination.exists() and any(destination.iterdir()):
        old = json.loads(receipt.read_text()) if receipt.is_file() else {}
        if old.get("files") != hashes:
            raise ValueError("The private SeedScout runtime already has a different installation")
        for name, checksum in hashes.items():
            if hashlib.sha256((destination / name).read_bytes()).hexdigest() != checksum:
                raise ValueError("The private SeedScout source has local changes")
        return old
    destination.mkdir(parents=True, exist_ok=True)
    for name in files:
        shutil.copyfile(source / name, destination / name)
    value = {"repository": "https://github.com/tsuremen2/ComfyUI-H3-SeedScout", "revision": revision, "files": hashes}
    receipt.write_text(json.dumps(value, indent=2) + "\n")
    return value


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--destination", type=Path, default=Path(__file__).resolve().parents[1] / "runtimes/minimax-h3/seed-scout")
    args = parser.parse_args()
    result = install(args.source, args.destination)
    print("SeedScout installed at", args.destination, "revision", result["revision"])
