"""Temporary, owner-bound local video uploads used only for compatible playback."""
import asyncio
from contextlib import asynccontextmanager
import errno
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from fastapi import HTTPException

from src.constants import DATA_DIR
from src.h3_video import _locked, _read, _write
from src.video_server_files import ServerVideoUpload
from src.video_submission import complete_operation, ensure_upload_space, store_upload_chunk

ROOT = Path(DATA_DIR) / 'video_jobs' / '.local_previews'
TTL_SECONDS = 2 * 60 * 60
SWEEP_SECONDS = 5 * 60
IDENTITY = re.compile(r'[0-9a-f]{32}\Z')
EXTENSIONS = {'.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi'}
logger = logging.getLogger(__name__)


class LocalVideoPreviewStore:
    def __init__(self, root=ROOT):
        self.root = Path(root)

    def _prepare(self):
        if self.root.is_symlink():
            raise HTTPException(503, 'Temporary video storage is unavailable')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)

    def _directory(self, identity):
        if not isinstance(identity, str) or not IDENTITY.fullmatch(identity):
            raise HTTPException(404, 'This temporary video preview is unavailable')
        path = self.root / identity
        if self.root.is_symlink() or path.is_symlink():
            raise HTTPException(404, 'This temporary video preview is unavailable')
        return path

    def _lease(self, identity, *, blocking=True):
        self._prepare()
        # Locks survive session deletion. A bounded striped set also prevents
        # cleanup and a late opener from locking two different unlinked inodes.
        return _locked(self.root / ('lock-%02d' % (int(identity[:2], 16) % 64)), blocking=blocking)

    def _remove(self, directory):
        from src.video_preview_proxy import delete_local_previews
        delete_local_previews(directory.name)
        shutil.rmtree(directory)

    def _manifest(self, directory, owner, *, ready=True):
        if not directory.is_dir() or directory.is_symlink():
            raise HTTPException(404, 'This temporary video preview is unavailable')
        path = directory / 'manifest.json'
        if path.is_symlink():
            raise HTTPException(404, 'This temporary video preview is unavailable')
        manifest = _read(path)
        if manifest.get('owner') != owner:
            raise HTTPException(404, 'This temporary video preview is unavailable')
        if time.time() >= manifest.get('expires_at', 0):
            self._remove(directory)
            raise HTTPException(410, 'This temporary preview expired. Open the video again.')
        if ready and manifest.get('status') != 'ready':
            raise HTTPException(409, 'This temporary video is still uploading')
        return manifest

    @asynccontextmanager
    async def upload(self, owner, upload, limit):
        suffix = Path(str(upload.filename or '')).suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(400, 'Choose an MP4, MOV, WebM, MKV, AVI or M4V video')
        if limit is not None and type(upload.size) is int and upload.size > limit:
            raise HTTPException(413, 'The video exceeds the configured video upload limit')
        await complete_operation(self.prune)
        self._prepare()
        identity = uuid.uuid4().hex
        directory = self._directory(identity)
        lease = None
        finished = False
        try:
            while True:
                candidate = self._lease(identity, blocking=False)
                if candidate.__enter__() is not None:
                    lease = candidate
                    break
                candidate.__exit__(None, None, None)
                await asyncio.sleep(.05)
            directory.mkdir(mode=0o700)
            now = time.time()
            manifest = {'id': identity, 'owner': owner, 'created_at': now, 'expires_at': now + TTL_SECONDS,
                        'status': 'uploading', 'filename': 'source' + suffix}
            _write(directory / 'manifest.json', manifest)
            known = upload.size if type(upload.size) is int and upload.size > 0 else 0
            await complete_operation(ensure_upload_space, directory, known)
            path = directory / manifest['filename']
            total = 0
            with path.open('xb') as stream:
                path.chmod(0o600)
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if limit is not None and total > limit:
                        raise HTTPException(413, 'The video exceeds the configured video upload limit')
                    await store_upload_chunk(stream, directory, chunk)
            if not total:
                raise HTTPException(400, 'The selected video is empty')
            metadata = await complete_operation(self._probe, directory, manifest)
            manifest.update(status='ready', metadata=metadata, expires_at=time.time() + TTL_SECONDS)
            _write(directory / 'manifest.json', manifest)
            yield {'id': identity, 'metadata': metadata, 'expires_at': manifest['expires_at'],
                   'preview_url': '/api/video/h3/local-previews/' + identity + '/video'}
            finished = True
        finally:
            try:
                if not finished and directory.exists():
                    self._remove(directory)
            finally:
                if lease is not None:
                    lease.__exit__(None, None, None)

    def _open(self, directory, manifest):
        filename = manifest.get('filename', '')
        if filename not in {'source' + ext for ext in EXTENSIONS}:
            raise HTTPException(404, 'This temporary video preview is unavailable')
        try:
            descriptor = os.open(directory / filename, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                                 | getattr(os, 'O_NONBLOCK', 0))
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP}:
                raise HTTPException(404, 'This temporary video preview is unavailable') from exc
            raise
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise HTTPException(404, 'This temporary video preview is unavailable')
            stream = os.fdopen(descriptor, 'rb', buffering=0)
        except BaseException:
            os.close(descriptor)
            raise
        return ServerVideoUpload(stream, filename=filename, info=info)

    def _probe(self, directory, manifest):
        from src.video_media_metadata import probe_open_video
        source = self._open(directory, manifest)
        try:
            header = source.file.read(12)
            source.file.seek(0)
            # Never let a renamed playlist/concat text file ask a demuxer to
            # open other files on the server. These are standalone containers.
            if not (header.startswith(b'\x1a\x45\xdf\xa3')
                    or header[:4] == b'RIFF' and header[8:12] == b'AVI '
                    or header[4:8] in {b'ftyp', b'moov', b'mdat', b'wide', b'free', b'skip'}):
                raise HTTPException(422, 'This file is not a supported standalone video container')
            return probe_open_video(source)
        finally:
            source.file.close()

    def open(self, identity, owner):
        directory = self._directory(identity)
        with self._lease(identity):
            return self._open(directory, self._manifest(directory, owner))

    def check(self, identity, owner):
        directory = self._directory(identity)
        with self._lease(identity):
            self._manifest(directory, owner)

    def delete(self, identity, owner):
        directory = self._directory(identity)
        with self._lease(identity):
            self._manifest(directory, owner)
            self._remove(directory)
        return {'deleted': True}

    def prune(self):
        if not self.root.is_dir() or self.root.is_symlink():
            return 0
        removed = 0
        for directory in self.root.iterdir():
            if not IDENTITY.fullmatch(directory.name) or not directory.is_dir() or directory.is_symlink():
                continue
            with self._lease(directory.name, blocking=False) as lease:
                if lease is None or not directory.is_dir():
                    continue
                manifest = _read(directory / 'manifest.json')
                # A crashed staging request can precede its manifest write.
                expires = manifest.get('expires_at', directory.stat().st_mtime + TTL_SECONDS)
                if type(expires) in (int, float) and expires <= time.time():
                    self._remove(directory)
                    removed += 1
        return removed


async def local_preview_cleanup_loop():
    store = LocalVideoPreviewStore()
    while True:
        try:
            await complete_operation(store.prune)
        except Exception:
            logger.warning('Temporary video preview cleanup failed; it will retry')
        await asyncio.sleep(SWEEP_SECONDS)
