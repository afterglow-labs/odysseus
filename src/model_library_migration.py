"""Resumable migration from Hub caches to ordinary, named model directories.

Planning is read-only. Execution publishes only complete, SHA-256-verified files;
cache paths become compatibility links after publication. Files without a known
repository filename are never removed. Sources on filesystems without symlink
support are retained, and explicitly recorded in the journal.
"""
from __future__ import annotations

import errno
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time
import uuid

CHUNK_SIZE = 8 * 1024 * 1024
RESERVE_BYTES = 1024 ** 3
REPOSITORY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")


def _read(path):
    try:
        value = json.loads(Path(path).read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temporary.open("w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _relative(value):
    path = PurePosixPath(str(value))
    if (not value or path.is_absolute() or any(part in {"..", "."} for part in path.parts)
            or "\\" in str(value) or str(value).endswith(".incomplete")):
        raise ValueError(f"Unsafe repository filename: {value!r}")
    return path


def _regular_files(root):
    """Do not follow directory symlinks or traverse download temporary data."""
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [name for name in dirs if name not in {".locks", "xet", ".cache"}
                   and not (Path(directory) / name).is_symlink()]
        for name in files:
            path = Path(directory) / name
            if name.endswith((".incomplete", ".lock")):
                continue
            try:
                if path.is_file():
                    yield path
            except OSError:
                continue


def _existing_parent(path):
    path = Path(path)
    while not path.exists():
        if path == path.parent:
            raise FileNotFoundError(path)
        path = path.parent
    return path


def h3_file(repository, filename, extra_paths=()):
    """Keep complete H3 component repositories and the H3 part of mixed repos."""
    repo = repository.lower()
    name = filename.lower()
    if "minimax" in repo and "h3" in repo:
        return True
    if "bfs-best-face-swap" in repo:
        return name.startswith("h3/")
    return any(str(path).endswith("/" + filename) for path in extra_paths)


def build_plan(roots, linux_root, other_root, *, h3_paths=()):
    """Discover finished files through snapshots, trees, and shared blob metadata.

    Every revision is preserved. Primary revision files get their original names;
    older versions occupy a separate named repository directory with a revision
    suffix. Unmapped files and incomplete downloads are reported and left alone.
    """
    roots = [Path(root).absolute() for root in roots if Path(root).is_dir()]
    linux_root, other_root = Path(linux_root).absolute(), Path(other_root).absolute()
    aliases = {}
    all_regular = {}
    for root in roots:
        for path in _regular_files(root):
            try:
                details = path.stat()
                if not path.is_symlink():
                    aliases.setdefault((details.st_dev, details.st_ino), set()).add(str(path))
                    all_regular[str(path)] = details.st_size
            except OSError:
                pass
    entries, primary_revisions, warnings = {}, {}, []
    for root in roots:
        for directory in sorted(root.glob("models--*")):
            repo = directory.name.removeprefix("models--").replace("--", "/", 1)
            if not REPOSITORY.fullmatch(repo):
                warnings.append(f"Unrecognized repository directory: {directory}")
                continue
            trees = {path.stem: _read(path).get("files", {}) for path in sorted(directory.glob("trees/*.json"))}
            snapshots = {path.name: path for path in sorted(directory.glob("snapshots/*")) if path.is_dir()}
            revisions = sorted(set(trees) | set(snapshots))
            try:
                preferred = (directory / "refs/main").read_text().strip()
            except OSError:
                preferred = ""
            if repo not in primary_revisions and revisions:
                primary_revisions[repo] = preferred if preferred in revisions else revisions[0]
            for revision in revisions:
                try:
                    _relative(revision)
                except ValueError:
                    warnings.append(f"Unsafe revision in {directory}")
                    continue
                metadata = dict(trees.get(revision, {}))
                snapshot = snapshots.get(revision)
                if snapshot:
                    for path in _regular_files(snapshot):
                        metadata.setdefault(path.relative_to(snapshot).as_posix(), {})
                for filename, info in sorted(metadata.items()):
                    try:
                        relative = _relative(filename)
                    except ValueError as error:
                        warnings.append(str(error))
                        continue
                    info = info if isinstance(info, dict) else {}
                    candidates = [directory / "snapshots" / revision / relative]
                    for key in ("lfs_sha256", "blob_id", "xet_hash"):
                        identifier = info.get(key)
                        if identifier and str(identifier).isalnum():
                            candidates.extend([directory / "blobs" / identifier,
                                               root / "blobs" / identifier[:2] / identifier])
                    expected_size = info.get("lfs_size", info.get("size"))
                    source_paths, resolved_paths, sizes = set(), set(), set()
                    for path in candidates:
                        try:
                            details = path.stat()
                            if not stat.S_ISREG(details.st_mode):
                                continue
                            if expected_size is not None and details.st_size != expected_size:
                                warnings.append(f"Incomplete or mismatched file left untouched: {path}")
                                continue
                            resolved = str(path.resolve(strict=True))
                            source_paths.add(str(path))
                            source_paths.update(aliases.get((details.st_dev, details.st_ino), ()))
                            resolved_paths.add(resolved)
                            sizes.add(details.st_size)
                        except (OSError, RuntimeError):
                            pass
                    if not source_paths:
                        continue
                    if len(sizes) != 1:
                        warnings.append(f"Conflicting sizes left untouched: {repo}/{filename} at {revision}")
                        continue
                    key = (repo, revision, filename)
                    row = entries.setdefault(key, {"repository": repo, "revision": revision,
                        "filename": filename, "size": sizes.pop(), "sha256": info.get("lfs_sha256"),
                        "source_paths": [], "resolved_paths": []})
                    if row["sha256"] and info.get("lfs_sha256") and row["sha256"] != info["lfs_sha256"]:
                        raise ValueError(f"Conflicting LFS digest for {repo}/{filename} at {revision}")
                    row["sha256"] = row["sha256"] or info.get("lfs_sha256")
                    row["source_paths"] = sorted(set(row["source_paths"]) | source_paths)
                    row["resolved_paths"] = sorted(set(row["resolved_paths"]) | resolved_paths)
    mapped = set()
    for row in entries.values():
        local = h3_file(row["repository"], row["filename"], h3_paths)
        root = linux_root if local else other_root
        repo = row["repository"]
        if row["revision"] != primary_revisions[repo]:
            repo += "--revision-" + row["revision"][:12]
        row["directory"] = str(root / repo)
        row["destination"] = str(root / repo / row["filename"])
        row["storage"] = "linux" if local else "external"
        row["id"] = hashlib.sha256((row["repository"] + "\0" + row["revision"] + "\0" + row["filename"]).encode()).hexdigest()[:24]
        mapped.update(row["source_paths"])
        mapped.update(row["resolved_paths"])
    # Local H3 files need no extra space (hardlinks). Bring remote H3 components
    # next, then start with smaller external models to reclaim space promptly.
    def order(row):
        if row["storage"] == "linux":
            return (0, row["size"])
        return (1, row["size"])
    rows = sorted(entries.values(), key=order)
    unique = set()
    physical_bytes = 0
    for row in rows:
        for source in row["resolved_paths"] + row["source_paths"]:
            try:
                details = Path(source).stat()
                inode = (details.st_dev, details.st_ino)
                if inode not in unique:
                    physical_bytes += details.st_size
                    unique.add(inode)
            except OSError:
                pass
    return {"format": "odysseus-model-migration", "version": 1, "created_at": time.time(),
            "roots": [str(root) for root in roots], "linux_root": str(linux_root),
            "other_root": str(other_root), "files": rows, "warnings": warnings,
            "storage_devices": {"linux": _existing_parent(linux_root).stat().st_dev,
                                "external": _existing_parent(other_root).stat().st_dev},
            "summary": {"file_count": len(rows), "linux_bytes": sum(row["size"] for row in rows if row["storage"] == "linux"),
                        "external_bytes": sum(row["size"] for row in rows if row["storage"] == "external"),
                        "source_physical_bytes": physical_bytes},
            "unmapped": [{"path": path, "size": size} for path, size in sorted(all_regular.items()) if path not in mapped]}


def _digest(path, progress=None):
    checksum = hashlib.sha256()
    done, reported = 0, time.monotonic()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            checksum.update(chunk)
            done += len(chunk)
            if progress and time.monotonic() - reported >= 10:
                progress("verify", done)
                reported = time.monotonic()
    return checksum.hexdigest()


def _fingerprint(path):
    details = Path(path).stat()
    return [details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns]


def _copy_resumable(source, destination, *, progress=None):
    """Resume only after checking that the existing temporary prefix matches."""
    temporary = destination.with_name("." + destination.name + ".odysseus-migration-partial")
    fingerprint = _fingerprint(source)
    source_hash = hashlib.sha256()
    offset = temporary.stat().st_size if temporary.exists() else 0
    if offset > fingerprint[2]:
        raise ValueError(f"Temporary copy is larger than source: {temporary}")
    if shutil.disk_usage(destination.parent).free < fingerprint[2] - offset + RESERVE_BYTES:
        raise OSError(errno.ENOSPC, f"Insufficient space to migrate {destination.name}")
    done, reported = 0, time.monotonic()
    with source.open("rb") as original:
        if offset:
            with temporary.open("rb") as existing:
                while done < offset:
                    count = min(CHUNK_SIZE, offset - done)
                    chunk = original.read(count)
                    if existing.read(count) != chunk:
                        raise ValueError(f"Temporary copy prefix differs from source: {temporary}")
                    source_hash.update(chunk)
                    done += len(chunk)
        with temporary.open("ab") as output:
            while chunk := original.read(CHUNK_SIZE):
                output.write(chunk)
                source_hash.update(chunk)
                done += len(chunk)
                if progress and time.monotonic() - reported >= 10:
                    progress("copy", done)
                    reported = time.monotonic()
            output.flush()
            os.fsync(output.fileno())
    if _fingerprint(source) != fingerprint:
        raise ValueError(f"Source changed while copying: {source}")
    digest = source_hash.hexdigest()
    if temporary.stat().st_size != fingerprint[2] or _digest(temporary, progress) != digest:
        raise ValueError(f"Read-back verification failed: {temporary}")
    return temporary, digest


def _can_symlink(directory):
    probe = Path(directory) / (".odysseus-migration-link-test-" + uuid.uuid4().hex)
    try:
        probe.symlink_to(".odysseus-migration-link-test-target")
        return probe.is_symlink()
    except OSError as error:
        if error.errno in {errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return False
        raise
    finally:
        probe.unlink(missing_ok=True)


def _publish_no_replace(temporary, destination):
    """Publish atomically without ever replacing a concurrent download.

    Linux hardlinks cover local disks. WSL's 9p network drives reject both
    hardlinks and renameat2(RENAME_NOREPLACE); the Windows File.Move API supplies
    no-replace semantics on their owning filesystem instead.
    """
    try:
        os.link(temporary, destination)
    except OSError as error:
        if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise
    else:
        temporary.unlink()
        return
    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) == 0:
            return
        code = ctypes.get_errno()
        if code not in {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP}:
            raise OSError(code, os.strerror(code), str(destination))
    executable = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    values = []
    for path in (temporary, destination):
        match = re.fullmatch(r"/mnt/([a-zA-Z])/(.+)", str(path))
        if not match or not executable.is_file():
            raise OSError(errno.ENOTSUP, "Filesystem does not support atomic no-replace publication", str(destination))
        values.append(match[1].upper() + ":\\" + match[2].replace("/", "\\"))
    payload = base64.b64encode(json.dumps({"source": values[0], "destination": values[1]}).encode()).decode()
    script = ("$ErrorActionPreference='Stop'; "
              "$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + payload + "'))|ConvertFrom-Json; "
              "try { [IO.File]::Move($p.source,$p.destination) } "
              "catch { if([IO.File]::Exists($p.destination)){exit 17}; [Console]::Error.WriteLine($_.Exception.Message); exit 1 }")
    result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, timeout=60)
    if result.returncode == 17:
        raise FileExistsError(errno.EEXIST, "Destination appeared during migration", str(destination))
    if result.returncode:
        raise OSError(f"Windows no-replace publication failed: {result.stderr.strip()}")
    if temporary.exists() or not destination.is_file():
        raise OSError(f"No-replace publication did not complete: {destination}")


def _publish_metadata(row, digest):
    from src.model_library import write_model_metadata
    aliases = sorted(set(row["source_paths"] + row["resolved_paths"]))
    write_model_metadata(Path(row["directory"]), row["repository"], files={row["filename"]: {
        "revision": row["revision"], "size": row["size"], "sha256": digest,
        "source_path": aliases[0], "source_paths": aliases,
    }})


def execute_plan(plan, journal_path, *, progress=None, limit=None):
    """Perform a saved plan; rerunning is safe after interruption or partial copy.

    Journal timestamps and status are durable. Per-file failures are retained and
    stop the run; already-published data stays usable. No unrelated file is removed.
    """
    journal_path = Path(journal_path)
    journal = _read(journal_path) or {"format": "odysseus-model-migration-journal", "version": 1, "files": {}}
    capabilities = {}
    digest_cache = {}
    completed = 0
    for row in plan["files"]:
        if limit is not None and completed >= limit:
            break
        state = journal["files"].setdefault(row["id"], {"destination": row["destination"]})
        destination = Path(row["destination"])
        if state.get("status") == "complete" and destination.is_file() and state.get("destination_fingerprint") == _fingerprint(destination):
            continue
        state.update(status="working", started_at=time.time())
        state.pop("error", None)
        _atomic_json(journal_path, journal)
        def emit(phase, done=0):
            if progress:
                progress({"id": row["id"], "file": row["filename"], "repository": row["repository"],
                          "phase": phase, "bytes": done, "total": row["size"]})
        emit("start")
        try:
            expected_device = plan.get("storage_devices", {}).get(row["storage"])
            if expected_device is not None and _existing_parent(destination.parent).stat().st_dev != expected_device:
                raise OSError(errno.ENODEV, f"Destination filesystem changed or is not mounted: {destination.parent}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidates = [Path(path) for path in row["source_paths"] + row["resolved_paths"]
                          if Path(path).is_file() and Path(path).stat().st_size == row["size"]]
            if not candidates and not destination.is_file():
                raise FileNotFoundError(f"No complete source for {row['repository']}/{row['filename']}")
            target_device = destination.parent.stat().st_dev
            candidates.sort(key=lambda path: path.stat().st_dev != target_device)
            source = candidates[0] if candidates else destination
            if destination.exists():
                if destination.is_symlink() or destination.stat().st_size != row["size"]:
                    raise ValueError(f"Destination conflict (left untouched): {destination}")
                digest = _digest(destination, emit)
                source_digest = _digest(source, emit) if not os.path.samefile(source, destination) else digest
                if source_digest != digest:
                    raise ValueError(f"Destination content differs (left untouched): {destination}")
            else:
                temporary = destination.with_name("." + destination.name + ".link-" + uuid.uuid4().hex)
                try:
                    os.link(source.resolve(strict=True), temporary)
                    digest = _digest(temporary, emit)
                except OSError as error:
                    temporary.unlink(missing_ok=True)
                    if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP}:
                        raise
                    temporary, digest = _copy_resumable(source, destination, progress=emit)
                if row.get("sha256") and digest != row["sha256"]:
                    temporary.unlink(missing_ok=True)
                    raise ValueError(f"Hugging Face SHA-256 mismatch: {source}")
                # Do not replace a different destination created concurrently.
                if destination.exists():
                    raise FileExistsError(f"Destination appeared during migration: {destination}")
                _publish_no_replace(temporary, destination)
            if row.get("sha256") and digest != row["sha256"]:
                raise ValueError(f"Hugging Face SHA-256 mismatch: {destination}")
            digest_cache[tuple(_fingerprint(source))] = digest
            _publish_metadata(row, digest)
            state.update(sha256=digest, published_at=time.time(), retained_sources=[])
            _atomic_json(journal_path, journal)
            # Resolve all physical files before changing any aliases, so replacing
            # a shared blob cannot hide separate hardlinks that still occupy space.
            source_paths = sorted(set(row["source_paths"] + row["resolved_paths"]))
            for value in source_paths:
                alias = Path(value)
                if str(alias) == str(destination) or alias.is_symlink():
                    continue
                try:
                    fingerprint = tuple(_fingerprint(alias))
                except FileNotFoundError:
                    continue
                if fingerprint[2] != row["size"]:
                    raise ValueError(f"Source alias changed: {alias}")
                if not os.path.samefile(alias, destination):
                    alias_digest = digest_cache.get(fingerprint)
                    if alias_digest is None:
                        alias_digest = _digest(alias, emit)
                        digest_cache[fingerprint] = alias_digest
                    if alias_digest != digest:
                        raise ValueError(f"Source alias differs; preserved: {alias}")
                device = fingerprint[0]
                if device not in capabilities:
                    capabilities[device] = _can_symlink(alias.parent)
                if not capabilities[device]:
                    state["retained_sources"].append(str(alias))
                    continue
                if tuple(_fingerprint(alias)) != fingerprint:
                    raise ValueError(f"Source alias changed during verification: {alias}")
                temporary = alias.with_name(alias.name + ".migration-link-" + uuid.uuid4().hex)
                try:
                    temporary.symlink_to(destination)
                    os.replace(temporary, alias)
                finally:
                    temporary.unlink(missing_ok=True)
            state.update(status="complete", completed_at=time.time(), destination_fingerprint=_fingerprint(destination))
            _atomic_json(journal_path, journal)
            completed += 1
            emit("complete", row["size"])
        except Exception as error:
            state.update(status="error", error=str(error), failed_at=time.time())
            _atomic_json(journal_path, journal)
            raise
    return journal
