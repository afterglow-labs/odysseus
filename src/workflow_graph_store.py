"""Owner-scoped graph documents, with optimistic revisions independent of runs."""
import copy
import hashlib
import re
import time
import uuid
from pathlib import Path

from src.h3_video import _locked, _read, _write

ID = re.compile(r"[a-f0-9]{32}\Z")


class GraphRevisionConflict(ValueError):
    pass


class GraphDocumentStore:
    def __init__(self, root):
        self.root = Path(root)

    def _owner_root(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ValueError("Sign in to use saved workflows")
        return self.root / hashlib.sha256(owner.encode()).hexdigest()

    def _path(self, owner, identity):
        if not isinstance(identity, str) or not ID.fullmatch(identity):
            raise FileNotFoundError("Workflow not found")
        path = self._owner_root(owner) / (identity + ".json")
        if path.is_symlink():
            raise FileNotFoundError("Workflow not found")
        return path

    def get(self, owner, identity):
        value = _read(self._path(owner, identity))
        if not value or value.get("owner") != owner:
            raise FileNotFoundError("Workflow not found")
        return {key: copy.deepcopy(value[key]) for key in ("id", "revision", "updated_at", "document")}

    def list(self, owner, offset=0, limit=50):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Choose a page of 1–100 workflows")
        rows = []
        for path in self._owner_root(owner).glob("*.json"):
            if not ID.fullmatch(path.stem) or path.is_symlink():
                continue
            value = _read(path)
            if value.get("owner") == owner and value.get("id") == path.stem:
                rows.append({"id": value["id"], "revision": value["revision"],
                             "updated_at": value["updated_at"],
                             "title": value.get("document", {}).get("title", "Untitled workflow")})
        rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
        return {"documents": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit}

    def save(self, owner, document, identity=None, revision=None):
        from src.workflow_graph import validate_document
        validate_document(document)
        directory = self._owner_root(owner)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        creating = identity is None
        identity = uuid.uuid4().hex if creating else identity
        path = self._path(owner, identity)
        with _locked(directory / "documents.lock"):
            previous = None if creating else self.get(owner, identity)
            if previous is not None and (type(revision) is not int or revision != previous["revision"]):
                raise GraphRevisionConflict("This workflow changed in another editor. Reopen it or save a new copy.")
            result = {"id": identity, "owner": owner,
                      "revision": 1 if previous is None else previous["revision"] + 1,
                      "updated_at": time.time(), "document": copy.deepcopy(document)}
            _write(path, result)
        return self.get(owner, identity)

    def delete(self, owner, identity):
        path = self._path(owner, identity)
        with _locked(path.parent / "documents.lock"):
            self.get(owner, identity)
            path.unlink()
        return {"deleted": True, "id": identity}
