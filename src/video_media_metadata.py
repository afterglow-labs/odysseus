"""Bounded metadata for an already-authorized, immutable server video handle.

Metadata comes from container headers, not a complete packet/frame scan. This
is intended for draft controls and preview compatibility, rather than replacing
the exact frame accounting performed when submitting a rendering job.
"""
from collections import OrderedDict
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from fastapi import HTTPException

from src.video_server_files import open_server_video_preview

PROBE_TIMEOUT = 8
PROBE_READ_LIMIT = 8 * 1024 * 1024
CACHE_TTL = 300
CACHE_LIMIT = 512
_CACHE = OrderedDict()
_LOCK = threading.Lock()
_PROBES = threading.BoundedSemaphore(3)

MIME_TYPES = {'.mp4': 'video/mp4', '.m4v': 'video/mp4', '.mov': 'video/quicktime',
              '.webm': 'video/webm', '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo'}


class _BudgetReader(io.RawIOBase):
    """Allow header seeks while preventing demuxers from reading a whole movie."""
    def __init__(self, stream, limit=PROBE_READ_LIMIT):
        self.stream, self.remaining = stream, limit

    def readable(self):
        return True

    def seekable(self):
        return True

    def read(self, size=-1):
        if self.remaining <= 0:
            raise OSError('Video metadata exceeded the bounded header read')
        size = min(size if size >= 0 else 32768, self.remaining)
        data = self.stream.read(size)
        self.remaining -= len(data)
        return data

    def seek(self, offset, whence=os.SEEK_SET):
        return self.stream.seek(offset, whence)

    def tell(self):
        return self.stream.tell()


def _positive(value):
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _probe_stream(stream, suffix):
    import av

    with av.open(_BudgetReader(stream), options={'protocol_whitelist': 'file,pipe',
                 'probesize': '262144', 'analyzeduration': '1000000', 'max_probe_packets': '256'}) as source:
        if not source.streams.video:
            raise ValueError('The file contains no video track')
        video = source.streams.video[0]
        video_start = float(video.start_time * video.time_base) if video.start_time is not None and video.time_base else 0.
        file_start = float(source.start_time / av.time_base) if source.start_time is not None else 0.
        start_offset = max(0., video_start - file_start)
        duration = float(video.duration * video.time_base) if video.duration is not None and video.time_base else None
        estimated = False
        if not _positive(duration) and video.frames and video.average_rate:
            duration = float(video.frames / video.average_rate)
            estimated = True
        if not _positive(duration) and source.duration:
            duration = float(source.duration / av.time_base) - start_offset
            estimated = True
        if not _positive(duration):
            raise ValueError('The video track has no readable duration')
        audio = source.streams.audio[0] if source.streams.audio else None
        video_codec = video.codec_context.name
        audio_codec = audio.codec_context.name if audio else None
        # Container matters: an H.264 stream inside MKV still will not play in
        # an iOS AVPlayer. Unknown codecs remain unknown instead of claiming
        # that every browser can decode them.
        mp4 = suffix in {'.mp4', '.m4v', '.mov'}
        apple_audio = audio_codec in {None, 'aac'}
        ios_compatible = mp4 and video_codec in {'h264', 'hevc'} and apple_audio
        browser_compatible = (True if mp4 and video_codec == 'h264' and audio_codec in {None, 'aac', 'mp3'}
                              else True if suffix == '.webm' and video_codec in {'vp8', 'vp9', 'av1'} and audio_codec in {None, 'opus', 'vorbis'}
                              else False if suffix in {'.mkv', '.avi'} or not ios_compatible else None)
        return {'duration_seconds': duration, 'duration_estimated': estimated,
                'video_start_offset_seconds': start_offset,
                'video_codec': video_codec, 'audio_codec': audio_codec,
                'width': video.codec_context.width, 'height': video.codec_context.height,
                'mime_type': MIME_TYPES[suffix], 'browser_compatible': browser_compatible,
                'ios_compatible': ios_compatible}


def probe_open_video(upload):
    """Pass the secure handle as seekable stdin, never reopen a supplied path."""
    upload.check_unchanged()
    position = upload.file.tell()
    upload.file.seek(0)
    try:
        result = subprocess.run([sys.executable, '-m', 'src.video_media_metadata', '--probe-stdin',
                                 Path(upload.filename).suffix.lower()],
                                stdin=upload.file, capture_output=True, timeout=PROBE_TIMEOUT,
                                cwd=Path(__file__).resolve().parent.parent, check=False)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, 'Video metadata took too long to read. Retry or choose a different file.') from exc
    except OSError as exc:
        raise HTTPException(503, 'The video metadata reader is unavailable.') from exc
    finally:
        upload.file.seek(position)
    upload.check_unchanged()
    try:
        data = json.loads(result.stdout)
        if (result.returncode or not isinstance(data, dict) or not _positive(data.get('duration_seconds'))
                or type(data.get('width')) is not int or type(data.get('height')) is not int
                or data['width'] <= 0 or data['height'] <= 0):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, 'Could not read this video’s duration and codec information.') from exc
    return data


def get_open_video_metadata(upload, selection):
    """Cached bounded metadata for a caller-owned validated video handle."""
    upload.check_unchanged()
    key = (selection['root'], selection['path'], *upload._source_signature)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            _CACHE.move_to_end(key)
            upload.check_unchanged()
            return dict(cached[1])
    if not _PROBES.acquire(timeout=1):
        raise HTTPException(429, 'Video metadata is busy. Retry in a moment.', headers={'Retry-After': '1'})
    try:
        data = probe_open_video(upload)
    finally:
        _PROBES.release()
    with _LOCK:
        _CACHE[key] = (time.monotonic() + CACHE_TTL, dict(data))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_LIMIT:
            _CACHE.popitem(last=False)
    return data


def get_server_video_metadata(selection):
    """Synchronous API helper; route must authenticate before running in a thread."""
    upload = open_server_video_preview(selection)
    try:
        return get_open_video_metadata(upload, selection)
    finally:
        upload.file.close()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--probe-stdin' or sys.argv[2] not in MIME_TYPES:
        raise SystemExit(2)
    try:
        # stdin is a regular inherited file handle on Linux and Windows. The
        # duplicate retains seek support and closes independently of Python IO.
        with os.fdopen(os.dup(sys.stdin.fileno()), 'rb') as source:
            print(json.dumps(_probe_stream(source, sys.argv[2])))
    except Exception:
        raise SystemExit(1)
