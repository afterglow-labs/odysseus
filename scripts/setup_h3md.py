#!/usr/bin/env python3
"""Install the pinned H3MD nodes and verified models in the private runtime."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

try:
    from scripts.h3md_controls import REVISION, SOURCE
except ModuleNotFoundError:
    from h3md_controls import REVISION, SOURCE


def install(project):
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Run with Odysseus venv/bin/python")
    from huggingface_hub import hf_hub_download
    destination = project / "runtimes/minimax-h3/h3md"
    def git(*args):
        return subprocess.check_output(["git", "-C", str(destination), *args], text=True).strip()
    if destination.exists():
        if git("rev-parse", "HEAD") != REVISION or git("status", "--porcelain", "--untracked-files=no"):
            raise RuntimeError("Existing H3MD differs from the pinned source; keeping it untouched")
    else:
        destination.mkdir(parents=True)
        git("init")
        git("remote", "add", "origin", SOURCE + ".git")
        git("fetch", "--depth", "1", "origin", REVISION)
        git("checkout", "--detach", "FETCH_HEAD")
    receipts = []
    for row in json.loads(Path(__file__).with_name("h3md_models.json").read_text()):
        print("Checking " + row["filename"], flush=True)
        path = Path(hf_hub_download(row["repo"], row["filename"], revision=row["revision"],
            cache_dir=project / "cache/huggingface/hub", local_dir=project / "models" / row["repo"]))
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise RuntimeError("H3MD model checksum mismatch: " + row["filename"])
        receipts.append(row)
    (destination.parent / "h3md-install.json").write_text(json.dumps({"revision": REVISION, "models": receipts}, indent=2) + "\n")
    print("H3MD code and model checks passed. The example's Civitai Enhancer is a separate optional download.")


if __name__ == "__main__":
    install(Path(__file__).resolve().parents[1])
