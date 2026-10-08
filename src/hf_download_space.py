"""Portable, read-only space preflight, also embedded in remote HF runners."""

import inspect
import os
from pathlib import Path
import shutil


DISK_FULL_EXIT = 28
DOWNLOAD_RESERVE = 128 * 1024 * 1024


def check_download_space(repo_id, pattern, cache_dir):
    """Check only the selected, not-yet-cached files on the destination volume.

    Do not credit abandoned .incomplete files: current Hub releases download
    into a new process-unique file, rather than resuming those old files.
    Metadata/auth/network errors intentionally propagate to the runner.
    """
    from huggingface_hub import snapshot_download

    if "dry_run" not in inspect.signature(snapshot_download).parameters:
        print("[odysseus] Disk preflight unavailable in this huggingface_hub version; "
              "download errors will still be checked for a full disk.", flush=True)
        return 0
    cache = Path(os.path.expanduser(cache_dir)).absolute()
    preview = snapshot_download(
        repo_id=repo_id,
        cache_dir=str(cache),
        allow_patterns=[pattern] if pattern else None,
        dry_run=True,
    )
    required = 0
    unknown = False
    for entry in preview:
        if not entry.will_download:
            continue
        if entry.file_size is None:
            unknown = True
        else:
            required += entry.file_size
    # Do not create directories simply to identify their destination volume.
    volume = cache
    while not volume.exists() and volume != volume.parent:
        volume = volume.parent
    free = shutil.disk_usage(volume).free
    if required and free < required + DOWNLOAD_RESERVE:
        print(
            f"DOWNLOAD_NO_SPACE: {cache} needs {required / 1e9:.2f} GB "
            f"for the selected uncached files plus {DOWNLOAD_RESERVE / 1e6:.0f} MB "
            f"working space; only {free / 1e9:.2f} GB is free. "
            "Choose a download folder on a drive with enough space. "
            "Stopped without downloading or retrying.",
            flush=True,
        )
        return DISK_FULL_EXIT
    if unknown:
        print("[odysseus] Some selected file sizes are unknown; "
              "disk space could only be checked for files with known sizes.", flush=True)
    print(f"[odysseus] Download destination: {cache} "
          f"({required / 1e9:.2f} GB needed, {free / 1e9:.2f} GB free)", flush=True)
    return 0
