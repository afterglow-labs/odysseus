"""Durable, owner-scoped retry receipts for single video submissions."""
import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import hashlib
import errno
from pathlib import Path
import re
import shutil
import time
import uuid

import anyio

from src.h3_video import _locked, _read, _write

HEADER = "X-Odysseus-Submission-Id"
UUID = re.compile(r"(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})\Z")


class UploadSpaceError(OSError):
    pass


def known_input_bytes(uploads):
    """Preflight known sizes; streaming writers still enforce the actual total."""
    return sum(item.size for values in uploads.values() for item in values
               if type(item.size) is int and item.size > 0)


def ensure_upload_space(directory, size):
    directory = Path(directory)
    while not directory.exists() and directory != directory.parent:
        directory = directory.parent
    if shutil.disk_usage(directory).free < size + 512 * 1024 * 1024:
        raise UploadSpaceError("Not enough disk space for this video input; 512 MiB must remain free")


def write_upload_chunk(stream, directory, chunk):
    """Leave room for active renders and receipts while bulk uploads arrive."""
    ensure_upload_space(directory, len(chunk))
    try:
        stream.write(chunk)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise UploadSpaceError("The video upload ran out of disk space") from exc
        raise


async def complete_operation(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Finish the current write before closing its file, cleaning up staging,
        # or allowing another retry to enter the same submission.
        with anyio.CancelScope(shield=True), suppress(Exception):
            await asyncio.shield(task)
        raise


async def launch_job(manager, directory, owner, config, uploads):
    return await complete_operation(manager.launch, directory, owner, config, uploads)


async def store_upload_chunk(stream, directory, chunk):
    return await complete_operation(write_upload_chunk, stream, directory, chunk)


def submission_key(request):
    values = request.headers.getlist(HEADER)
    if not values:
        return None
    if len(values) != 1 or not UUID.fullmatch(values[0]):
        raise ValueError("X-Odysseus-Submission-Id must be a single UUID")
    return uuid.UUID(values[0]).hex


def input_metadata(uploads, family):
    def filename(upload):
        # Browsers can send Windows paths even to a Linux server. Store labels,
        # never client paths, and bound every label independently of body size.
        name = str(upload.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
        return "".join(c for c in name if c.isprintable())[:255] or "Unnamed input"
    names = {field: [filename(item) for item in files] for field, files in uploads.items() if files}
    sources = names.get("source_video") or names.get("reference_videos" if family == "h3" else "source_video", [])
    return {"input_names": names, "source_name": sources[-1] if sources else None}


class Submission:
    def __init__(self, manager, owner, family, key):
        self.manager, self.owner, self.key = manager, owner, key
        self.directory = None
        self.root = manager.root / ".submissions"
        self.job_id = hashlib.sha256(f"{family}\0{owner}\0{key}".encode()).hexdigest()[:32] if key else None
        self.receipt = self.root / f"{self.job_id}.json" if key else None

    def existing(self):
        if not self.key:
            return None
        directory = self.manager.directory(self.job_id)
        receipt = _read(self.receipt)
        if self.receipt.exists() and (receipt.get("owner") != self.owner or receipt.get("submission_id") != self.key):
            raise RuntimeError("The saved submission receipt is unavailable")
        if (directory / "state.json").exists():
            try:
                return self.manager.view(self.job_id, self.owner)
            except FileNotFoundError as exc:
                raise RuntimeError("The submitted video job is no longer available") from exc
        if receipt and not directory.exists():
            # Keep a small tombstone when the user deletes completed job files:
            # a late retry must not silently start the deleted render again.
            raise RuntimeError("This submission's job was deleted; submit again with a new submission ID")
        return None

    def stage(self):
        if self.key:
            directory = self.manager.directory(self.job_id)
            if directory.exists():
                if (directory / "state.json").exists():
                    raise RuntimeError("This video submission has already been accepted")
                if any(directory.iterdir()) and not self.receipt.exists():
                    raise RuntimeError("The video submission staging directory is unavailable")
                # A dead request can leave uploads but cannot have spawned a
                # worker without first publishing state.json. The retry lock
                # is still held while its abandoned staging is replaced.
                shutil.rmtree(directory)
        self.directory = self.manager.stage(self.job_id) if self.key else self.manager.stage()
        if self.key:
            _write(self.receipt, {"owner": self.owner, "submission_id": self.key,
                                  "id": self.job_id, "created_at": time.time()})
        return self.directory

    def save_metadata(self, uploads, family):
        value = input_metadata(uploads, family)
        value["submission_id"] = self.key
        _write(self.directory / "submission.json", value)

    def cleanup(self):
        if self.directory is not None and not (self.directory / "state.json").exists():
            shutil.rmtree(self.directory, ignore_errors=True)
            if self.receipt is not None and not self.directory.exists():
                self.receipt.unlink(missing_ok=True)


@asynccontextmanager
async def submission(manager, owner, family, key):
    value = Submission(manager, owner, family, key)
    lease = None
    try:
        if key:
            value.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            # A fixed set of lock files bounds lock metadata. Nonblocking flock
            # plus async waiting also works across processes without blocking
            # the event loop behind another request's upload.
            lock_path = value.root / f"lock-{int(value.job_id[:2], 16) % 64:02d}"
            while True:
                candidate = _locked(lock_path, blocking=False)
                if candidate.__enter__() is not None:
                    lease = candidate
                    break
                candidate.__exit__(None, None, None)
                await asyncio.sleep(.05)
        yield value
    finally:
        # Cleanup precedes releasing the retry lock: an old cancelled request
        # must never remove a fresh retry's staging directory.
        try:
            value.cleanup()
        finally:
            if lease is not None:
                lease.__exit__(None, None, None)
