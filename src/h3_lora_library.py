"""Data-only H3 adapter installation and persistent, reusable profiles."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid
from urllib.parse import unquote, urlsplit
import requests

from scripts.h3_lora_controls import PROFILE_LIMIT, read_profile, write_profile, validate_lora_profile

MAX_BYTES = 8 * 1024**3
CHUNK = 1024**2
RESERVE = 512 * 1024**2
ID = re.compile(r"[a-f0-9]{32}\Z")
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


def safe_filename(value, *, nested=False):
    if (not isinstance(value, str) or not value or len(value) > 240 or "\\" in value or ":" in value
            or any(ord(char) < 32 for char in value) or any(part in {"", ".", ".."} or part.startswith(".") for part in value.split("/"))
            or not nested and "/" in value or not value.lower().endswith(".safetensors")):
        raise ValueError("Choose a relative .safetensors filename without hidden or parent directories")
    return value


def download_source(raw):
    if not isinstance(raw, dict) or raw.keys() - {"url", "repository", "filename", "revision", "profile"}:
        raise ValueError("Invalid LoRA download fields")
    repository, filename, revision = raw.get("repository"), raw.get("filename"), raw.get("revision", "main")
    if raw.get("url"):
        if repository or filename:
            raise ValueError("Supply an exact Hugging Face file URL or repository and filename")
        if not isinstance(raw["url"], str) or len(raw["url"]) > 2048:
            raise ValueError("Invalid Hugging Face file URL")
        url = urlsplit(raw["url"])
        parts = unquote(url.path).strip("/").split("/")
        if url.scheme != "https" or url.netloc != "huggingface.co" or url.fragment or len(parts) < 5 or parts[2] not in {"blob", "resolve"}:
            raise ValueError("Use an exact https://huggingface.co repository file URL")
        repository, revision, filename = "/".join(parts[:2]), parts[3], "/".join(parts[4:])
    if not isinstance(repository, str) or not REPO.fullmatch(repository) or ".." in repository:
        raise ValueError("Use a Hugging Face owner/repository name")
    if not isinstance(revision, str) or not REVISION.fullmatch(revision) or revision in {".", ".."}:
        raise ValueError("Use a branch, tag, or commit revision")
    return {"repository": repository, "filename": safe_filename(filename, nested=True), "revision": revision,
            "profile": validate_lora_profile(raw.get("profile", {}))}


def require_space(directory, amount):
    if shutil.disk_usage(directory).free < amount + RESERVE:
        raise OSError("Not enough disk space for this adapter; keep at least 512 MiB free")


def validate_adapter(path):
    from src.video_workflow import validate_safetensors
    from src.model_artifacts import _safetensors_kind
    validate_safetensors(Path(path))
    adapter, _ = _safetensors_kind(path)
    if not adapter:
        raise ValueError("This safetensors file has no recognized LoRA adapter tensors")


class LoraLibrary:
    def __init__(self, manager):
        self.manager = manager
        self.root = manager.project / "models" / "loras"
        self.state_root = manager.root.parent / ".lora-library-downloads"
        self.lock = threading.RLock()
        self.active = {}

    def _prepare(self):
        for path in (self.root, self.state_root):
            if path.is_symlink():
                raise ValueError("The adapter library directory cannot be a symbolic link")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def components(self):
        from src.h3_video import discover_components, cache_roots
        return [item for item in discover_components(cache_roots(self.manager.project)) if item["role"] == "lora"]

    def invalidate(self):
        with self.manager._submission_inventory_lock:
            self.manager._submission_inventory_cache = None

    def profile(self, identity, profile):
        profile = validate_lora_profile(profile)
        component = next((item for item in self.components() if identity == item["id"] or identity in item.get("aliases", [])), None)
        if component is None:
            raise FileNotFoundError("Installed adapter not found")
        validate_adapter(component["path"])
        write_profile(component["path"], profile, overwrite=True)
        self.invalidate()
        return next(item for item in self.components() if item["id"] == component["id"])

    def stage(self, filename):
        self._prepare()
        filename = safe_filename(filename)
        staging = self.root / (".upload-" + uuid.uuid4().hex)
        staging.mkdir(mode=0o700)
        return staging / filename

    def install(self, path, profile=None, *, repository=None, source_filename=None, revision=None):
        path = Path(path)
        validate_adapter(path)
        write_profile(path, validate_lora_profile(profile or {}), overwrite=True)
        if repository:
            from src.model_library import write_model_metadata
            write_model_metadata(path.parent, repository, {path.name: {"upstream_relative_path": source_filename,
                                                          "revision": revision, "sha256": self.digest(path)}})
        target = self.root / uuid.uuid4().hex
        os.replace(path.parent, target)
        self.invalidate()
        component_path = str(target / path.name)
        return next(item for item in self.components() if item["path"] == component_path)

    @staticmethod
    def digest(path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(CHUNK), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _write(self, state):
        target = self.state_root / (state["id"] + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)

    def _state(self, identity, owner):
        if not ID.fullmatch(identity):
            raise FileNotFoundError("Adapter download not found")
        try:
            state = json.loads((self.state_root / (identity + ".json")).read_text())
        except (OSError, ValueError):
            raise FileNotFoundError("Adapter download not found") from None
        if state.get("owner") != owner:
            raise FileNotFoundError("Adapter download not found")
        if state["status"] in {"queued", "downloading"} and identity not in self.active:
            state.update(status="failed", error="Server restarted before the download finished. Add the adapter again to retry.")
            staging = state.get("staging_name", "")
            if isinstance(staging, str) and re.fullmatch(r"\.upload-[a-f0-9]{32}", staging):
                shutil.rmtree(self.root / staging, ignore_errors=True)
            self._write(state)
        return state

    @staticmethod
    def public(state):
        result = {key: value for key, value in state.items() if key not in {"owner", "source", "staging_name"}}
        source = state.get("source", {})
        for key in ("repository", "filename"):
            if isinstance(source.get(key), str):
                result[key] = source[key]
        return result

    def status(self, identity, owner):
        with self.lock:
            return self.public(self._state(identity, owner))

    def downloads(self, owner):
        if not self.state_root.exists():
            return []
        values = []
        for path in self.state_root.glob("*.json"):
            try:
                values.append(self.status(path.stem, owner))
            except FileNotFoundError:
                pass
        return sorted(values, key=lambda value: value.get("created_at", 0), reverse=True)[:30]

    def start(self, owner, raw):
        source = download_source(raw)
        self._prepare()
        with self.lock:
            if len(self.active) >= 2:
                raise RuntimeError("Two adapter downloads are already active; wait or cancel one")
            identity = uuid.uuid4().hex
            state = {"id": identity, "owner": owner, "status": "queued", "downloaded_bytes": 0,
                     "total_bytes": None, "error": None, "created_at": time.time(), "source": source}
            stop = threading.Event()
            self.active[identity] = stop
            self._write(state)
            threading.Thread(target=self._download, args=(state, stop), daemon=True,
                             name="h3-lora-download").start()
            return self.public(state)

    def cancel(self, identity, owner):
        with self.lock:
            state = self._state(identity, owner)
            if identity in self.active:
                self.active[identity].set()
                state.update(status="cancelled", error=None)
                self._write(state)
            return self.public(state)

    def _download(self, state, stop):
        path = None
        try:
            from huggingface_hub import get_hf_file_metadata, hf_hub_url
            from routes.cookbook_helpers import load_stored_hf_token
            source = state["source"]
            token = load_stored_hf_token(state_path=self.manager.root.parent.parent / "cookbook_state.json")
            url = hf_hub_url(source["repository"], source["filename"], revision=source["revision"])
            metadata = get_hf_file_metadata(url, token=token or False, timeout=30)
            if not isinstance(metadata.size, int) or not 0 < metadata.size <= MAX_BYTES:
                raise ValueError("The adapter must have a known size of at most 8 GiB")
            if not metadata.commit_hash or not re.fullmatch(r"[a-f0-9]{40,64}", metadata.commit_hash):
                raise ValueError("Hugging Face did not provide a pinned file revision")
            path = self.stage(Path(source["filename"]).name)
            require_space(path.parent, metadata.size)
            state.update(status="downloading", total_bytes=metadata.size, staging_name=path.parent.name)
            with self.lock:
                self._write(state)
            digest = hashlib.sha256()
            url = hf_hub_url(source["repository"], source["filename"], revision=metadata.commit_hash)
            headers = {"Authorization": "Bearer " + token} if token else {}
            if stop.is_set():
                raise InterruptedError
            with requests.get(url, headers=headers, stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    for chunk in response.iter_content(chunk_size=CHUNK):
                        if stop.is_set():
                            raise InterruptedError
                        if not chunk:
                            continue
                        state["downloaded_bytes"] += len(chunk)
                        if state["downloaded_bytes"] > metadata.size:
                            raise ValueError("Downloaded file exceeds the advertised size")
                        require_space(path.parent, len(chunk))
                        stream.write(chunk)
                        digest.update(chunk)
                        with self.lock:
                            if stop.is_set():
                                raise InterruptedError
                            self._write(state)
            if state["downloaded_bytes"] != metadata.size:
                raise ValueError("The adapter download was incomplete")
            if metadata.etag and re.fullmatch(r"[a-f0-9]{64}", metadata.etag) and digest.hexdigest() != metadata.etag:
                raise ValueError("The adapter checksum did not match Hugging Face")
            with self.lock:
                if stop.is_set():
                    raise InterruptedError
                component = self.install(path, source["profile"], repository=source["repository"],
                                         source_filename=source["filename"], revision=metadata.commit_hash)
                state.update(status="completed", component=component)
        except InterruptedError:
            state.update(status="cancelled", error=None)
        except Exception as error:
            # Network exceptions often include signed URLs; do not retain them or credentials.
            message = str(error) if isinstance(error, (ValueError, OSError)) and not isinstance(error, requests.exceptions.RequestException) else "Adapter download failed. Check the exact file, Hugging Face access token, and connection, then retry."
            message = re.sub(r"hf_[A-Za-z0-9]+", "[redacted]", message)
            state.update(status="cancelled" if stop.is_set() else "failed", error=None if stop.is_set() else message[:500])
        finally:
            if path is not None and path.parent.name.startswith(".upload-"):
                shutil.rmtree(path.parent, ignore_errors=True)
            with self.lock:
                self.active.pop(state["id"], None)
                self._write(state)
