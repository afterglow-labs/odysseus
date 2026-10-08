"""Readable model repository metadata; standard-library only for remote runners.

Weights retain upstream filenames. This small, local manifest records their
repository identity and compatibility aliases when old caches are reorganized.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile


METADATA_NAME = ".odysseus-model.json"
FORMAT = "odysseus-model"
VERSION = 1
_REPOSITORY = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")
_REVISION = re.compile(r"[a-fA-F0-9]{40,64}\Z")


def _repository(value):
    if not isinstance(value, str) or not _REPOSITORY.fullmatch(value):
        raise ValueError("Choose a valid Hugging Face owner/repository")
    return value


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid model filename")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Model filenames must stay inside their repository")
    return path.as_posix()


def named_model_directory(root, repository):
    """Return the exact readable repository directory on the selected host."""
    repository = _repository(repository)
    # This also runs on the coordinator for SSH targets. Leave ~ and Windows
    # drive roots untouched until the runner executes on the selected host.
    directory = str(root).replace("\\", "/").rstrip("/")
    if not directory:
        directory = "/"
    return directory.rstrip("/") + "/" + repository


def migrated_library_root(value, environ=None):
    """Translate obsolete client download roots after an installed migration.

    Only explicitly recorded roots are translated; other chosen folders retain
    their meaning. This lets an older phone/tab keep the new storage policy.
    """
    if not value:
        return value
    env = os.environ if environ is None else environ
    try:
        aliases = json.loads(env.get("ODYSSEUS_MODEL_DIR_ALIASES", "{}"))
    except (ValueError, TypeError):
        return value
    if not isinstance(aliases, dict):
        return value
    key = os.path.expanduser(str(value)).replace("\\", "/").rstrip("/")
    for old, current in aliases.items():
        if (isinstance(old, str) and isinstance(current, str) and current
                and os.path.expanduser(old).replace("\\", "/").rstrip("/") == key):
            return current
    return value


def read_model_metadata(directory):
    path = Path(directory) / METADATA_NAME
    try:
        if path.is_symlink() or path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(value, dict) or value.get("format") != FORMAT
                or value.get("version") != VERSION or not isinstance(value.get("files", {}), dict)):
            return None
        _repository(value.get("repository"))
        return value
    except (OSError, ValueError, TypeError):
        return None


@contextmanager
def _metadata_lock(directory):
    path = Path(directory) / ".odysseus-model.lock"
    if path.is_symlink():
        raise ValueError("Model metadata lock cannot be a symbolic link")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if not stream.tell():
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def write_model_metadata(directory, repository, files=None):
    """Merge local provenance atomically, retaining concurrent download records."""
    repository = _repository(repository)
    directory = Path(directory)
    updates = {}
    for name, row in (files or {}).items():
        name = _relative(name)
        if not isinstance(row, dict):
            raise ValueError("Invalid model file metadata")
        updates[name] = dict(row)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / METADATA_NAME
    with _metadata_lock(directory):
        if target.is_symlink():
            raise ValueError("Model metadata cannot be a symbolic link")
        previous = read_model_metadata(directory)
        if target.exists() and previous is None:
            raise ValueError("Existing model metadata is invalid; it was not overwritten")
        if previous and previous["repository"] != repository:
            raise ValueError("This directory belongs to a different model repository")
        document = previous or {"format": FORMAT, "version": VERSION, "repository": repository, "files": {}}
        document.setdefault("files", {})
        for name, row in updates.items():
            document["files"][name] = {**document["files"].get(name, {}), **row}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                             prefix=".odysseus-model-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return document


def _file_metadata(path, metadata_cache=None):
    metadata_cache = {} if metadata_cache is None else metadata_cache
    path = Path(path).absolute()
    candidates = [path]
    resolved = path.resolve()
    if resolved != path:
        candidates.append(resolved)
    for candidate in candidates:
        # Existing HF snapshot/hash paths do not carry named-library metadata.
        # Their symlink target, if migrated, is inspected on the next iteration.
        if any(part.startswith("models--") for part in candidate.parts):
            continue
        if "blobs" in candidate.parts and re.fullmatch(r"[a-f0-9]{40,64}", candidate.name):
            continue
        # Repository files can be nested (transformer shards, LoRA families).
        for parent in list(candidate.parents)[:16]:
            key = str(parent)
            if key not in metadata_cache:
                metadata_cache[key] = read_model_metadata(parent)
            metadata = metadata_cache[key]
            if metadata is None:
                continue
            relative = candidate.relative_to(parent).as_posix()
            if relative.startswith("."):
                return None
            row = metadata.get("files", {}).get(relative, {})
            return parent, relative, metadata, row if isinstance(row, dict) else {}
    return None


def component_reference(path):
    """Return portable HF identity for a readable file, never local aliases."""
    found = _file_metadata(path)
    if not found:
        return None
    root, relative, metadata, row = found
    result = {"name": PurePosixPath(relative).name, "repository": metadata["repository"], "relative_path": relative}
    revision = row.get("revision")
    # Local-dir metadata describes the current named file. Prefer it to an
    # older migration record when HF has since downloaded a newer revision.
    download_meta = root / ".cache/huggingface/download" / (relative + ".metadata")
    try:
        if not download_meta.is_symlink() and download_meta.stat().st_size <= 64 * 1024:
            current = download_meta.read_text(encoding="utf-8").splitlines()[0]
            if _REVISION.fullmatch(current):
                revision = current
    except (OSError, ValueError, IndexError):
        pass
    if isinstance(revision, str) and _REVISION.fullmatch(revision):
        result["revision"] = revision
    return result


def component_aliases(path, metadata_cache=None):
    """Old path-based component IDs accepted after a verified migration."""
    found = _file_metadata(path, metadata_cache)
    if not found:
        return []
    row = found[3]
    sources = row.get("source_paths", [])
    sources = sources if isinstance(sources, list) else []
    if row.get("source_path"):
        sources = [*sources, row["source_path"]]
    return sorted({hashlib.sha256(value.encode()).hexdigest()[:32]
                   for value in sources if isinstance(value, str) and os.path.isabs(value)})
