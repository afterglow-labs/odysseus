"""Portable, read-only space preflight, also embedded in remote HF runners."""

import inspect
import os
from pathlib import Path
import shutil


DISK_FULL_EXIT = 28
DOWNLOAD_RESERVE = 128 * 1024 * 1024


def _preflight_progress_class():
    """Silence only the metadata check, without changing transfer progress.

    Hub creates byte bars even for ``dry_run=True`` and labels those empty
    bars "Download complete". A per-call class avoids mutating Hub's global
    progress state or the runner's explicit progress environment settings.
    """
    from tqdm.auto import tqdm

    class PreflightProgress(tqdm):
        def __init__(self, *args, **kwargs):
            kwargs.pop("name", None)
            kwargs["disable"] = True
            super().__init__(*args, **kwargs)

    return PreflightProgress


def check_download_space(repo_id, pattern, cache_dir, *, local_dir=None, preview_callback=None):
    """Check only the selected, not-yet-cached files on the destination volume.

    Do not credit abandoned .incomplete files: current Hub releases download
    into a new process-unique file, rather than resuming those old files.
    Metadata/auth/network errors intentionally propagate to the runner.
    """
    from huggingface_hub import snapshot_download

    supported = inspect.signature(snapshot_download).parameters
    if "dry_run" not in supported:
        print("[odysseus] Disk preflight unavailable in this huggingface_hub version; "
              "download errors will still be checked for a full disk.", flush=True)
        return 0
    cache = Path(os.path.expanduser(local_dir or cache_dir)).absolute()
    destination = {"local_dir": str(cache)} if local_dir else {"cache_dir": str(cache)}
    if local_dir:
        destination["ignore_patterns"] = [".odysseus-model*"]
    print(f"[odysseus] Checking selected files and free space in {cache}...", flush=True)
    if "tqdm_class" in supported:
        destination["tqdm_class"] = _preflight_progress_class()
    preview = snapshot_download(
        repo_id=repo_id,
        **destination,
        allow_patterns=[pattern] if pattern else None,
        dry_run=True,
    )
    required = 0
    unknown = False
    cached_copies = 0
    cached_copy_bytes = 0
    for entry in preview:
        if not entry.will_download:
            if not local_dir:
                continue
            # HF also reports will_download=False when it plans to COPY a
            # file from a separate blob cache. That copy still needs space in
            # this library, even though no network transfer is required.
            try:
                target = cache / entry.filename
                if target.is_file() and (entry.file_size is None or target.stat().st_size == entry.file_size):
                    continue
                if not target.exists():
                    cached_copies += 1
                    cached_copy_bytes += entry.file_size or 0
            except OSError:
                pass
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
    print("[odysseus] File and space check complete. Model files have not been transferred by this check.", flush=True)
    if cached_copies:
        print(f"[odysseus] {cached_copies} selected file(s) already exist in the Hub cache "
              f"({cached_copy_bytes / 1e9:.2f} GB with known sizes) and will be copied to this folder. "
              "Hugging Face does not report byte progress during cached-file copies.", flush=True)
    if preview_callback is not None:
        preview_callback(preview)
    return 0
