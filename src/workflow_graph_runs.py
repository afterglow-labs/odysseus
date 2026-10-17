"""Durable graph coordination using the existing owner-scoped video queue.

The graph compiler supplies data, never executable code or filesystem paths.
Only the authenticated upload route may provide trusted staged media to accept.
One flock lease owns each coordinator, including across application processes.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid

from starlette.datastructures import UploadFile

from src.h3_video import _locked, _read, _write
from src.video_submission import ensure_upload_space


RUN_ID = re.compile(r"[a-f0-9]{32}\Z")
TERMINAL = {"completed", "failed", "cancelled"}
STEP_TERMINAL = {"completed", "failed", "blocked", "cancelled"}
OPERATIONS = {"submit", "execute", "refine", "trim", "resize", "save"}


def _clone(source, target):
    """Copy inputs so a staged file or source job can safely be removed."""
    source, target = Path(source), Path(target)
    if source.is_symlink() or not source.is_file():
        raise ValueError("A workflow input is unavailable")
    ensure_upload_space(target.parent, source.stat().st_size)
    # Uploaded inputs are immutable after acceptance. A copy also isolates a
    # future caller's mutation of its staging file, unlike a hardlink.
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)


def _label(value, fallback="output.mp4"):
    text = str(value or fallback).replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(char for char in text if char.isprintable())[:200] or fallback


def _plain(value):
    return json.loads(json.dumps(value, allow_nan=False))


class _Cancelled(Exception):
    pass


class GraphRunManager:
    def __init__(self, root, managers, *, poll_interval=.5):
        self.root = Path(root)
        self.managers = dict(managers)
        self.poll_interval = max(.02, poll_interval)
        self._threads = {}
        self._thread_lock = threading.Lock()
        self._shutdown = threading.Event()

    def _identity(self, owner, request_id):
        if not isinstance(owner, str) or not owner:
            raise ValueError("A workflow run owner is required")
        try:
            key = uuid.UUID(str(request_id)).hex
        except (ValueError, TypeError, AttributeError):
            raise ValueError("A request_id UUID is required for safe workflow retries") from None
        return hashlib.sha256(f"workflow-graph\0{owner}\0{key}".encode()).hexdigest()[:32], key

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise FileNotFoundError("Workflow run not found")
        directory = self.root / run_id
        if directory.is_symlink():
            raise FileNotFoundError("Workflow run not found")
        return directory

    def _owned(self, run_id, owner):
        directory = self._directory(run_id)
        state = _read(directory / "state.json")
        if not state or state.get("owner") != owner:
            raise FileNotFoundError("Workflow run not found")
        return directory, state

    def existing(self, owner, request_id):
        run_id, _ = self._identity(owner, request_id)
        if not (self._directory(run_id) / "state.json").is_file():
            return None
        value = self.view(run_id, owner)
        if value["status"] not in TERMINAL:
            self._start(run_id)
        return value

    def _check_plan(self, compiled, media):
        if not isinstance(compiled, dict) or compiled.get("valid") is not True:
            raise ValueError("Validate the workflow before running it")
        plan = compiled.get("plan")
        if not isinstance(plan, list) or not 1 <= len(plan) <= 300:
            raise ValueError("Choose at least one executable workflow output")
        ids = [step.get("id") for step in plan if isinstance(step, dict)]
        if (len(ids) != len(plan) or any(not isinstance(i, str) or not i or len(i) > 200 for i in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Workflow steps need unique identifiers")
        by_id = {step["id"]: step for step in plan}
        if not isinstance(media, dict) or set(media) & set(ids):
            raise ValueError("Invalid workflow media bindings")
        referenced = set()
        for step in plan:
            operation, family = step.get("operation"), step.get("family")
            if operation not in OPERATIONS or family not in {"media", "h3", "bfs", "comfy"}:
                raise ValueError("Unsupported workflow operation")
            if ((operation in {"trim", "resize", "save"}) != (family == "media") or operation == "refine" and family != "h3"
                    or (operation == "execute") != (family == "comfy")):
                raise ValueError("Workflow operation does not match its execution family")
            if family != "media" and family not in self.managers:
                raise ValueError("This workflow's video backend is unavailable")
            dependencies = step.get("dependencies", [])
            if not isinstance(dependencies, list) or any(dep not in by_id or dep == step["id"] for dep in dependencies):
                raise ValueError("Invalid workflow dependency")
            inputs = step.get("inputs", {})
            if not isinstance(inputs, dict):
                raise ValueError("Invalid workflow step inputs")
            for field, refs in inputs.items():
                if not isinstance(field, str) or not isinstance(refs, list):
                    raise ValueError("Invalid workflow input binding")
                for ref in refs:
                    if not isinstance(ref, str):
                        raise ValueError("Invalid workflow input reference")
                    if ref in by_id:
                        if ref not in dependencies:
                            raise ValueError("A connected workflow step must be a dependency")
                    else:
                        referenced.add(ref)
                        if not media.get(ref):
                            raise ValueError(f"Attach media for workflow input {ref}")
            if operation == "refine":
                source = step.get("depends_on")
                if source not in dependencies or by_id[source].get("family") != "h3":
                    raise ValueError("Refinement requires an upstream H3 preview")
                if type(step.get("candidate")) is not int or not 1 <= step["candidate"] <= 3:
                    raise ValueError("Choose preview candidate 1, 2 or 3")
                from src.h3_seed_hunter import FINAL_EDIT_SETTINGS, FINAL_OPTIONS
                config = step.get("config", {})
                if (not isinstance(config, dict) or config.keys() - (FINAL_EDIT_SETTINGS | {"denoise"})
                        or not isinstance(config.get("seed_hunter", {}), dict)
                        or config.get("seed_hunter", {}).keys() - FINAL_OPTIONS):
                    raise ValueError("Unsupported saved-preview refinement setting")
            elif operation in {"trim", "resize", "save"}:
                self._cpu_params(step)
                video_refs = inputs.get("video", [])
                if len(video_refs) != 1 or sum(len(media[ref]) if ref in media else 1 for ref in video_refs) != 1:
                    raise ValueError("Media processing needs exactly one video input")
        visiting, done = set(), set()

        def visit(identity):
            if identity in visiting:
                raise ValueError("Workflow dependencies contain a cycle")
            if identity in done:
                return
            visiting.add(identity)
            for dependency in by_id[identity].get("dependencies", []):
                visit(dependency)
            visiting.remove(identity)
            done.add(identity)

        for identity in ids:
            visit(identity)
        for ref in referenced:
            values = media[ref]
            if not isinstance(values, list) or not 1 <= len(values) <= 25:
                raise ValueError("Invalid workflow media binding")
            for value in values:
                if not isinstance(value, dict) or not isinstance(value.get("path"), (str, Path)):
                    raise ValueError("Invalid staged workflow input")
                path = Path(value["path"])
                if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                    raise ValueError("A staged workflow input is unavailable or empty")
        for step in plan:
            if step["operation"] in {"submit", "execute"}:
                uploads = {field: [None for ref in refs for _ in (media[ref] if ref in media else [None])]
                           for field, refs in step.get("inputs", {}).items()}
                self._validate_native(step, uploads)
        return referenced

    def accept(self, owner, document, compiled, media, request_id):
        run_id, key = self._identity(owner, request_id)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        locks = self.root / ".locks"
        locks.mkdir(exist_ok=True, mode=0o700)
        # A retry reaches its receipt before touching supplied media again.
        with _locked(locks / f"accept-{int(run_id[:2], 16) % 64:02d}"):
            existing = self.existing(owner, key)
            if existing is not None:
                return existing
            snapshot, compiled = _plain(document), _plain(compiled)
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid workflow document")
            referenced = self._check_plan(compiled, media)
            temporary = Path(tempfile.mkdtemp(prefix=".accept-", dir=self.root))
            try:
                inputs = temporary / "inputs"
                inputs.mkdir(mode=0o700)
                saved = {}
                for ref in sorted(referenced):
                    saved[ref] = []
                    for index, value in enumerate(media[ref]):
                        suffix = Path(value["path"]).suffix.lower()
                        target = inputs / (hashlib.sha256(ref.encode()).hexdigest()[:24] + f"-{index}" + suffix)
                        _clone(value["path"], target)
                        saved[ref].append({"path": str(target.relative_to(temporary)),
                                           "name": _label(value.get("name"), Path(value["path"]).name)})
                now = time.time()
                state = {"id": run_id, "owner": owner, "request_id": key, "status": "queued",
                         "created_at": now, "updated_at": now, "cancel_requested": False,
                         "title": str(snapshot.get("title", "Workflow run"))[:200],
                         "steps": {step["id"]: {"id": step["id"], "status": "pending", "family": step["family"],
                                                "operation": step["operation"]} for step in compiled["plan"]}}
                _write(temporary / "manifest.json", {"document": snapshot, "compiled": compiled, "media": saved})
                _write(temporary / "state.json", state)
                destination = self._directory(run_id)
                if destination.exists():
                    raise RuntimeError("Workflow run staging already exists")
                temporary.rename(destination)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        self._start(run_id)
        from src.workflow_prompt_history import remember_graph_prompts
        remember_graph_prompts(self, owner, compiled)
        return self.view(run_id, owner)

    def _public(self, directory, state):
        manifest = _read(directory / "manifest.json")
        result = {key: state.get(key) for key in ("id", "title", "status", "created_at", "updated_at", "finished_at", "error", "cancel_requested")}
        result["steps"] = [{key: value for key, value in row.items() if key != "output"}
                           for row in state.get("steps", {}).values()]
        result["outputs"] = []
        for output in manifest.get("compiled", {}).get("outputs", []):
            step = state.get("steps", {}).get(output.get("source"), {})
            result["outputs"].append({**output, "status": step.get("status", "pending"),
                                      "available": step.get("status") == "completed" and bool(step.get("output")),
                                      "name": step.get("output", {}).get("name"),
                                      "url": f"/api/workflow-graphs/runs/{state['id']}/outputs/{output['id']}"})
        return result

    def view(self, run_id, owner):
        directory, state = self._owned(run_id, owner)
        return self._public(directory, state)

    def detail(self, run_id, owner):
        """Return an owner's immutable submitted graph for editing a new draft."""
        directory, state = self._owned(run_id, owner)
        result = self._public(directory, state)
        result["document"] = _plain(_read(directory / "manifest.json").get("document", {}))
        return result

    def list(self, owner):
        if not self.root.is_dir():
            return []
        rows = []
        for directory in self.root.iterdir():
            if RUN_ID.fullmatch(directory.name) and not directory.is_symlink():
                try:
                    rows.append(self.view(directory.name, owner))
                except FileNotFoundError:
                    pass
        return sorted(rows, key=lambda row: row.get("created_at", 0), reverse=True)

    def output_path(self, run_id, step_id, owner):
        directory, state = self._owned(run_id, owner)
        manifest = _read(directory / "manifest.json")
        output = next((row for row in manifest.get("compiled", {}).get("outputs", []) if row.get("id") == step_id), None)
        source = output["source"] if output else step_id
        row = state.get("steps", {}).get(source, {})
        if row.get("status") != "completed" or not row.get("output"):
            raise FileNotFoundError("Workflow output is not ready")
        path = self._local(directory, row["output"]["path"])
        return path, row["output"].get("name", path.name)

    def _local(self, directory, value):
        path = directory / value
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
            raise FileNotFoundError("Workflow artifact is unavailable")
        return path

    def _save(self, directory, state):
        state["updated_at"] = time.time()
        _write(directory / "state.json", state)

    def cancel(self, run_id, owner):
        directory, _ = self._owned(run_id, owner)
        with _locked(directory / "state.lock"):
            state = _read(directory / "state.json")
            if state["status"] in TERMINAL:
                return self._public(directory, state)
            state["cancel_requested"] = True
            state["status"] = "cancelling"
            self._save(directory, state)
        self._start(run_id)
        return self.view(run_id, owner)

    def recover(self):
        if not self.root.is_dir():
            return 0
        count = 0
        for directory in self.root.iterdir():
            if RUN_ID.fullmatch(directory.name) and not directory.is_symlink():
                state = _read(directory / "state.json")
                if state and state.get("status") not in TERMINAL:
                    self._start(directory.name)
                    count += 1
        return count

    def close(self, timeout=3):
        """Stop coordination for shutdown; accepted native jobs remain durable."""
        self._shutdown.set()
        with self._thread_lock:
            threads = list(self._threads.values())
        end = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0, end - time.monotonic()))

    def _start(self, run_id):
        if self._shutdown.is_set():
            return
        with self._thread_lock:
            if run_id in self._threads and self._threads[run_id].is_alive():
                return
            thread = threading.Thread(target=self._coordinate, args=(run_id,), daemon=True,
                                      name=f"workflow-{run_id[:8]}")
            self._threads[run_id] = thread
            thread.start()

    def _coordinate(self, run_id):
        directory = self._directory(run_id)
        try:
            with _locked(directory / "coordinator.lock", blocking=False) as lease:
                if lease is None:
                    return
                self._stop_orphan_cpu(directory)
                while not self._shutdown.is_set():
                    if self._tick(directory):
                        return
                    self._shutdown.wait(self.poll_interval)
        except Exception as exc:
            with _locked(directory / "state.lock"):
                state = _read(directory / "state.json")
                if state and state.get("status") not in TERMINAL:
                    state.update(status="failed", error=self._error(exc), finished_at=time.time())
                    self._save(directory, state)

    @staticmethod
    def _error(exc):
        from src.h3_video import _redact
        return str(_redact(str(exc)))[:1500]

    def _tick(self, directory):
        manifest = _read(directory / "manifest.json")
        plan = manifest["compiled"]["plan"]
        with _locked(directory / "state.lock"):
            state = _read(directory / "state.json")
            if state["status"] in TERMINAL:
                return True
            if state["cancel_requested"]:
                return self._cancel_steps(directory, state)
            state["status"] = "running"
            self._save(directory, state)
        for step in plan:
            if self._shutdown.is_set():
                return False
            with _locked(directory / "state.lock"):
                state = _read(directory / "state.json")
                if state["cancel_requested"]:
                    return False
                row = state["steps"][step["id"]]
                if row["status"] in STEP_TERMINAL:
                    continue
                dependencies = [state["steps"][key] for key in step.get("dependencies", [])]
                if any(dep["status"] in {"failed", "blocked", "cancelled"} for dep in dependencies):
                    row.update(status="blocked", error="An upstream step did not complete", finished_at=time.time())
                    self._save(directory, state)
                    continue
                if any(dep["status"] != "completed" for dep in dependencies):
                    continue
                try:
                    if step["family"] != "media":
                        if not row.get("submission_confirmed"):
                            # Keep cancellation and launch publication atomic.
                            key = uuid.uuid5(uuid.UUID(state["id"]), step["id"]).hex
                            row["job_id"] = hashlib.sha256(f"graph-{step['family']}\0{state['owner']}\0{key}".encode()).hexdigest()[:32]
                            row["submission_id"] = key
                            self._save(directory, state)
                            job = asyncio.run(self._submit_native(directory, state, manifest, step))
                            row.update(job_id=job["id"], submission_confirmed=True, started_at=time.time(), status="queued")
                            self._save(directory, state)
                        self._observe_native(directory, state, step)
                        self._save(directory, state)
                        continue
                    row.update(status="running", started_at=row.get("started_at", time.time()))
                    self._save(directory, state)
                except Exception as exc:
                    row.update(status="failed", error=self._error(exc), finished_at=time.time())
                    self._save(directory, state)
                    continue
            # CPU processing releases the state lock so Stop remains responsive.
            try:
                output = self._cpu(directory, state, manifest, step)
                with _locked(directory / "state.lock"):
                    state = _read(directory / "state.json")
                    row = state["steps"][step["id"]]
                    row.update(status="cancelled" if state["cancel_requested"] else "completed",
                               output=output, finished_at=time.time())
                    self._save(directory, state)
            except _Cancelled:
                return False
            except Exception as exc:
                with _locked(directory / "state.lock"):
                    state = _read(directory / "state.json")
                    state["steps"][step["id"]].update(status="failed", error=self._error(exc), finished_at=time.time())
                    self._save(directory, state)
        with _locked(directory / "state.lock"):
            state = _read(directory / "state.json")
            if state["cancel_requested"]:
                return False
            if all(row["status"] in STEP_TERMINAL for row in state["steps"].values()):
                failed = any(row["status"] != "completed" for row in state["steps"].values())
                state.update(status="failed" if failed else "completed", finished_at=time.time())
                self._save(directory, state)
                return True
        return False

    def _cancel_steps(self, directory, state):
        errors = []
        for row in state["steps"].values():
            if row["status"] in STEP_TERMINAL:
                continue
            try:
                if row.get("job_id"):
                    try:
                        self.managers[row["family"]].cancel(row["job_id"], state["owner"])
                    except FileNotFoundError:
                        # The immutable submission ID was recorded before a
                        # crash/failed validation could publish a native job.
                        pass
                row.update(status="cancelled", finished_at=time.time())
            except Exception as exc:
                row["error"] = self._error(exc)
                errors.append(row["error"])
        if not errors:
            state.update(status="cancelled", finished_at=time.time())
        else:
            state["error"] = "Stopping linked jobs: " + "; ".join(errors)
        self._save(directory, state)
        return not errors

    def _inputs(self, directory, state, manifest, step):
        result = {}
        for field, refs in step.get("inputs", {}).items():
            result[field] = []
            for ref in refs:
                rows = manifest["media"].get(ref)
                if rows is None:
                    upstream = state["steps"].get(ref, {})
                    rows = [upstream["output"]] if upstream.get("output") else []
                if not rows:
                    raise ValueError("A connected workflow input has no saved output")
                for row in rows:
                    result[field].append({"path": self._local(directory, row["path"]), "name": row["name"]})
        return result

    def validate_plan(self, compiled, inventories):
        """Check connected native settings before clients upload any media.

        Each connected media source counts as one prospective file here. Run
        acceptance still validates the actual staged files and their counts.
        """
        result = _plain(compiled)
        if not result.get("valid"):
            return result
        for step in result.get("plan", []):
            if step.get("operation") != "submit" or step.get("family") not in {"h3", "bfs"}:
                continue
            uploads = {field: [None] * len(refs) for field, refs in step.get("inputs", {}).items()}
            try:
                self._validate_native(step, uploads, inventory=inventories[step["family"]])
            except ValueError as exc:
                result.setdefault("issues", []).append({
                    "severity": "error", "code": "generation_settings", "message": str(exc),
                    "node_id": step["id"], "node_type": step.get("node_type"),
                })
                result["valid"] = False
        return result

    def _validate_native(self, step, uploads, *, inventory=None):
        family = step["family"]
        manager = self.managers[family]
        if inventory is None:
            inventory = manager.submission_inventory()
        if family == "comfy":
            return manager.validate_config(step["config"], uploads)
        if family == "h3":
            from src.h3_video import validate_config
            from src.h3_seed_hunter import preflight
            config = validate_config(step["config"], inventory["components"], inventory["gpus"], uploads)
            if config.get("recipe") == "seed_hunter" and config["seed_hunter"]["stage"] == "final":
                raise ValueError("Connect refinement to a saved preview instead of submitting a final job")
            preflight(config, inventory, uploads)
            if not inventory.get("runtime_ready"):
                raise ValueError(inventory.get("runtime_error") or "H3 runtime is unavailable")
        else:
            from src.bfs_video import validate_config
            config = validate_config(step["config"], inventory, uploads)
        return config

    async def _submit_native(self, directory, state, manifest, step):
        from src.video_submission import submission, launch_job, complete_operation, known_input_bytes
        from src.upload_limits import get_chat_upload_max_bytes
        family, owner = step["family"], state["owner"]
        manager = self.managers[family]
        key = uuid.uuid5(uuid.UUID(state["id"]), step["id"]).hex
        scope = f"graph-{family}"
        async with submission(manager, owner, scope, key) as pending:
            existing = pending.existing()
            if existing is not None:
                return existing
            if step["operation"] == "refine":
                from src.h3_seed_hunter import prepare_final
                source = state["steps"][step["depends_on"]]["job_id"]
                body = {"candidate": step["candidate"], "request_id": key, "config": step["config"]}
                target = pending.stage()
                config, saved = await complete_operation(prepare_final, manager, source, owner, body, target, key)
                return await launch_job(manager, target, owner, config, saved)
            sources = self._inputs(directory, state, manifest, step)
            opened = []
            try:
                uploads = {}
                for field, files in sources.items():
                    uploads[field] = []
                    for item in files:
                        upload = UploadFile(item["path"].open("rb"), filename=item["name"], size=item["path"].stat().st_size)
                        opened.append(upload)
                        uploads[field].append(upload)
                config = self._validate_native(step, uploads)
                limit = get_chat_upload_max_bytes()
                if known_input_bytes(uploads) > limit:
                    raise ValueError("Workflow inputs exceed the per-job upload limit")
                if family == "h3":
                    from src.h3_vfx import normalize_vfx_inputs
                    from routes.h3_video_routes import _store_uploads as store_uploads
                    uploads = normalize_vfx_inputs(config, uploads)
                elif family == "bfs":
                    from routes.bfs_video_routes import store_uploads
                else:
                    store_uploads = self._store_comfy_uploads
                target = pending.stage()
                pending.save_metadata(uploads, family)
                saved = await store_uploads(target, uploads, limit)
                return await launch_job(manager, target, owner, config, saved)
            finally:
                for upload in opened:
                    await upload.close()

    @staticmethod
    async def _store_comfy_uploads(directory, uploads, limit):
        from src.video_submission import store_upload_chunk
        saved, total = {}, 0
        for key, files in uploads.items():
            if not re.fullmatch(r"asset_[0-9]+",key):
                raise ValueError("Invalid Comfy media binding")
            paths=[]
            for upload in files:
                extension=Path(upload.filename or "").suffix.lower()
                if extension not in {".png",".jpg",".jpeg",".webp",".bmp",".mp4",".mov",".m4v",".webm",".mkv",".avi",".wav",".mp3",".flac",".m4a",".ogg",".aac"}:
                    raise ValueError("Unsupported Comfy media file")
                path=directory/(uuid.uuid4().hex+extension)
                with path.open("xb") as stream:
                    path.chmod(0o600)
                    while chunk := await upload.read(1024*1024):
                        total += len(chunk)
                        if total > limit: raise ValueError("Workflow inputs exceed the per-job upload limit")
                        await store_upload_chunk(stream,directory,chunk)
                if not path.stat().st_size: raise ValueError("A workflow input is empty")
                if extension in {".png",".jpg",".jpeg",".webp",".bmp"}:
                    from PIL import Image
                    with Image.open(path) as image:
                        if image.width*image.height > 64*1024*1024: raise ValueError("Image exceeds 64 megapixels")
                        image.verify()
                paths.append(str(path.resolve()))
            saved[key]=paths
        return saved

    def _observe_native(self, directory, state, step):
        row = state["steps"][step["id"]]
        manager = self.managers[step["family"]]
        job = manager.view(row["job_id"], state["owner"])
        row["job"] = {key: job.get(key) for key in ("id", "status", "phase", "step", "total_steps", "queue_position", "url", "seed_hunter", "scout") if key in job}
        if job["status"] in {"failed", "stopped"}:
            row.update(status="failed", error=job.get("error") or "The video job was stopped", finished_at=time.time())
        elif job["status"] == "completed":
            if not row.get("output"):
                summary = job.get("seed_hunter") or {}
                candidates = summary.get("candidates") or []
                if summary.get("stage") in {"preview", "dialogue"} and candidates:
                    from src.h3_seed_hunter import artifact
                    source = artifact(manager.directory(job["id"]), candidates[0]["index"], "video")
                    name = f"preview-{candidates[0]['index']}.mp4"
                else:
                    source, name = manager.video_path(job["id"], state["owner"])
                target = directory / ("output-" + hashlib.sha256(step["id"].encode()).hexdigest()[:24] + Path(source).suffix)
                if not target.exists():
                    temporary = target.with_name(target.name + ".copying")
                    temporary.unlink(missing_ok=True)
                    _clone(source, temporary)
                    temporary.replace(target)
                row["output"] = {"path": target.name, "name": _label(name)}
            row.update(status="completed", finished_at=time.time())
        else:
            row["status"] = job["status"]

    @staticmethod
    def _cpu_params(step):
        params = step.get("params", {})
        if not isinstance(params, dict):
            raise ValueError("Invalid media processing settings")
        if step["operation"] == "trim":
            start, end = params.get("start", 0), params.get("end")
            if (type(start) not in (int, float) or not math.isfinite(start) or not 0 <= start <= 86400
                    or end is not None and (type(end) not in (int, float) or not math.isfinite(end) or not start < end <= 86400)):
                raise ValueError("Choose a valid video trim range")
        elif step["operation"] == "resize":
            if any(type(params.get(key)) is not int or not 16 <= params[key] <= 4096 or params[key] % 2 for key in ("width", "height")):
                raise ValueError("Resize dimensions must be even numbers from 16 to 4096")
            if params.get("fit", "contain") not in {"contain", "cover", "stretch"}:
                raise ValueError("Choose contain, cover or stretch for resize")
        return params

    def _cpu(self, directory, state, manifest, step):
        files = self._inputs(directory, state, manifest, step).get("video", [])
        if len(files) != 1:
            raise ValueError("Media processing needs exactly one video")
        source = files[0]["path"]
        params = self._cpu_params(step)
        stem = "output-" + hashlib.sha256(step["id"].encode()).hexdigest()[:24]
        suffix = source.suffix if step["operation"] == "save" else ".mp4"
        target, partial = directory / (stem + suffix), directory / (stem + ".partial" + suffix)
        partial.unlink(missing_ok=True)
        if step["operation"] == "save":
            if not target.exists():
                _clone(source, partial)
                partial.replace(target)
        else:
            from scripts.h3_seed_hunter import ffmpeg_binary
            command = [ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
            if step["operation"] == "trim":
                command += ["-ss", str(params.get("start", 0))]
            command += ["-protocol_whitelist", "file,pipe", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
            if step["operation"] == "trim" and params.get("end") is not None:
                command += ["-t", str(params["end"] - params.get("start", 0))]
            if step["operation"] == "resize":
                width, height, fit = params["width"], params["height"], params.get("fit", "contain")
                scale = f"scale={width}:{height}"
                if fit == "contain":
                    scale += f":force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
                elif fit == "cover":
                    scale += f":force_original_aspect_ratio=increase,crop={width}:{height}"
                command += ["-vf", scale]
            command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", str(partial)]
            log_path = directory / (stem + ".log")
            with log_path.open("wb") as log:
                os.chmod(log_path, 0o600)
                environment = {**os.environ, "ODYSSEUS_WORKFLOW_CPU_RUN": state["id"]}
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log, stdin=subprocess.DEVNULL,
                                           env=environment, start_new_session=True)
                try:
                    while process.poll() is None:
                        if self._shutdown.is_set() or _read(directory / "state.json").get("cancel_requested"):
                            process.terminate()
                            try:
                                process.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait()
                            raise _Cancelled()
                        time.sleep(.1)
                    if process.returncode:
                        raise ValueError("Video processing failed: " + log_path.read_text(errors="replace")[-1000:])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
            if not partial.is_file() or not partial.stat().st_size:
                raise ValueError("Video processing produced no output")
            partial.replace(target)
        name = _label(params.get("name"), _label(files[0]["name"]))
        if Path(name).suffix.lower() != suffix:
            name = Path(name).stem + suffix
        return {"path": target.name, "name": name}

    @staticmethod
    def _stop_orphan_cpu(directory):
        """Recover a crashed server's FFmpeg children before replacing files.

        The coordinator lease proves no live coordinator still owns this run;
        both an inherited marker and a run-local command argument must match.
        Process start identities prevent killing a reused PID.
        """
        from src.h3_video import _snapshot, _stop_supervisor
        marker = ("ODYSSEUS_WORKFLOW_CPU_RUN=" + directory.name).encode()
        for pid, (_, start) in _snapshot().items():
            if pid in {1, os.getpid(), os.getppid()}:
                continue
            try:
                environment = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                if marker in environment and any(arg.startswith(str(directory.resolve()).encode() + b"/") for arg in command):
                    _stop_supervisor({"pid": pid, "start": start})
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
