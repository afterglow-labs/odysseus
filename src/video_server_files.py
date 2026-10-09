"""Bounded, allowlisted server video selection for remote workflow clients.

Selections name files relative to an administrator-configured media root. They
become read-only uploads, so the ordinary job uploader makes a private snapshot
and retains its existing quota, validation, metadata and cleanup behavior.
"""
import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat

import anyio
from fastapi import HTTPException
from starlette.datastructures import Headers, UploadFile


VIDEO_EXTENSIONS = frozenset({'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'})
SELECTION_LIMIT = 65536
MAX_FILES = 17
MAX_DIRECTORY_ENTRIES = 100000
ROOT_ID = re.compile(r'[a-zA-Z0-9_-]{1,64}\Z')
STAMP = re.compile(r'0|[1-9][0-9]{0,19}\Z')


@dataclass(frozen=True)
class MediaRoot:
    id: str
    name: str
    path: Path


def media_roots():
    """ODYSSEUS_VIDEO_BROWSE_ROOTS is a JSON map or list of named root objects."""
    raw = os.environ.get('ODYSSEUS_VIDEO_BROWSE_ROOTS', '').strip()
    if not raw:
        return [MediaRoot('e', 'E:', Path('E:\\') if os.name == 'nt' else Path('/mnt/e'))]
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [{'id': key, 'name': 'E:' if key == 'e' else key, 'path': value}
                    for key, value in data.items()]
        if not isinstance(data, list) or not 1 <= len(data) <= 32:
            raise ValueError()
        roots, seen = [], set()
        for item in data:
            if not isinstance(item, dict) or set(item) - {'id', 'name', 'path'}:
                raise ValueError()
            identity, name, location = item.get('id'), item.get('name', item.get('id')), item.get('path')
            if not isinstance(identity, str) or not ROOT_ID.fullmatch(identity) or identity in seen:
                raise ValueError()
            if not isinstance(name, str) or not name or len(name) > 128 or not isinstance(location, str):
                raise ValueError()
            path = Path(location).expanduser()
            if not path.is_absolute():
                raise ValueError()
            seen.add(identity)
            roots.append(MediaRoot(identity, name, path))
        return roots
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(503, 'Server video folders are not configured correctly') from exc


def _relative(value, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > 4096 or '\x00' in value or '\\' in value or ':' in value:
        raise HTTPException(400, 'Choose a relative path inside the server video folder')
    if allow_empty and value == '':
        return ()
    parts = value.split('/')
    if not value or any(part in {'', '.', '..'} for part in parts):
        raise HTTPException(400, 'Choose a relative path inside the server video folder')
    return tuple(parts)


def _linked(path):
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def _available(root):
    try:
        # A bare mountpoint directory remains after WSL unmounts a Windows drive.
        # Do not silently browse that unrelated Linux directory instead.
        if os.name != 'nt' and len(root.path.parts) >= 3 and root.path.parts[1] == 'mnt':
            drive = root.path.parts[2]
            if re.fullmatch(r'[a-zA-Z]', drive) and not os.path.ismount(Path('/mnt') / drive):
                return False
        return root.path.is_dir() and not _linked(root.path)
    except OSError:
        return False


def _root(roots, identity):
    selected = next((root for root in roots if root.id == identity), None)
    if selected is None:
        raise HTTPException(400, 'Unknown server video folder')
    if not _available(selected):
        raise HTTPException(503, f'{selected.name} is unavailable on the Odysseus server. Mount or reconnect the drive, then refresh.')
    return selected


def _file_error(exc):
    if isinstance(exc, PermissionError):
        return HTTPException(403, 'The server cannot read this video folder or file')
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        return HTTPException(400, 'Links and paths outside the server video folder cannot be selected')
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, 'The server video file or folder no longer exists; refresh the browser')
    return HTTPException(503, 'The server video drive cannot be read; reconnect it and refresh')


def _posix_open(root, parts, *, directory=False):
    """Anchor traversal to directory handles; no symlink can escape the root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0)
    current = os.open(root.path, flags)
    try:
        for part in parts[:-1] if not directory else parts:
            child = os.open(part, flags, dir_fd=current)
            os.close(current)
            current = child
        if directory:
            answer, current = current, None
            return answer
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                       getattr(os, 'O_CLOEXEC', 0), dir_fd=current)
    finally:
        if current is not None:
            os.close(current)


def _windows_path(root, parts):
    path = root.path
    for part in parts:
        path = path / part
        if _linked(path):
            raise HTTPException(400, 'Links cannot be selected from server video folders')
    if not path.resolve().is_relative_to(root.path.resolve()):
        raise HTTPException(400, 'Choose a file inside the server video folder')
    return path


def _windows_final_path(file):
    """Validate the opened handle, not a path that can change before open()."""
    import ctypes
    from ctypes import wintypes
    import msvcrt
    function = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = function(msvcrt.get_osfhandle(file.fileno()), buffer, len(buffer), 0)
    if not length or length >= len(buffer):
        raise OSError('Cannot resolve opened file handle')
    value = buffer.value
    if value.startswith('\\\\?\\UNC\\'):
        value = '\\\\' + value[8:]
    elif value.startswith('\\\\?\\'):
        value = value[4:]
    return Path(value)


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def list_server_videos(*, root='e', path='', offset=0, limit=200, search=''):
    if type(offset) is not int or offset < 0 or offset > MAX_DIRECTORY_ENTRIES:
        raise HTTPException(400, 'Invalid server video page')
    if type(limit) is not int or not 1 <= limit <= 500:
        raise HTTPException(400, 'Choose a page size between 1 and 500')
    if not isinstance(search, str) or len(search) > 255:
        raise HTTPException(400, 'The server video search is too long')
    parts = _relative(path, allow_empty=True)
    roots = media_roots()
    selected = _root(roots, root)
    entries, handle = [], None
    try:
        if os.name == 'nt':
            directory = _windows_path(selected, parts)
        else:
            handle = _posix_open(selected, parts, directory=True)
            directory = handle
        with os.scandir(directory) as iterator:
            for count, entry in enumerate(iterator, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise HTTPException(413, 'This folder has too many entries; choose a smaller media folder')
                if search.casefold() not in entry.name.casefold() or entry.is_symlink():
                    continue
                try:
                    if os.name == 'nt' and _linked(Path(entry.path)):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if not is_directory and (not stat.S_ISREG(info.st_mode) or
                                             Path(entry.name).suffix.lower() not in VIDEO_EXTENSIONS):
                        continue
                    relative = '/'.join((*parts, entry.name))
                    value = {'name': entry.name, 'kind': 'directory' if is_directory else 'video',
                             'path': relative, 'size': 0 if is_directory else info.st_size,
                             'mtime_ns': str(info.st_mtime_ns)}
                    if not is_directory:
                        value['selection'] = {'root': root, 'path': relative, 'size': info.st_size,
                                              'mtime_ns': str(info.st_mtime_ns)}
                    entries.append(value)
                except (FileNotFoundError, PermissionError):
                    continue
    except OSError as exc:
        raise _file_error(exc) from exc
    finally:
        if handle is not None:
            os.close(handle)
    entries.sort(key=lambda item: (item['kind'] != 'directory', item['name'].casefold(), item['name']))
    end = offset + limit
    return {'roots': [{'id': item.id, 'name': item.name, 'available': _available(item)} for item in roots],
            'root': root, 'path': '/'.join(parts), 'parent_path': '/'.join(parts[:-1]) if parts else None,
            'entries': entries[offset:end], 'total': len(entries),
            'has_more': end < len(entries), 'next_offset': end if end < len(entries) else None}


class ServerVideoUpload(UploadFile):
    def __init__(self, file, *, filename, info):
        super().__init__(file, filename=filename, size=info.st_size,
                         headers=Headers({'content-type': 'application/octet-stream'}))
        self._source_signature = _signature(info)
        self._bytes_read = 0

    def _read(self, size):
        # Check in the same thread as the read. An edited source must not become
        # an accepted job with a mixture of old and new input bytes.
        if _signature(os.fstat(self.file.fileno())) != self._source_signature:
            raise HTTPException(409, 'A selected server video changed. Refresh and select it again.')
        data = self.file.read(size)
        self._bytes_read += len(data)
        if _signature(os.fstat(self.file.fileno())) != self._source_signature:
            raise HTTPException(409, 'A selected server video changed. Refresh and select it again.')
        if not data and self._bytes_read != self.size:
            raise HTTPException(409, 'A selected server video could not be copied completely. Select it again.')
        return data

    async def read(self, size=-1):
        from starlette.concurrency import run_in_threadpool
        try:
            return await run_in_threadpool(self._read, size)
        except OSError as exc:
            raise _file_error(exc) from exc


def _selection(value):
    if not isinstance(value, dict) or set(value) != {'root', 'path', 'size', 'mtime_ns', 'index'}:
        raise HTTPException(400, 'Invalid server video selection')
    if not isinstance(value['root'], str) or not ROOT_ID.fullmatch(value['root']):
        raise HTTPException(400, 'Invalid server video folder')
    if type(value['size']) is not int or not 0 < value['size'] < 2 ** 63:
        raise HTTPException(400, 'Choose a nonempty server video')
    if not isinstance(value['mtime_ns'], str) or not STAMP.fullmatch(value['mtime_ns']):
        raise HTTPException(400, 'Invalid server video modification time')
    if type(value['index']) is not int or not 0 <= value['index'] < MAX_FILES:
        raise HTTPException(400, 'Invalid server video position')
    parts = _relative(value['path'])
    if Path(parts[-1]).suffix.lower() not in VIDEO_EXTENSIONS:
        raise HTTPException(400, 'Only videos can be selected from server folders')
    return parts


def _open_selection(value, roots):
    parts = _selection(value)
    root = _root(roots, value['root'])
    file = None
    try:
        if os.name == 'nt':
            file = _windows_path(root, parts).open('rb')
            actual = _windows_final_path(file)
            if not actual.is_relative_to(root.path.resolve()):
                raise HTTPException(400, 'Choose a video inside the server video folder')
        else:
            handle = _posix_open(root, parts)
            try:
                file = os.fdopen(handle, 'rb')
            except BaseException:
                os.close(handle)
                raise
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise HTTPException(400, 'Choose a regular video file')
        if info.st_size != value['size'] or str(info.st_mtime_ns) != value['mtime_ns']:
            raise HTTPException(409, 'A selected server video changed. Refresh and select it again.')
        result = ServerVideoUpload(file, filename=parts[-1], info=info)
        file = None
        return result
    except OSError as exc:
        raise _file_error(exc) from exc
    finally:
        if file is not None:
            file.close()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate server video field')
        result[key] = value
    return result


def _parse_inputs(form, uploads, extensions):
    values = form.getlist('server_inputs')
    if not values:
        return {}
    if len(values) != 1 or not isinstance(values[0], str) or len(values[0].encode('utf-8')) > SELECTION_LIMIT:
        raise HTTPException(400, 'Server video selections exceed the metadata limit or were repeated')
    try:
        values = json.loads(values[0], object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise HTTPException(400, 'Invalid server video selections JSON') from exc
    if not isinstance(values, dict):
        raise HTTPException(400, 'Server video selections must map video fields to lists')
    total = sum(len(items) for items in uploads.values())
    for field, selections in values.items():
        suffixes = extensions.get(field)
        if not suffixes or not set(suffixes).issubset(VIDEO_EXTENSIONS):
            raise HTTPException(400, 'Server files can only be used in video attachment fields')
        if not isinstance(selections, list) or len(selections) > MAX_FILES:
            raise HTTPException(400, 'Too many server video selections')
        total += len(selections)
        indices = set()
        combined_count = len(uploads.get(field, [])) + len(selections)
        for selection in selections:
            parts = _selection(selection)
            if Path(parts[-1]).suffix.lower() not in suffixes:
                raise HTTPException(400, 'Unsupported file type for this video attachment field')
            index = selection['index']
            if index >= combined_count or index in indices:
                raise HTTPException(400, 'Server video positions must be unique and within the attachment list')
            indices.add(index)
    if total > MAX_FILES:
        raise HTTPException(400, 'Too many video workflow attachments')
    return values


def _acquire_inputs(selections, uploads):
    opened = []
    try:
        roots = media_roots()
        combined = {field: list(items) for field, items in uploads.items()}
        for field, values in selections.items():
            items = combined.setdefault(field, [])
            for selection in sorted(values, key=lambda item: item['index']):
                upload = _open_selection(selection, roots)
                opened.append(upload)
                items.insert(selection['index'], upload)
        return combined, opened
    except BaseException:
        for upload in opened:
            upload.file.close()
        raise


@asynccontextmanager
async def server_video_inputs(form, uploads, extensions):
    """Merge positional server selections into multipart inputs and own handles.

    Call only after authentication and accepted-submission lookup, and keep this
    context around validation and job-local copying. Server files never become
    worker paths; the normal uploader must still enforce byte/disk limits.
    """
    selections = _parse_inputs(form, uploads, extensions)
    if not selections:
        yield uploads
        return
    acquired, handles = None, []
    try:
        acquired = asyncio.create_task(asyncio.to_thread(_acquire_inputs, selections, uploads))
        try:
            combined, handles = await asyncio.shield(acquired)
        except asyncio.CancelledError:
            # A cancelled browser request must close handles opened by a late
            # filesystem thread before abandoning the submission.
            with anyio.CancelScope(shield=True), suppress(Exception):
                _, handles = await asyncio.shield(acquired)
            raise
        yield combined
    finally:
        for upload in handles:
            upload.file.close()
