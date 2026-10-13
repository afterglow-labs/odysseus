"""Small, disposable phone-compatible previews of allowlisted server videos."""
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
import time
import uuid

from fastapi import HTTPException

from src.constants import DATA_DIR
from src.video_server_files import ServerVideoUpload, open_server_video_preview

CACHE_ROOT = Path(DATA_DIR) / 'video_previews'
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_AGE_SECONDS = 3600
_ENCODERS = threading.BoundedSemaphore(2)
_CACHE_LOCK = threading.Lock()


class _CachedPreviewUpload(ServerVideoUpload):
    def check_unchanged(self):
        # Pruning can unlink an immutable cache entry while its open handle is
        # streaming. That changes ctime, but not the bytes held by this handle.
        info = os.fstat(self.file.fileno())
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if signature != self._source_signature[:4]:
            raise HTTPException(409, 'The video preview changed. Reload the preview.')


def open_server_video_proxy(selection, *, start_seconds=0, duration_seconds=30):
    """Hold the cache inode before responding so eviction cannot break a seek."""
    for _ in range(2):
        path = get_server_video_preview(selection, start_seconds=start_seconds, duration_seconds=duration_seconds)
        with _CACHE_LOCK:
            try:
                descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            except FileNotFoundError:
                # Another request evicted this entry between generation/cache
                # lookup and acquisition. Revalidate the source and retry once.
                continue
            try:
                stream = os.fdopen(descriptor, 'rb')
            except BaseException:
                os.close(descriptor)
                raise
            try:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size < 512:
                    raise HTTPException(422, 'This video preview is unavailable. Reload the preview.')
                result = _CachedPreviewUpload(stream, filename='preview.mp4', info=info)
                stream = None
                return result
            finally:
                if stream is not None:
                    stream.close()
    raise HTTPException(503, 'This video preview expired while loading. Retry the preview.')


def _prune(directory, keep=None):
    with _CACHE_LOCK:
        files = []
        for path in directory.glob('*.mp4'):
            if path.is_symlink() or len(path.stem) != 64:
                continue
            try:
                files.append((path, path.stat()))
            except FileNotFoundError:
                continue
        files.sort(key=lambda entry: entry[1].st_mtime, reverse=True)
        total = 0
        for path, info in files:
            try:
                total += info.st_size
                if path != keep and (time.time() - info.st_mtime > MAX_AGE_SECONDS or total > MAX_CACHE_BYTES):
                    path.unlink(missing_ok=True)
            except FileNotFoundError:
                pass


def get_server_video_preview(selection, *, start_seconds=0, duration_seconds=30):
    """Return a seekable H.264/AAC MP4 window; time zero is the requested start.

    Only a maximum of 30 seconds is encoded, on CPU. The validated source file
    handle stays open through FFmpeg and is checked again before publishing.
    No preview bytes or transformations are used by generation jobs.
    """
    if (type(start_seconds) not in (int, float) or not math.isfinite(start_seconds) or start_seconds < 0
            or type(duration_seconds) not in (int, float) or not math.isfinite(duration_seconds)
            or not .25 <= duration_seconds <= 30):
        raise HTTPException(400, 'Choose a preview start at zero or later and a length up to 30 seconds')
    upload = open_server_video_preview(selection)
    acquired = False
    temporary = None
    try:
        info = os.fstat(upload.file.fileno())
        identity = [2, selection, info.st_dev, info.st_ino, info.st_ctime_ns,
                    float(start_seconds), float(duration_seconds)]
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        CACHE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = CACHE_ROOT / (key + '.mp4')
        if target.is_file() and not target.is_symlink():
            upload.check_unchanged()
            return target
        acquired = _ENCODERS.acquire(timeout=5)
        if not acquired:
            raise HTTPException(429, 'Other previews are being prepared. Try again in a moment.', headers={'Retry-After': '3'})
        if target.is_file() and not target.is_symlink():
            upload.check_unchanged()
            return target
        from src.video_media_metadata import get_open_video_metadata
        metadata = get_open_video_metadata(upload, selection)
        source_duration = metadata.get('duration_seconds')
        if source_duration is not None and start_seconds >= source_duration:
            raise HTTPException(400, 'The preview start is past the end of this video')
        offset = start_seconds + metadata.get('video_start_offset_seconds', 0)
        executable = shutil.which('ffmpeg')
        if executable is None:
            import imageio_ffmpeg
            executable = imageio_ffmpeg.get_ffmpeg_exe()
        temporary = CACHE_ROOT / (key + '.' + uuid.uuid4().hex + '.partial.mp4')
        input_path = f'/proc/self/fd/{upload.file.fileno()}' if os.name == 'posix' and Path('/proc/self/fd').is_dir() else 'pipe:0'
        command = [executable, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                   '-protocol_whitelist', 'file,pipe', '-threads', '2', '-filter_threads', '2', '-ss', str(offset), '-i', input_path,
                   '-t', str(duration_seconds), '-map', '0:v:0', '-map', '0:a:0?',
                   '-vf', 'scale=640:360:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1,fps=24',
                   '-af', 'aresample=48000:async=1:first_pts=0', '-c:v', 'libx264', '-preset', 'ultrafast',
                   '-crf', '27', '-maxrate', '2M', '-bufsize', '4M', '-pix_fmt', 'yuv420p',
                   '-profile:v', 'baseline', '-level', '3.0', '-threads', '2',
                   '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000',
                   '-map_metadata', '-1', '-movflags', '+faststart', str(temporary)]
        kwargs = {'pass_fds': (upload.file.fileno(),)} if input_path.startswith('/proc/') else {'stdin': upload.file}
        try:
            result = subprocess.run(command, capture_output=True, timeout=90, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise HTTPException(504, 'Preparing this preview took too long. Choose another section or retry.') from exc
        except OSError as exc:
            raise HTTPException(503, 'The video preview converter is unavailable') from exc
        upload.check_unchanged()
        if result.returncode or not temporary.is_file() or temporary.stat().st_size < 512:
            raise HTTPException(422, 'This section could not be previewed. The source may be unreadable or have no video here.')
        os.replace(temporary, target)
        temporary = None
        _prune(CACHE_ROOT, keep=target)
        return target
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        upload.file.close()
        if acquired:
            _ENCODERS.release()
