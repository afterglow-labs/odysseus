"""Local MiniMax H3 jobs, cached component discovery, and restart-safe supervision.

The web process never imports a GPU runtime. Each render has a detached private
Python supervisor and a separate worker session; only their recorded process
identities can be cancelled. No shell commands or caller-supplied paths are used.
"""

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from src.constants import BASE_DIR, COOKBOOK_STATE_FILE, DATA_DIR, GENERATED_IMAGES_DIR
from src.model_artifacts import _safetensors_kind

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(BASE_DIR).resolve()
JOB_ROOT = Path(DATA_DIR) / "video_jobs" / "h3"
RUNTIME_ROOT = PROJECT_ROOT / "runtimes" / "minimax-h3" / "ComfyUI"
ACTIVE = {"queued", "running"}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
MARKER = "ODYSSEUS_H3_JOB"
DEFAULTS = {"mode": "t2va", "width": 960, "height": 544, "frames": 124,
            "steps": 20, "seed": 42, "sampler": "euler", "scheduler": "simple",
            "shift_video": 12.0, "shift_audio": 3.0, "lora_scale": 1.0,
            "reference_size": "match", "gpu": "0"}
UPLOAD_EXTENSIONS = {
    "first_frame": {".jpg", ".jpeg", ".png", ".webp"},
    "last_frame": {".jpg", ".jpeg", ".png", ".webp"},
    "reference_images": {".jpg", ".jpeg", ".png", ".webp"},
    "reference_videos": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "reference_audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus"},
}


def _read(path, default=None):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def _write(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _locked(path):
    import fcntl
    with Path(path).open("a") as stream:
        os.chmod(path, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def _redact(value):
    text = re.sub(r"\bhf_[A-Za-z0-9]+\b", "[redacted]", str(value))
    return re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[redacted]", text)


def _tail(path, limit=8000):
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - limit))
            return _redact(stream.read(limit).decode("utf-8", "replace"))
    except OSError:
        return ""


def cache_roots(project=PROJECT_ROOT, state_file=COOKBOOK_STATE_FILE):
    roots = [Path(project) / "cache/huggingface/hub", Path.home() / ".cache/huggingface/hub",
             Path(project) / "runtimes/minimax-h3/models"]
    state = _read(state_file)
    environment = state.get("env") if isinstance(state.get("env"), dict) else {}
    servers = environment.get("servers") if isinstance(environment.get("servers"), list) else []
    for server in servers:
        if not isinstance(server, dict) or str(server.get("host") or "").strip():
            continue
        entries = server.get("modelDirs", [])
        if not isinstance(entries, list):
            entries = []
        for value in entries + [server.get("modelDir"), server.get("downloadDir")]:
            if isinstance(value, str) and value.strip():
                roots.append(Path(value).expanduser())
    return list(dict.fromkeys(path.resolve() for path in roots if path.is_dir()))


def discover_components(roots):
    components, seen = [], set()
    for base in roots:
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"blobs", "refs"}
                       and not Path(directory, d).is_symlink()]
            for name in sorted(files):
                path = Path(directory, name)
                text = str(path).lower().replace("-", "_")
                if not name.lower().endswith(".safetensors") or not re.search(r"minimax.*h3|(?:/|_)h3(?:/|_)", text):
                    continue
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_file() or resolved.stat().st_size < 16:
                        continue
                    # The same HF blob can exist in overlapping hub roots.
                    # Its content hash identifies one component across both.
                    identity = (resolved.name, name) if "blobs" in resolved.parts and re.fullmatch(r"[a-f0-9]{64}", resolved.name) else resolved
                    if identity in seen:
                        continue
                    seen.add(identity)
                except OSError:
                    continue
                lower = name.lower()
                adapter, _ = _safetensors_kind(path)
                if adapter or "lora" in lower:
                    role = "lora"
                elif "vae" in lower:
                    role = "audio_vae" if "audio" in lower else "video_vae"
                elif "qwen" in lower or "encoder" in lower or "clip" in lower:
                    role = "encoder"
                elif "ref2va" in lower or "fl2va" in lower:
                    role = "model"
                else:
                    continue
                variant = "ref2va" if "ref2va" in lower else "fl2va" if "fl2va" in lower else "shared"
                components.append({"id": hashlib.sha256(str(resolved).encode()).hexdigest()[:32],
                                   "name": name, "path": str(path.absolute()), "role": role,
                                   "variant": variant, "nvfp4": "nvfp4" in lower})
    return sorted(components, key=lambda row: (row["role"], row["name"], row["path"]))


def gpu_inventory():
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=uuid,name,compute_cap",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        if result.returncode:
            return []
        gpus = []
        for line in result.stdout.splitlines():
            parts = [part.strip() for part in line.split(",")]
            if len(parts) == 3 and parts[0].startswith("GPU-"):
                try:
                    blackwell = float(parts[2]) >= 10.0
                except ValueError:
                    blackwell = False
                gpus.append({"id": parts[0], "name": parts[1], "nvfp4": blackwell})
        return gpus
    except (OSError, subprocess.SubprocessError):
        return []


def runtime_error(runtime=RUNTIME_ROOT, worker=None):
    if not sys.platform.startswith("linux"):
        return "Local MiniMax H3 generation currently requires Linux/WSL2."
    worker = Path(worker or PROJECT_ROOT / "scripts/h3_video_worker.py")
    if not worker.is_file() or not (Path(runtime) / "comfy/ldm/minimax").is_dir():
        return "MiniMax H3 runtime is not installed in this Odysseus environment."
    missing = []
    for module in ("torch", "safetensors", "transformers", "av", "comfy_kitchen", "comfy_aimdo"):
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ValueError, ImportError):
            missing.append(module)
    return "Missing private Python dependencies: " + ", ".join(missing) if missing else ""


def validate_config(raw, components, gpus, uploads):
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    allowed = set(DEFAULTS) | {"prompt", "model", "encoder", "video_vae", "audio_vae", "lora"}
    if set(raw) - allowed:
        raise ValueError("Unknown generation setting: " + sorted(set(raw) - allowed)[0])
    config = {**DEFAULTS, **raw}
    if not isinstance(config["mode"], str) or config["mode"] not in {"t2va", "fl2va", "ref2va"}:
        raise ValueError("Choose text, first/last frame, or reference generation")
    prompt = config.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ValueError("A prompt of 1–16,000 characters is required")
    for key, low, high in (("width", 256, 1920), ("height", 256, 1920), ("frames", 124, 362),
                           ("steps", 1, 100), ("seed", 0, 2**63 - 1)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    if config["width"] % 32 or config["height"] % 32 or config["width"] * config["height"] > 768 * 1344:
        raise ValueError("Dimensions must be multiples of 32, up to 1,032,192 pixels")
    if (config["frames"] - 5) % 17:
        raise ValueError("H3 frame count must be 17 × n + 5 (124, 141, 158 … 362)")
    for key, low, high in (("lora_scale", -4, 4), ("shift_video", 0.01, 100), ("shift_audio", 0.01, 100)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a finite number from {low} to {high}")
    if not isinstance(config["sampler"], str) or config["sampler"] not in {"euler", "res_multistep"}:
        raise ValueError("Unsupported H3 sampler")
    if not isinstance(config["scheduler"], str) or config["scheduler"] not in {"simple", "normal"}:
        raise ValueError("Unsupported H3 scheduler")
    if not isinstance(config["reference_size"], str) or config["reference_size"] not in {"match", "max"}:
        raise ValueError("Unsupported reference size")
    gpu = next((g for g in gpus if g["id"] == config["gpu"]), None) if isinstance(config["gpu"], str) else None
    if gpu is None:
        raise ValueError("Choose an available NVIDIA GPU")
    config["gpu"] = gpu["id"]
    by_id = {item["id"]: item for item in components}
    selected = {}
    for role in ("model", "encoder", "video_vae", "audio_vae", "lora"):
        if role == "lora" and config.get(role) in (None, ""):
            config[role] = None
            continue
        item = by_id.get(config.get(role)) if isinstance(config.get(role), str) else None
        if item is None or item["role"] != role or not Path(item["path"]).is_file():
            raise ValueError(f"Choose a cached {role.replace('_', ' ')} from the component list")
        if item.get("nvfp4") and not gpu.get("nvfp4"):
            raise ValueError("NVFP4 components require a Blackwell GPU; select the RTX 5090 or use INT8 weights")
        selected[role] = item
        config[role] = item["path"]
    wanted = "ref2va" if config["mode"] == "ref2va" else "fl2va"
    if selected["model"]["variant"] != wanted:
        raise ValueError(f"{config['mode']} requires a {wanted.upper()} checkpoint")
    counts = {key: len(uploads.get(key, [])) for key in UPLOAD_EXTENSIONS}
    for key, limit in (("first_frame", 1), ("last_frame", 1), ("reference_images", 9),
                       ("reference_videos", 3), ("reference_audio", 3)):
        if counts[key] > limit:
            raise ValueError(f"At most {limit} {key.replace('_', ' ')} allowed")
    reference_count = sum(counts[key] for key in ("reference_images", "reference_videos", "reference_audio"))
    if reference_count > 12:
        raise ValueError("At most 12 reference files are allowed")
    if config["mode"] == "ref2va":
        if not (counts["reference_images"] or counts["reference_videos"]) or counts["first_frame"] or counts["last_frame"]:
            raise ValueError("Reference mode needs an image or video reference and does not accept first/last frames")
    elif config["mode"] == "fl2va":
        if not (counts["first_frame"] or counts["last_frame"]) or reference_count:
            raise ValueError("First/last frame mode needs a first or last image and does not accept reference files")
    elif any(counts.values()):
        raise ValueError("Text mode does not accept attachments; choose first/last frame or reference mode")
    return config


def _snapshot():
    from src.cookbook_stop import _snapshot as snapshot
    return snapshot()


def _live_record(record, snapshot=None):
    current = snapshot if snapshot is not None else _snapshot()
    return bool(record and current.get(int(record.get("pid", 0)), (0, None))[1] == record.get("start"))


def _identity(pid):
    value = _snapshot().get(pid)
    return {"pid": pid, "start": value[1]} if value else None


def _owned_workers(directory):
    """Collect descendants and inherited markers, verifying saved start times."""
    state = _read(directory / "state.json")
    receipt = _read(directory / "processes.json")
    current = _snapshot()
    excluded = {1, os.getpid(), (state.get("supervisor") or {}).get("pid")}
    ancestor = os.getppid()
    while ancestor and ancestor not in excluded:
        excluded.add(ancestor)
        ancestor = current.get(ancestor, (0, ""))[0]
    owned = {int(pid) for pid, start in receipt.items() if current.get(int(pid), (0, None))[1] == start}
    marker = (MARKER + "=" + directory.name).encode()
    for pid in current:
        if pid in excluded:
            continue
        try:
            if marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0"):
                owned.add(pid)
        except (OSError, ProcessLookupError):
            pass
    while True:
        children = {pid for pid, (parent, _) in current.items() if parent in owned and pid not in excluded}
        if children <= owned:
            break
        owned.update(children)
    owned.difference_update(excluded)
    result = {pid: current[pid][1] for pid in owned if pid in current}
    receipt.update({str(pid): start for pid, start in result.items()})
    _write(directory / "processes.json", receipt)
    return result


def stop_workers(directory, grace=1.5):
    directory = Path(directory)
    with _locked(directory / "stop.lock"):
        active = _owned_workers(directory)
        for sig, duration in ((signal.SIGINT, grace), (signal.SIGTERM, grace), (signal.SIGKILL, 2)):
            if not active:
                return True
            deadline = time.monotonic() + duration
            while active and time.monotonic() < deadline:
                snapshot = _snapshot()
                for pid, start in active.items():
                    if snapshot.get(pid, (0, None))[1] == start:
                        try:
                            os.kill(pid, sig)
                        except ProcessLookupError:
                            pass
                time.sleep(0.05)
                active = _owned_workers(directory)
        return not active


def _save_gallery(manifest, state):
    from core.database import GalleryImage, SessionLocal
    source = Path(manifest["output_path"])
    target = Path(manifest["gallery_directory"]) / state["filename"]
    target.parent.mkdir(parents=True, exist_ok=True)
    metadata = {key: value for key, value in manifest["config"].items()
                if key not in {"model", "encoder", "video_vae", "audio_vae", "lora"}}
    metadata["job_id"] = state["id"]
    metadata["lora"] = Path(manifest["config"]["lora"]).name if manifest["config"].get("lora") else None
    with SessionLocal() as db:
        row = db.query(GalleryImage).filter(GalleryImage.id == state["id"]).first()
        if row is None:
            row = GalleryImage(id=state["id"], filename=state["filename"], owner=state["owner"],
                               prompt=manifest["config"]["prompt"], model=Path(manifest["config"]["model"]).name,
                               caption=json.dumps(metadata), size=f"{metadata['width']}x{metadata['height']}",
                               tags="video,minimax-h3,generated", width=metadata["width"], height=metadata["height"],
                               file_size=source.stat().st_size)
            db.add(row)
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        temporary.replace(target)
        db.commit()


class H3JobManager:
    def __init__(self, root=JOB_ROOT, *, project=PROJECT_ROOT, runtime=RUNTIME_ROOT,
                 worker=None, gallery_directory=GENERATED_IMAGES_DIR):
        self.root, self.project, self.runtime = Path(root), Path(project), Path(runtime)
        self.worker = Path(worker or self.project / "scripts/h3_video_worker.py")
        self.gallery_directory = Path(gallery_directory)

    def inventory(self):
        components = discover_components(cache_roots(self.project))
        gpus = gpu_inventory()
        error = runtime_error(self.runtime, self.worker)
        defaults = dict(DEFAULTS)
        if gpus:
            defaults["gpu"] = next((g["id"] for g in gpus if g.get("nvfp4")), gpus[0]["id"])
        return {"components": components, "gpus": gpus, "runtime_ready": not error,
                "runtime_error": error, "defaults": defaults}

    def directory(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise FileNotFoundError("Video job not found")
        return self.root / job_id

    def _state(self, job_id, owner=None):
        directory = self.directory(job_id)
        state = _read(directory / "state.json")
        if not state or (owner is not None and state.get("owner") != owner):
            raise FileNotFoundError("Video job not found")
        if state.get("status") in ACTIVE:
            with _locked(directory / "job.lock"):
                state = _read(directory / "state.json")
                if state.get("status") in ACTIVE and not _live_record(state.get("supervisor")):
                    active = _owned_workers(directory)
                    if active:
                        state["phase"] = "Supervisor interrupted; stop this job before starting another"
                    elif time.time() - state.get("created_at", 0) > 15:
                        state.update(status="failed", error="Render process ended without a completion receipt", phase="failed", finished_at=time.time())
                    _write(directory / "state.json", state)
        return state

    def view(self, job_id, owner):
        state = self._state(job_id, owner)
        directory = self.directory(job_id)
        progress = _read(directory / "progress.json")
        result = {key: state.get(key) for key in ("id", "status", "phase", "error", "filename", "created_at", "finished_at")}
        if state["status"] in ACTIVE:
            result["phase"] = progress.get("phase", result["phase"])
        result.update(step=progress.get("step", 0), total_steps=progress.get("total_steps", state.get("steps", 0)),
                      log_tail=_tail(directory / "render.log"),
                      url=f"/api/video/h3/jobs/{job_id}/video" if state["status"] == "completed" else None)
        if result.get("error"):
            result["error"] = _redact(result["error"])
        return result

    def jobs(self, owner):
        if not self.root.is_dir():
            return []
        result = []
        for directory in self.root.iterdir():
            if directory.is_dir() and JOB_ID.fullmatch(directory.name):
                try:
                    result.append(self.view(directory.name, owner))
                except FileNotFoundError:
                    pass
        return sorted(result, key=lambda row: row["created_at"], reverse=True)

    def stage(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_id = uuid.uuid4().hex
        directory = self.root / job_id
        directory.mkdir(mode=0o700)
        return directory

    def launch(self, directory, owner, config, uploads):
        directory = Path(directory)
        with _locked(self.root / "active.lock"):
            for other in self.root.iterdir():
                if other.is_dir() and JOB_ID.fullmatch(other.name) and (other / "state.json").exists():
                    if self._state(other.name).get("status") in ACTIVE:
                        raise RuntimeError("Another H3 video is rendering. Stop it or wait for completion first.")
            if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
                raise RuntimeError("At least 512 MB of free space is required for a video render")
            state = {"id": directory.name, "owner": owner, "status": "queued", "phase": "starting",
                     "error": None, "filename": directory.name + ".mp4", "created_at": time.time(), "steps": config["steps"]}
            manifest = {"config": config, **uploads, "runtime_path": str(self.runtime.resolve()),
                        "output_path": str((directory / "output.mp4").resolve()),
                        "status_path": str((directory / "progress.json").resolve()),
                        "worker_path": str(self.worker.resolve()), "gallery_directory": str(self.gallery_directory.resolve()),
                        "project_path": str(self.project.resolve())}
            _write(directory / "state.json", state)
            _write(directory / "manifest.json", manifest)
            env = os.environ.copy()
            env[MARKER] = directory.name
            env["PYTHONUNBUFFERED"] = "1"
            try:
                with _locked(directory / "job.lock"), (directory / "render.log").open("ab") as output:
                    process = subprocess.Popen([sys.executable, "-m", "src.h3_video", "--supervise",
                                                str((directory / "manifest.json").resolve())],
                                               cwd=self.project, env=env, stdout=output, stderr=subprocess.STDOUT,
                                               stdin=subprocess.DEVNULL, start_new_session=True)
                    state["supervisor"] = _identity(process.pid)
                    _write(directory / "state.json", state)
            except OSError as exc:
                state.update(status="failed", phase="failed", error="Could not start the private video worker", finished_at=time.time())
                _write(directory / "state.json", state)
                raise RuntimeError(state["error"]) from exc
            threading.Thread(target=process.wait, daemon=True).start()
            return self.view(directory.name, owner)

    def cancel(self, job_id, owner):
        state = self._state(job_id, owner)
        directory = self.directory(job_id)
        if state["status"] not in ACTIVE:
            return self.view(job_id, owner)
        with _locked(directory / "job.lock"):
            (directory / "cancel").touch(mode=0o600)
        if not stop_workers(directory):
            raise RuntimeError("The video worker did not stop. Retry Stop; shutdown has not been confirmed.")
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            if state.get("status") in ACTIVE:
                state.update(status="stopped", phase="stopped", error=None, finished_at=time.time())
                _write(directory / "state.json", state)
        return self.view(job_id, owner)

    def video_path(self, job_id, owner):
        state = self._state(job_id, owner)
        path = self.directory(job_id) / "output.mp4"
        if state["status"] != "completed" or not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Completed video not found")
        return path, state["filename"]


def supervise(manifest_path):
    """Detached supervisor persists completion even when the web server restarts."""
    manifest_path = Path(manifest_path)
    directory = manifest_path.parent
    manifest = _read(manifest_path)
    try:
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            if (directory / "cancel").exists():
                state.update(status="stopped", phase="stopped", finished_at=time.time())
                _write(directory / "state.json", state)
                return
            env = os.environ.copy()
            env[MARKER] = directory.name
            env["CUDA_VISIBLE_DEVICES"] = str(manifest["config"]["gpu"])
            process = subprocess.Popen([sys.executable, manifest["worker_path"], "--job", str(manifest_path)],
                                       cwd=manifest["project_path"], env=env, stdin=subprocess.DEVNULL,
                                       start_new_session=True)
            state.update(status="running", phase="loading model", worker=_identity(process.pid))
            if state["worker"]:
                _write(directory / "processes.json", {str(process.pid): state["worker"]["start"]})
            _write(directory / "state.json", state)
        while process.poll() is None:
            if (directory / "cancel").exists():
                stop_workers(directory)
            time.sleep(0.2)
        # Workers can spawn descendants that outlive the main interpreter.
        # Free the entire recorded render before publishing a terminal state.
        if not stop_workers(directory, grace=0.25):
            raise RuntimeError("A render child process survived shutdown; use Stop to retry")
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            if (directory / "cancel").exists():
                state.update(status="stopped", phase="stopped", error=None)
            elif process.returncode:
                progress = _read(manifest["status_path"])
                state.update(status="failed", phase="failed", error=_redact(progress.get("error") or f"Render exited with code {process.returncode}; see the log"))
            elif not Path(manifest["output_path"]).is_file() or Path(manifest["output_path"]).stat().st_size < 1:
                state.update(status="failed", phase="failed", error="Renderer returned no video file")
            else:
                _save_gallery(manifest, state)
                state.update(status="completed", phase="completed", error=None)
            state["finished_at"] = state.get("finished_at") or time.time()
            _write(directory / "state.json", state)
    except BaseException as exc:
        log.exception("H3 supervisor failed")
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            # If descendants remain, keep the job stoppable and block relaunch.
            alive = bool(_owned_workers(directory))
            state.update(status="running" if alive else "failed", phase="interrupted" if alive else "failed", error=_redact(exc))
            if not alive:
                state["finished_at"] = time.time()
            _write(directory / "state.json", state)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--supervise":
        raise SystemExit("Usage: python -m src.h3_video --supervise manifest.json")
    supervise(sys.argv[2])
