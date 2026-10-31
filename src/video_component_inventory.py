"""Bounded model discovery: slow libraries must not hold up job requests.

Each configured root has one background scan and a persistent last successful
snapshot. Scans of local models can finish independently of an unavailable NAS.
Only the small index lives here; model files are never copied or relocated.
"""
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from src.constants import VIDEO_COMPONENT_CACHE_DIR

log = logging.getLogger(__name__)
REFRESH_SECONDS = 30
WAIT_SECONDS = 1.0
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_INDEXES = {}
_INDEX_LOCK = threading.Lock()


class InventoryPending(RuntimeError):
    def __init__(self):
        super().__init__("Model libraries are still being scanned in the background. Refresh components in a moment.")


@dataclass
class _Root:
    rows: list | None = None
    expires: float = 0
    scanning: bool = False
    error: str = ""
    revision: int = 0


class ComponentIndex:
    def __init__(self, namespace, scanner, *, directory=None):
        self.namespace, self.scanner = namespace, scanner
        self.directory = Path(directory or VIDEO_COMPONENT_CACHE_DIR)
        self.condition = threading.Condition()
        self.roots = {}

    def _path(self, root):
        key = hashlib.sha256((self.namespace + "\0" + root).encode()).hexdigest()
        return self.directory / (key + ".json")

    def _load(self, root):
        try:
            path = self._path(root)
            if path.stat().st_size > MAX_SNAPSHOT_BYTES:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if (value.get("version") != 1 or value.get("namespace") != self.namespace
                    or value.get("root") != root):
                return None
            rows = value["components"]
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ("id", "name", "path"))
                for row in rows
            ):
                return None
            return rows
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _save(self, root, rows):
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(root)
            temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
            with temporary.open("x", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                json.dump({"version": 1, "namespace": self.namespace, "root": root,
                           "components": rows}, stream)
            temporary.replace(path)
        except OSError:
            log.warning("Unable to persist video model inventory", exc_info=True)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _scan(self, root, entry):
        started = time.monotonic()
        revision = entry.revision
        try:
            # All mounted-drive I/O, including resolving symlinks, stays here.
            path = Path(root)
            try:
                path.stat()
            except FileNotFoundError:
                if entry.rows:
                    raise
                rows = []
            else:
                rows = self.scanner([path])
            for row in rows:
                resolved = Path(row["path"]).resolve()
                # Preserve H3's deduplication of identical HF blobs in different
                # cache roots, without resolving network paths during requests.
                if "blobs" in resolved.parts and re.fullmatch(r"[a-f0-9]{64}", resolved.name):
                    row["_blob_identity"] = resolved.name + "/" + row["name"]
            with self.condition:
                if revision == entry.revision:
                    entry.rows, entry.error = rows, ""
                    self._save(root, rows)
            log.info("Video model scan: %s, %d components, %.3fs", root, len(rows), time.monotonic() - started)
        except Exception as error:
            with self.condition:
                entry.error = str(error)
            log.warning("Video model scan failed for %s; keeping previous inventory: %s", root, error)
        finally:
            with self.condition:
                entry.expires = time.monotonic() + REFRESH_SECONDS if revision == entry.revision else 0
                entry.scanning = False
                self.condition.notify_all()

    def invalidate(self, path, component=None):
        path = Path(path).absolute()
        with self.condition:
            for root, entry in self.roots.items():
                if path.is_relative_to(root):
                    entry.revision += 1
                    entry.expires = 0
                    if component is not None and entry.rows is not None:
                        entry.rows = [row for row in entry.rows if row['id'] != component['id']]
                        entry.rows.append(deepcopy(component))
                        self._save(root, entry.rows)

    def get(self, roots, *, refresh=False, wait=WAIT_SECONDS):
        # absolute() is lexical. resolve()/is_dir() can block on a mounted NAS.
        roots = list(dict.fromkeys(str(Path(root).absolute()) for root in roots))
        deadline = time.monotonic() + wait
        with self.condition:
            for root in roots:
                if root not in self.roots:
                    self.roots[root] = _Root(rows=self._load(root))
                entry = self.roots[root]
                if not entry.scanning and (refresh or entry.expires <= time.monotonic()):
                    entry.scanning = True
                    threading.Thread(target=self._scan, args=(root, entry), daemon=True,
                                     name="video-model-scan").start()
            # Explicit refreshes give fast disks a chance to publish new files.
            # Ordinary edits/reruns use the current snapshot without waiting.
            # On a cold start, allow the main library to finish even if a tiny
            # runtime helper directory happens to complete first.
            wait_for_scan = refresh or bool(roots and self.roots[roots[0]].rows is None)
            while any(self.roots[root].scanning for root in roots) and wait_for_scan:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.condition.wait(remaining)
            rows = deepcopy([row for root in roots for row in (self.roots[root].rows or [])])
            status = {"refreshing": any(self.roots[root].scanning for root in roots),
                      "pending_roots": [root for root in roots if self.roots[root].rows is None],
                      "unavailable_roots": [root for root in roots if self.roots[root].error]}
        return merge_components(rows), status


def merge_components(rows):
    """Keep root priority and checkpoint replacement aliases across snapshots."""
    superseded = {alias for row in rows for alias in row.get("aliases", []) if alias != row["id"]}
    seen, blobs, result = set(), set(), []
    for row in rows:
        blob = row.pop("_blob_identity", None)
        if row["id"] in seen or row["id"] in superseded or blob and blob in blobs:
            continue
        seen.add(row["id"])
        if blob:
            blobs.add(blob)
        result.append(row)
    return result


def component_index(project, family, scanner):
    namespace = family + ":" + str(Path(project).absolute())
    key = (namespace, scanner, VIDEO_COMPONENT_CACHE_DIR)
    with _INDEX_LOCK:
        if key not in _INDEXES:
            _INDEXES[key] = ComponentIndex(namespace, scanner)
        return _INDEXES[key]
