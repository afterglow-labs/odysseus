"""Portable receipts for readable Hugging Face downloads, also used over SSH.

Receipts belong to this destination and selected file pattern. An older copy
in the global Hub cache can never make an interrupted directory download look
complete. Hugging Face retains its own small download metadata for resumption.
"""

import fnmatch
import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.model_library import write_model_metadata


def _directory_receipt_path(directory, pattern):
    key = hashlib.sha256((pattern or "").encode()).hexdigest()
    return Path(directory) / ".cache" / "odysseus" / "downloads" / (key + ".json")


def _write_directory_receipt(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".download-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare_directory_download(repo_id, directory, pattern, expected_files=None):
    """Record identity and invalidate a prior success before a new transfer."""
    directory = Path(os.path.expanduser(directory)).absolute()
    write_model_metadata(directory, repo_id)
    receipt = {
        "repository": repo_id, "pattern": pattern or "", "status": "pending",
    }
    if expected_files is not None:
        receipt["expected_files"] = expected_files
    _write_directory_receipt(_directory_receipt_path(directory, pattern), receipt)


def directory_download_files(directory, pattern=""):
    """Return materialized files, excluding internal metadata and partials."""
    directory = Path(os.path.expanduser(directory)).absolute()
    files = {}
    for base, dirs, names in os.walk(directory):
        relative_directory = Path(base).relative_to(directory).as_posix()
        if relative_directory == ".":
            dirs[:] = [name for name in dirs if name != ".git"]
        elif relative_directory == ".cache":
            dirs[:] = [name for name in dirs if name not in {"huggingface", "odysseus"}]
        for name in names:
            if (relative_directory == "." and name.startswith(".odysseus-model")) or name.endswith(".incomplete"):
                continue
            path = Path(base) / name
            relative = path.relative_to(directory).as_posix()
            if pattern and not fnmatch.fnmatchcase(relative, pattern):
                continue
            try:
                size = path.stat().st_size
                if size >= 0:
                    files[relative] = size
            except OSError:
                pass
    return files


def finish_directory_download(repo_id, directory, pattern):
    directory = Path(os.path.expanduser(directory)).absolute()
    files = directory_download_files(directory, pattern)
    try:
        receipt = json.loads(_directory_receipt_path(directory, pattern).read_text(encoding="utf-8"))
        expected = receipt.get("expected_files")
    except (OSError, ValueError, TypeError):
        expected = None
    if isinstance(expected, dict):
        # Validate this revision's selected files, not an older file that
        # happens to match the glob in the same directory.
        if not expected:
            files = {}
        elif any(name not in files or (size is not None and files[name] != size)
                 for name, size in expected.items()):
            print("DOWNLOAD_INCOMPLETE: Selected files are missing or have unexpected sizes in the download folder.", flush=True)
            return 1
        else:
            files = {name: files[name] for name in expected}
    if not files:
        print("DOWNLOAD_EMPTY: No matching files were downloaded. Check the filename/quant pattern for this repository.", flush=True)
        return 1
    write_model_metadata(directory, repo_id)
    _write_directory_receipt(_directory_receipt_path(directory, pattern), {
        "repository": repo_id, "pattern": pattern or "", "status": "complete", "files": files,
    })
    return 0


def directory_download_complete(repo_id, directory, pattern=""):
    directory = Path(os.path.expanduser(directory)).absolute()
    try:
        receipt = json.loads(_directory_receipt_path(directory, pattern).read_text(encoding="utf-8"))
        if receipt.get("repository") != repo_id or receipt.get("status") != "complete":
            return False
        recorded = receipt.get("files")
        if not isinstance(recorded, dict) or not recorded:
            return False
        files = directory_download_files(directory, pattern)
        return all(files.get(name) == size for name, size in recorded.items())
    except (OSError, ValueError, TypeError):
        return False


def directory_download_incomplete(repo_id, directory, pattern=""):
    directory = Path(os.path.expanduser(directory)).absolute()
    try:
        receipt = json.loads(_directory_receipt_path(directory, pattern).read_text(encoding="utf-8"))
        if receipt.get("repository") == repo_id and receipt.get("status") == "pending":
            return True
    except (OSError, ValueError, TypeError):
        pass
    download = directory / ".cache" / "huggingface" / "download"
    return download.is_dir() and any(download.rglob("*.incomplete"))
