"""Local MiniMax H3 jobs, cached component discovery, and restart-safe supervision.

The web process never imports a GPU runtime. Each render has a detached private
Python supervisor and a separate worker session; only their recorded process
identities can be cancelled. No shell commands or caller-supplied paths are used.
"""

from contextlib import contextmanager
import csv
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
from src.hf_cache import effective_hf_cache
from src.h3_vfx import VFX_LORAS, normalize_vfx_inputs
from src.model_artifacts import _safetensors_kind
from scripts.h3_lora_controls import lora_strength_control, lora_strength_bounds, read_profile, validate_lora_profile, built_in_lora_profile

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(BASE_DIR).resolve()
JOB_ROOT = Path(DATA_DIR) / "video_jobs" / "h3"
H3_DEFAULTS_FILE = Path(DATA_DIR) / "h3_video_defaults.json"
VFX_EDIT_LORA = "minimax_h3_vfx_edit_v1.0_r128.safetensors"
RUNTIME_ROOT = PROJECT_ROOT / "runtimes" / "minimax-h3" / "ComfyUI"
ACTIVE = {"queued", "running"}
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
MARKER = "ODYSSEUS_H3_JOB"
SUPERVISOR_CONTROL_VERSION = 1
DEFAULTS = {"mode": "t2va", "width": 960, "height": 544, "frames": 124,
            "steps": 20, "seed": 42, "sampler": "euler", "scheduler": "simple",
            "shift_video": 12.0, "shift_audio": 3.0, "lora_scale": 1.0,
            "reference_size": "match", "gpu": "0", "vae_gpu": ""}
TERMINAL = {"completed", "failed", "stopped"}


class VideoJobChanged(RuntimeError):
    """A reviewed history job changed and must not be deleted by that snapshot."""

_GPU_TELEMETRY_LOCK = threading.Lock()
_GPU_TELEMETRY_CACHE = {"expires": 0., "payload": None}
UPLOAD_EXTENSIONS = {
    "reference_mods": {".safetensors"},
    "source_video": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "first_frame": {".jpg", ".jpeg", ".png", ".webp"},
    "last_frame": {".jpg", ".jpeg", ".png", ".webp"},
    "reference_images": {".jpg", ".jpeg", ".png", ".webp"},
    "reference_videos": {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"},
    "reference_audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".mka", ".aac", ".opus"},
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
def _locked(path, *, blocking=True):
    import fcntl
    with Path(path).open("a") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield None
            return
        yield stream


def _queue_entries(root, *, reconcile=False):
    """Both workflow families share one FIFO, including pre-queue running jobs."""
    root = Path(root)
    roots = [root]
    if root.name in {"h3", "bfs", "comfy"}:
        roots.extend(root.with_name(name) for name in ("h3", "bfs", "comfy") if name != root.name)
    entries = []
    processes = None
    for base in roots:
        if not base.is_dir():
            continue
        manager = H3JobManager(root=base)
        for directory in base.iterdir():
            if not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
                continue
            state = _read(directory / "state.json")
            if state.get("status") not in ACTIVE:
                continue
            if reconcile:
                if processes is None:
                    processes = _snapshot()
                state = manager._state(directory.name, snapshot=processes)
            if state.get("status") in ACTIVE:
                entries.append((directory, state))
    return sorted(entries, key=lambda entry: (
        entry[1].get("queue_order", int(entry[1].get("created_at", 0) * 1_000_000_000)), entry[0].name))


def _has_turn(directory):
    # Called while holding the execution lock, then the submission lock. Never
    # wait for either lock while holding a job lock (cancel takes only job locks).
    with _locked(directory.parent.parent / "video-render.lock"):
        entries = _queue_entries(directory.parent, reconcile=True)
        if any(state["status"] == "running" for _, state in entries):
            return False
        paused = _read(directory.parent.parent / "video-queue-control.json").get("paused_owners", {})
        paused = paused if isinstance(paused, dict) else {}
        eligible = [(path, state) for path, state in entries if not paused.get(str(state.get("owner")), False)]
        return bool(eligible and eligible[0][0] == directory)


def _owner_queue_paused(directory, owner):
    paused = _read(directory.parent.parent / "video-queue-control.json").get("paused_owners", {})
    return bool(isinstance(paused, dict) and paused.get(str(owner), False))


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
    roots = [Path(project) / "models", Path(project) / "cache/huggingface/hub",
             Path(project) / "runtimes/minimax-h3/models"]
    if os.environ.get("ODYSSEUS_H3_MODEL_DIR"):
        roots.insert(0, Path(os.environ["ODYSSEUS_H3_MODEL_DIR"]).expanduser())
    if any(os.environ.get(key) for key in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME")):
        roots.append(Path(effective_hf_cache()))
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
    from src.model_library import component_aliases
    components, seen, known_ids, metadata_cache = [], set(), set(), {}
    for base in roots:
        for directory, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"blobs", "refs", "bfs-shared"}
                       and not Path(directory, d).is_symlink()]
            for name in sorted(files):
                path = Path(directory, name)
                text = str(path).lower().replace("-", "_")
                control = lora_strength_control(name)
                if not name.lower().endswith(".safetensors"):
                    continue
                profile = read_profile(path)
                named_h3 = bool(re.search(r"minimax.*h3|(?:^|[/_])h3(?:[/_.]|$)", text))
                if not (profile or control or named_h3):
                    continue
                adapter, _ = _safetensors_kind(path)
                if not (profile and adapter or control or named_h3):
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
                identity = hashlib.sha256(str(resolved).encode()).hexdigest()[:32]
                aliases = component_aliases(resolved, metadata_cache)
                if identity in known_ids:
                    continue
                known_ids.update([identity, *aliases])
                lower = name.lower()
                if "latent_upscaler" in lower:
                    role = "latent_upscaler"
                elif adapter or "lora" in lower:
                    role = "lora"
                elif "vae" in lower:
                    role = "audio_vae" if "audio" in lower else "video_vae"
                elif "qwen" in lower or "encoder" in lower or "clip" in lower:
                    role = "encoder"
                elif "ref2va" in lower or "fl2va" in lower:
                    role = "model"
                else:
                    continue
                # LightX2V uses ref2v/fl2v in LoRA names, while H3 checkpoints
                # use ref2va/fl2va. They describe the same model families.
                variant = "ref2va" if re.search(r"ref2va?(?:[_.\s-]|$)", lower) else "fl2va" if re.search(r"fl2va?(?:[_.\s-]|$)", lower) else "shared"
                vfx_edit = role == "lora" and lower in VFX_LORAS
                if vfx_edit:
                    variant = "ref2va"
                    if profile is None and adapter:
                        profile = built_in_lora_profile(name)
                if role == "lora" and profile is not None and adapter:
                    variant = profile["variant"]
                    control = profile.get("strength_control") or control
                else:
                    profile = None
                display_name = None
                label_file = path.parent / ".odysseus-h3-labels.json"
                try:
                    labels = json.loads(label_file.read_text()) if not label_file.is_symlink() and label_file.stat().st_size <= 65536 else {}
                    label = labels.get(name) if isinstance(labels, dict) else None
                    if isinstance(label, str) and 0 < len(label.strip()) <= 160 and "\0" not in label:
                        display_name = label.strip()
                except (OSError, ValueError):
                    pass
                components.append({"id": identity, **({"aliases": aliases} if aliases else {}),
                                   "name": name, "path": str(path.absolute()), "role": role,
                                   "variant": variant, "nvfp4": "nvfp4" in lower,
                                   **({"display_name": display_name} if display_name else {}),
                                   **({"strength_control": control} if role == "lora" and control else {}),
                                   **({"lora_profile": profile, "display_name": profile["display_name"] or name} if profile else {}),
                                   **({"recipe": "vfx_edit"} if vfx_edit else {})})
    return sorted(components, key=lambda row: (row["role"], row["name"], row["path"]))


def gpu_inventory():
    from src.nvidia_smi import run_nvidia_smi
    try:
        result = run_nvidia_smi(["--query-gpu=uuid,name,compute_cap",
                                 "--format=csv,noheader,nounits"], timeout=5)
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


def gpu_telemetry():
    """Short-lived read-only driver telemetry; never initialize a CUDA context."""
    from src.nvidia_smi import run_nvidia_smi
    with _GPU_TELEMETRY_LOCK:
        if _GPU_TELEMETRY_CACHE["expires"] > time.monotonic():
            payload = _GPU_TELEMETRY_CACHE["payload"]
        else:
            payload = {"gpus": []}
            try:
                result = run_nvidia_smi([
                    "--query-gpu=uuid,name,memory.used,memory.total,utilization.gpu",
                    "--format=csv,noheader,nounits"], timeout=2)
                if result.returncode:
                    raise RuntimeError("Driver query failed")
                for values in csv.reader(result.stdout.splitlines()):
                    if len(values) != 5:
                        continue
                    identity, name, used, total, utilization = [value.strip() for value in values]
                    if not identity.startswith("GPU-"):
                        continue
                    def number(raw, maximum=None):
                        try:
                            value = float(raw)
                            if not math.isfinite(value) or value < 0 or (maximum is not None and value > maximum):
                                return None
                            return round(value)
                        except ValueError:
                            return None
                    payload["gpus"].append({"id": identity, "name": name,
                                           "memory_used_mib": number(used), "memory_total_mib": number(total),
                                           "utilization_percent": number(utilization, 100)})
                if not payload["gpus"]:
                    payload["error"] = "No NVIDIA GPU telemetry is available"
            except (OSError, subprocess.SubprocessError, RuntimeError):
                payload["error"] = "NVIDIA GPU telemetry is temporarily unavailable"
            _GPU_TELEMETRY_CACHE.update(expires=time.monotonic() + 2., payload=payload)
        # Do not expose mutable cache data to inventory/UI callers.
        return {**payload, "gpus": [dict(row) for row in payload["gpus"]]}


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
    from src.h3_loras import lora_entries
    allowed = set(DEFAULTS) | {"prompt", "model", "encoder", "video_vae", "audio_vae", "lora", "loras", "recipe", "seed_hunter", "latent_upscaler", "reference_video_ranges"}
    if set(raw) - allowed:
        raise ValueError("Unknown generation setting: " + sorted(set(raw) - allowed)[0])
    config = {**DEFAULTS, **raw}
    from scripts.h3_seed_hunter_controls import validate_options, limits, SAMPLERS, SCHEDULERS, scout_upscale_enabled, validate_scout_output
    hunt = config.get("recipe") == "seed_hunter"
    if "recipe" in raw and not hunt or not hunt and ("seed_hunter" in raw or "latent_upscaler" in raw):
        raise ValueError("Unknown H3 recipe")
    options = validate_options(config.get("seed_hunter", {})) if hunt else None
    bounds = limits(options) if hunt else {"min_edge": 256, "max_edge": 1920, "max_pixels": 768 * 1344, "min_frames": 124}
    if hunt:
        config["seed_hunter"] = options
    by_id = {alias: item for item in components for alias in item.get("aliases", [])}
    by_id.update({item["id"]: item for item in components})
    adapters = lora_entries(config, components=by_id)
    vfx_edit = any(row["strength"] != 0 and by_id.get(row["id"], {}).get("name", "").lower() in VFX_LORAS for row in adapters)
    if hunt and vfx_edit:
        raise ValueError("Use the standard H3 VFX workflow for the VFX Edit adapter")
    if "loras" in raw:
        config["lora_scale"] = adapters[0]["strength"] if adapters else 1
    if not isinstance(config["mode"], str) or config["mode"] not in {"t2va", "fl2va", "ref2va"}:
        raise ValueError("Choose text, first/last frame, or reference generation")
    if hunt and config["mode"] != "ref2va":
        raise ValueError("Seed Hunter requires a Ref2VA base model")
    prompt = config.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
        raise ValueError("A prompt of 1–16,000 characters is required")
    for key, low, high in (("width", bounds["min_edge"], bounds["max_edge"]), ("height", bounds["min_edge"], bounds["max_edge"]), ("frames", 73 if vfx_edit else bounds["min_frames"], 362),
                           ("steps", 1, 100), ("seed", 0, 2**63 - 1)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    if hunt and config["seed"] + 1000 * (options["preview_count"] - 1) > 2**53 - 1:
        raise ValueError("Seed Hunter seeds including preview offsets must fit a 53-bit integer")
    if config["width"] % 32 or config["height"] % 32 or config["width"] * config["height"] > bounds["max_pixels"]:
        raise ValueError(f"Dimensions must be multiples of 32, up to {bounds['max_pixels']:,} pixels")
    if (config["frames"] - 5) % 17:
        raise ValueError("H3 frame count must be 17 × n + 5 (124, 141, 158 … 362)")
    if hunt and options["stage"] == "final":
        options["final_frames"] = config["frames"]
    first_lora_name = by_id.get(adapters[0]["id"], {}).get("name", "") if adapters else ""
    first_lora_profile = by_id.get(adapters[0]["id"], {}).get("lora_profile") if adapters else None
    lora_low, lora_high = lora_strength_bounds(first_lora_name, first_lora_profile)
    for key, low, high in (("lora_scale", lora_low, lora_high), ("shift_video", 0.01, 100), ("shift_audio", 0.01, 100)):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{key} must be a finite number from {low} to {high}")
    if not isinstance(config["sampler"], str) or config["sampler"] not in (SAMPLERS if hunt else {"euler", "res_multistep"}):
        raise ValueError("Unsupported H3 sampler")
    if not isinstance(config["scheduler"], str) or config["scheduler"] not in (SCHEDULERS if hunt else {"simple", "normal"}):
        raise ValueError("Unsupported H3 scheduler")
    if not isinstance(config["reference_size"], str) or config["reference_size"] not in {"match", "max"}:
        raise ValueError("Unsupported reference size")
    gpu = next((g for g in gpus if g["id"] == config["gpu"]), None) if isinstance(config["gpu"], str) else None
    if gpu is None:
        raise ValueError("Choose an available NVIDIA GPU")
    config["gpu"] = gpu["id"]
    vae_gpu = config["vae_gpu"]
    if not isinstance(vae_gpu, str) or (vae_gpu and not any(g["id"] == vae_gpu for g in gpus)):
        raise ValueError("Choose an available NVIDIA GPU for the VAEs, or use the model GPU")
    selected = {}
    for role in ("model", "encoder", "video_vae", "audio_vae"):
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
    config["loras"], paths = [], set()
    for row in adapters:
        lora = by_id.get(row["id"])
        if not lora or lora["role"] != "lora" or not Path(lora["path"]).is_file():
            raise ValueError("Choose a cached lora from the component list")
        path = Path(lora["path"]).resolve()
        if path in paths:
            raise ValueError("The same LoRA file cannot be selected more than once")
        paths.add(path)
        if row["strength"] != 0 and lora.get("variant", "shared") not in {"shared", wanted}:
            raise ValueError(f"{lora['name']} is a {lora['variant'].upper()} LoRA; choose a compatible LoRA for {config['mode']} mode")
        config["loras"].append({"path": lora["path"], "strength": row["strength"]})
        config.setdefault("lora_profiles", {})[lora["path"]] = validate_lora_profile(
            lora.get("lora_profile") or {"variant": lora.get("variant", "shared")})
    config["lora"] = config["loras"][0]["path"] if config["loras"] else None
    config["lora_scale"] = config["loras"][0]["strength"] if config["loras"] else 1
    if hunt:
        validate_scout_output(config)
    if hunt and (config.get("latent_upscaler") or options["stage"] == "final" or scout_upscale_enabled(config)):
        upscaler = by_id.get(config.get("latent_upscaler")) if isinstance(config.get("latent_upscaler"), str) else None
        if not upscaler or upscaler["role"] != "latent_upscaler" or not Path(upscaler["path"]).is_file():
            raise ValueError("Choose an installed H3 latent upscaler")
        config["latent_upscaler"] = upscaler["path"]
    uploads = normalize_vfx_inputs(config, uploads)
    counts = {key: len(uploads.get(key, [])) for key in UPLOAD_EXTENSIONS}
    if "reference_video_ranges" in config:
        from scripts.h3_reference_video import validate_ranges
        config["reference_video_ranges"] = validate_ranges(config["reference_video_ranges"], counts["reference_videos"])
    for key, limit in (("source_video", 1), ("first_frame", 1), ("last_frame", 1), ("reference_images", 9),
                       ("reference_videos", 3), ("reference_audio", 3), ("reference_mods", 8)):
        if counts[key] > limit:
            raise ValueError(f"At most {limit} {key.replace('_', ' ')} allowed")
    reference_count = sum(counts[key] for key in ("reference_images", "reference_videos", "reference_audio"))
    if reference_count > 12:
        raise ValueError("At most 12 reference files are allowed")
    if not hunt and counts["reference_mods"]:
        raise ValueError("RefMods require the Seed Hunter workflow")
    if hunt:
        if options["stage"] == "continuation":
            if counts["source_video"] != 1 or config["frames"] <= options["continuation_overlap"]:
                raise ValueError("Continuation needs one source video and a generation longer than its overlap")
        elif counts["source_video"]:
            raise ValueError("A source video belongs to Seed Hunter continuation mode")
        if (options["first_frame_guide"] and not counts["first_frame"] and counts["reference_images"] < 1
                or options["last_frame_guide"] and not counts["last_frame"] and counts["reference_images"] < 2):
            raise ValueError("First/last reference guides require Picture 1 / Picture 2 respectively")
        if counts["first_frame"] and not options["first_frame_guide"] or counts["last_frame"] and not options["last_frame_guide"]:
            raise ValueError("Enable the matching first/last guide for each guide image")
        if options["force_audio"] and not counts["reference_audio"]:
            raise ValueError("Preserve supplied audio requires Audio 1")
        if options["refmod_strengths"] and len(options["refmod_strengths"]) != counts["reference_mods"]:
            raise ValueError("Set one strength per uploaded RefMod")
    if vfx_edit:
        if config["mode"] != "ref2va" or counts["source_video"] != 1 or counts["first_frame"] or counts["last_frame"]:
            raise ValueError("VFX Edit needs exactly one source video and Reference mode. Use reference images for an edited first frame.")
        marker_limits = {"picture": counts["reference_images"], "subject": counts["reference_images"],
                         "video": counts["reference_videos"], "audio": counts["reference_audio"] + counts["reference_videos"]}
        for marker in re.finditer(r"<(Picture|Video|Audio|Subject)\s+(\d+)>", prompt, re.I):
            kind = marker[1].lower()
            available = marker_limits[kind]
            if not 1 <= int(marker[2]) <= available:
                field = {"picture": "Reference images", "subject": "Reference images",
                         "video": "Reference videos", "audio": "Reference audio / video soundtracks"}[kind]
                guidance = (' If you mean Source video, write "the source video" instead.'
                            if kind == "video" else "")
                raise ValueError(f"{marker[0]} has no matching attachment ({field}: {available})."
                                 f" Correct the reference number or attach the matching file.{guidance}")
        prompt = re.sub(r"^(?:vfx_edit:\s*)+", "", prompt.strip(), flags=re.I)
        if not prompt:
            raise ValueError("VFX Edit needs an edit instruction after vfx_edit:")
        config["prompt"] = "vfx_edit: " + prompt
        if len(config["prompt"]) > 16000:
            raise ValueError("The VFX Edit prompt including its prefix must be at most 16000 characters")
    elif counts["source_video"] and not hunt:
        raise ValueError("A source video guide requires an active VFX Edit LoRA")
    if config["mode"] == "ref2va":
        if not (hunt or vfx_edit or counts["reference_images"] or counts["reference_videos"]) or not hunt and (counts["first_frame"] or counts["last_frame"]):
            raise ValueError("Reference mode needs an image or video reference and does not accept first/last frames")
    elif config["mode"] == "fl2va":
        if not (counts["first_frame"] or counts["last_frame"]) or reference_count:
            raise ValueError("First/last frame mode needs a first or last image and does not accept reference files")
    elif any(value for key, value in counts.items() if not (hunt and key in {"source_video", "reference_mods"})):
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


def _verify_waiting_supervisor(directory, previous):
    """Validate an exact recorded waiter before changing its process."""
    current = _snapshot()
    if _live_record(previous, current):
        excluded = {1, os.getpid()}
        ancestor = os.getppid()
        while ancestor and ancestor not in excluded:
            excluded.add(ancestor)
            ancestor = current.get(ancestor, (0, ""))[0]
        if previous["pid"] in excluded:
            raise RuntimeError("Could not verify the queued job's supervisor")
        if sys.platform.startswith("linux"):
            try:
                command = Path(f"/proc/{previous['pid']}/cmdline").read_bytes().split(b"\0")
                expected = [b"-m", b"src.h3_video", b"--supervise", str((directory / "manifest.json").resolve()).encode()]
                if command[1:5] != expected:
                    raise RuntimeError("Could not verify the queued job's supervisor")
            except FileNotFoundError:
                if _live_record(previous):
                    raise RuntimeError("Could not verify the queued job's supervisor")


def _stop_supervisor(record):
    """Stop one verified waiting supervisor, never its process group."""
    if not _live_record(record):
        return
    pid = record["pid"]
    if pid in {1, os.getpid(), os.getppid()}:
        raise RuntimeError("Could not verify the queued job's supervisor")
    handle = None
    try:
        if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
            handle = os.pidfd_open(pid)
        for sig, wait in ((signal.SIGTERM, 1.0), (signal.SIGKILL, 2.0)):
            if not _live_record(record):
                return
            try:
                if handle is not None:
                    signal.pidfd_send_signal(handle, sig)
                else:
                    os.kill(pid, sig)
            except ProcessLookupError:
                return
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                if not _live_record(record):
                    return
                time.sleep(.025)
        raise RuntimeError("Could not update this queued job's supervisor; retry Edit")
    except ProcessLookupError:
        return
    finally:
        if handle is not None:
            os.close(handle)


def _owned_workers(directory, *, snapshot=None):
    """Collect descendants and inherited markers, verifying saved start times."""
    state = _read(directory / "state.json")
    receipt = _read(directory / "processes.json")
    current = _snapshot() if snapshot is None else snapshot
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
    progress = _read(manifest["status_path"])
    for key in ("frames", "fps", "duration", "width", "height", "audio_mode"):
        if key in progress:
            metadata[key] = progress[key]
    metadata["lora"] = Path(manifest["config"]["lora"]).name if manifest["config"].get("lora") else None
    with SessionLocal() as db:
        row = db.query(GalleryImage).filter(GalleryImage.id == state["id"]).first()
        if row is None:
            row = GalleryImage(id=state["id"], filename=state["filename"], owner=state["owner"],
                               prompt=manifest["config"]["prompt"], model=Path(manifest["config"]["model"]).name,
                               caption=json.dumps(metadata), size=f"{metadata['width']}x{metadata['height']}",
                               tags=("video,bfs," + manifest["config"]["family"] + ",generated") if manifest["config"].get("workflow_id") else "video,minimax-h3,generated", width=metadata["width"], height=metadata["height"],
                               file_size=source.stat().st_size)
            db.add(row)
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        temporary.chmod(0o600)
        temporary.replace(target)
        db.commit()


def installed_h3_preset(path, components, gpus, defaults):
    """Resolve an optional local installation preset against this exact scan.

    Only new drafts consume these defaults. Submission/edit/rerun validation
    never merges them, so installing a LoRA cannot rewrite existing jobs.
    """
    path = Path(path)
    if not path.is_file():
        return None, ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or set(document) != {"format_version", "id", "name", "config"}:
            raise ValueError("Expected format_version, id, name, and config")
        if type(document["format_version"]) is not int or document["format_version"] != 1:
            raise ValueError("Unsupported preset format")
        if not isinstance(document["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", document["id"]):
            raise ValueError("Invalid preset id")
        if not isinstance(document["name"], str) or not document["name"].strip() or len(document["name"]) > 160:
            raise ValueError("Invalid preset name")
        values = document["config"]
        roles = {"model", "encoder", "video_vae", "audio_vae", "lora"}
        allowed = roles | {"mode", "steps", "sampler", "scheduler", "shift_video", "shift_audio", "lora_scale", "loras"}
        if (not isinstance(values, dict) or set(values) - allowed or not {"mode", "model"} <= set(values)
                or not {"lora", "loras"} & set(values)):
            raise ValueError("Preset requires mode, model, and lora, with recognized settings only")
        resolved = dict(values)
        for role in roles & set(values):
            value = values[role]
            if not isinstance(value, str) or not value:
                raise ValueError(f"Invalid {role} selection")
            value_id = hashlib.sha256(str(Path(value).expanduser().resolve()).encode()).hexdigest()[:32]
            matches = [item for item in components if item["role"] == role and
                       (item["id"] == value or value in item.get("aliases", [])
                        or value_id in item.get("aliases", [])
                        or Path(item["path"]).resolve() == Path(value).expanduser().resolve())]
            if len(matches) != 1:
                raise ValueError(f"Installed {role.replace('_', ' ')} was not found in the cached component scan")
            resolved[role] = matches[0]["id"]
        if "loras" in values:
            from src.h3_loras import lora_entries
            resolved["loras"] = []
            available = {identity: item for item in components for identity in [item["id"], *item.get("aliases", [])]}
            for row in lora_entries(values, components=available):
                value = row["id"]
                value_id = hashlib.sha256(str(Path(value).expanduser().resolve()).encode()).hexdigest()[:32]
                matches = [item for item in components if item["role"] == "lora" and
                           (item["id"] == value or value in item.get("aliases", [])
                            or value_id in item.get("aliases", [])
                            or Path(item["path"]).resolve() == Path(value).expanduser().resolve())]
                if len(matches) != 1:
                    raise ValueError("Installed lora was not found in the cached component scan")
                resolved["loras"].append({"id": matches[0]["id"], "strength": row["strength"]})
        candidate = {**defaults, **resolved, "prompt": "Installed H3 preset validation"}
        for role in roles - {"model", "lora"}:
            if role not in candidate:
                item = next((item for item in components if item["role"] == role), None)
                if item:
                    candidate[role] = item["id"]
        inputs = {"reference_videos": [None]} if candidate["mode"] == "ref2va" else {"first_frame": [None]} if candidate["mode"] == "fl2va" else {}
        validate_config(candidate, components, gpus, inputs)
        revision = hashlib.sha256(json.dumps({"id": document["id"], "config": resolved}, sort_keys=True).encode()).hexdigest()[:24]
        return {"id": document["id"], "name": document["name"], "revision": revision, "config": resolved}, ""
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        return None, f"Installed H3 preset unavailable: {error}"


class H3JobManager:
    output_basename = "output.mp4"
    def __init__(self, root=JOB_ROOT, *, project=PROJECT_ROOT, runtime=RUNTIME_ROOT,
                 worker=None, gallery_directory=GENERATED_IMAGES_DIR, preset_file=H3_DEFAULTS_FILE):
        self.root, self.project, self.runtime = Path(root), Path(project), Path(runtime)
        self.worker = Path(worker or self.project / "scripts/h3_video_worker.py")
        self.gallery_directory = Path(gallery_directory)
        self.preset_file = Path(preset_file)
        self._submission_inventory_lock = threading.Lock()
        self._submission_inventory_cache = None

    def submission_inventory(self):
        """Reuse one short scan across a burst of serial batch uploads."""
        with self._submission_inventory_lock:
            cached = self._submission_inventory_cache
            if cached is None or cached[0] <= time.monotonic():
                inventory = self.inventory()
                cached = (time.monotonic() + 10, inventory)
                self._submission_inventory_cache = cached
            return cached[1]

    def inventory(self):
        from src.h3_loras import MAX_LORAS
        components = discover_components(cache_roots(self.project))
        gpus = gpu_inventory()
        error = runtime_error(self.runtime, self.worker)
        defaults = dict(DEFAULTS)
        if gpus:
            defaults["gpu"] = next((g["id"] for g in gpus if g.get("nvfp4")), gpus[0]["id"])
        preset, preset_error = installed_h3_preset(self.preset_file, components, gpus, defaults)
        from src.h3_seed_hunter import capabilities
        return {"components": components, "gpus": gpus, "runtime_ready": not error, "batch_jobs": True,
                "seed_hunter": capabilities(self.project, self.runtime, components, gpus),
                "batch_auto_video_length": True, "reference_video_ranges": True,
                "server_video_files": True,
                "lora_stack": True, "lora_library": True, "max_loras": MAX_LORAS, "vfx_references": True,
                "runtime_error": error, "defaults": {**defaults, **(preset["config"] if preset else {})},
                "base_defaults": defaults, "installed_preset": preset, "installed_preset_error": preset_error}

    def directory(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise FileNotFoundError("Video job not found")
        directory = self.root / job_id
        if directory.is_symlink():
            raise FileNotFoundError("Video job not found")
        return directory

    def _state(self, job_id, owner=None, *, snapshot=None):
        directory = self.directory(job_id)
        state = _read(directory / "state.json")
        if not state or (owner is not None and state.get("owner") != owner):
            raise FileNotFoundError("Video job not found")
        if state.get("status") in ACTIVE:
            with _locked(directory / "job.lock"):
                state = _read(directory / "state.json")
                if state.get("status") in ACTIVE and not _live_record(state.get("supervisor"), snapshot):
                    active = (_owned_workers(directory) if snapshot is None
                              else _owned_workers(directory, snapshot=snapshot))
                    if active:
                        state.update(status="running", phase="Supervisor interrupted; stop this job to continue the queue")
                    elif time.time() - state.get("created_at", 0) > 15:
                        state.update(status="failed", error="Render process ended without a completion receipt", phase="failed", finished_at=time.time())
                    _write(directory / "state.json", state)
        return state

    def view(self, job_id, owner, *, queue_positions=None, snapshot=None):
        state = self._state(job_id, owner, snapshot=snapshot)
        directory = self.directory(job_id)
        # Read the current atomic launch snapshot after verifying ownership.
        # Older/interrupted jobs may not have a usable manifest.
        manifest = _read(directory / "manifest.json")
        config = manifest.get("config")
        prompt = config.get("prompt") if isinstance(config, dict) else None
        progress = _read(directory / "progress.json")
        result = {key: state.get(key) for key in ("id", "status", "phase", "error", "filename", "created_at", "started_at", "finished_at")}
        source_name = manifest.get("source_name", state.get("source_name"))
        result["source_name"] = source_name if isinstance(source_name, str) else None
        result["revision"] = manifest.get("revision", 0)
        result["submission_id"] = state.get("submission_id") if isinstance(state.get("submission_id"), str) else None
        length = manifest.get("batch_video_length")
        if manifest.get("revision", 0) == 0 and isinstance(length, dict) and isinstance(config, dict):
            length_frames = (config.get("seed_hunter", {}).get("final_frames")
                             if length.get("target") == "final" else config.get("frames"))
            if length.get("frames") == length_frames:
                result["batch_video_length"] = length
        if state["status"] == "queued" and queue_positions is None:
            waiting = [path.name for path, row in _queue_entries(self.root) if row["status"] == "queued"]
            queue_positions = {identifier: index + 1 for index, identifier in enumerate(waiting)}
        result["queue_position"] = (queue_positions or {}).get(job_id) if state["status"] == "queued" else None
        result["prompt"] = prompt if isinstance(prompt, str) else None
        result["gpu"] = config.get("gpu") if isinstance(config, dict) and isinstance(config.get("gpu"), str) else None
        result["vae_gpu"] = config.get("vae_gpu", "") if isinstance(config, dict) and isinstance(config.get("vae_gpu", ""), str) else ""
        if state["status"] == "running":
            result["phase"] = progress.get("phase", result["phase"])
        steps = config.get("steps", state.get("steps", 0)) if isinstance(config, dict) else state.get("steps", 0)
        result.update(step=progress.get("step", 0), total_steps=progress.get("total_steps", steps),
                      log_tail=_tail(directory / "render.log"),
                      url=f"/api/video/h3/jobs/{job_id}/video" if state["status"] == "completed" else None)
        if result.get("error"):
            result["error"] = _redact(result["error"])
        if isinstance(config, dict) and config.get("recipe") == "seed_hunter":
            from src.h3_seed_hunter import summary
            from src.video_job_inputs import input_videos
            result["input_videos"] = input_videos(job_id, manifest)
            result.update(recipe="seed_hunter", seed_hunter=summary(directory, config, state))
            if config.get("seed_hunter", {}).get("stage") == "scout":
                from src.h3_seed_scout import summary as scout_summary
                result["scout"] = scout_summary(directory, state)
        return result

    def jobs(self, owner):
        if not self.root.is_dir():
            return []
        # A large batch needs one queue scan and one process enumeration per
        # list response, not one of each for every queued job.
        queued = _queue_entries(self.root)
        waiting = [path.name for path, row in queued if row["status"] == "queued"]
        positions = {identifier: index + 1 for index, identifier in enumerate(waiting)}
        snapshot = _snapshot() if queued else {}
        result = []
        for directory in self.root.iterdir():
            if directory.is_dir() and JOB_ID.fullmatch(directory.name):
                try:
                    result.append(self.view(directory.name, owner, queue_positions=positions, snapshot=snapshot))
                except FileNotFoundError:
                    pass
        return sorted(result, key=lambda row: row["created_at"], reverse=True)

    def stage(self, job_id=None):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_id = job_id or uuid.uuid4().hex
        directory = self.directory(job_id)
        directory.mkdir(mode=0o700)
        return directory

    def launch(self, directory, owner, config, uploads):
        directory = Path(directory)
        # Persist the FIFO receipt before starting a lightweight supervisor. Its
        # worker will acquire the separate execution lock only when it has turn.
        with _locked(self.root.parent / "video-render.lock"):
            if shutil.disk_usage(self.root).free < 512 * 1024 * 1024:
                raise RuntimeError("At least 512 MB of free space is required for a video render")
            previous = _read(self.root.parent / "video-queue.json").get("last_order", 0)
            order = max(time.time_ns(), previous + 1)
            _write(self.root.parent / "video-queue.json", {"last_order": order})
            state = {"id": directory.name, "owner": owner, "status": "queued", "phase": "queued", "queue_order": order,
                     "error": None, "filename": directory.name + Path(self.output_basename).suffix, "created_at": time.time(), "steps": config["steps"],
                     "recipe": config.get("recipe", "h3"),
                     "supervisor_edit_version": 1, "supervisor_control_version": SUPERVISOR_CONTROL_VERSION}
            submission = _read(directory / "submission.json")
            state.update(source_name=submission.get("source_name"), submission_id=submission.get("submission_id"))
            manifest = {"config": config, "revision": 0, **uploads, "runtime_path": str(self.runtime.resolve()),
                        "output_path": str((directory / self.output_basename).resolve()),
                        "status_path": str((directory / "progress.json").resolve()),
                        "worker_path": str(self.worker.resolve()), "gallery_directory": str(self.gallery_directory.resolve()),
                        "project_path": str(self.project.resolve())}
            manifest.update(input_names=submission.get("input_names", {}), source_name=state["source_name"],
                            submission_id=state["submission_id"])
            if isinstance(submission.get("batch_video_length"), dict):
                manifest["batch_video_length"] = submission["batch_video_length"]
            _write(directory / "manifest.json", manifest)
            _write(directory / "state.json", state)
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
        from src.workflow_prompt_history import remember_job_prompts
        remember_job_prompts(self, owner, [config])
        return self.view(directory.name, owner)

    def upgrade_queued_supervisor(self, directory, state):
        """Called only under job.lock, before an edited manifest is published.

        Old waiting supervisors may have imported the manifest-before-lock
        renderer. Replace just this job's waiter. Start the replacement first
        so a spawn failure leaves the original queue receipt and waiter intact.
        Neither waiter can start a worker until this job lock is released.
        """
        if (state.get("supervisor_edit_version", 0) >= 1
                and state.get("supervisor_control_version", 0) >= SUPERVISOR_CONTROL_VERSION):
            return
        if state.get("status") != "queued" or state.get("worker") or _owned_workers(directory):
            raise RuntimeError("This job has started rendering and cannot be edited")
        previous = state.get("supervisor")
        _verify_waiting_supervisor(directory, previous)
        env = os.environ.copy()
        env[MARKER], env["PYTHONUNBUFFERED"] = directory.name, "1"
        try:
            with (directory / "render.log").open("ab") as output:
                process = subprocess.Popen([sys.executable, "-m", "src.h3_video", "--supervise",
                                            str((directory / "manifest.json").resolve())],
                                           cwd=self.project, env=env, stdout=output, stderr=subprocess.STDOUT,
                                           stdin=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise RuntimeError("Could not prepare the queued job for editing; its original settings are unchanged") from exc
        replacement = _identity(process.pid)
        if replacement is None:
            process.wait(timeout=3)
            raise RuntimeError("The updated video supervisor ended before the edit was saved")
        try:
            _stop_supervisor(previous)
        except BaseException:
            _stop_supervisor(replacement)
            process.wait(timeout=3)
            raise
        state.update(supervisor=replacement, supervisor_edit_version=1,
                     supervisor_control_version=SUPERVISOR_CONTROL_VERSION)
        _write(directory / "state.json", state)
        threading.Thread(target=process.wait, daemon=True).start()

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

    def delete(self, job_id, owner, *, expected_revision=None):
        """Remove only a terminal owned job whose complete process tree exited.

        Submission and job locks serialize deletion with queue/cancel operations.
        Keep the state receipt until the last filesystem operation so an I/O
        failure leaves an owner-authorized record that can be retried safely.
        """
        self._state(job_id, owner)
        directory = self.directory(job_id)
        with _locked(self.root.parent / "video-render.lock"):
            with _locked(directory / "job.lock"):
                state = _read(directory / "state.json")
                if not state or state.get("owner") != owner:
                    raise FileNotFoundError("Video job not found")
                manifest = _read(directory / "manifest.json")
                if expected_revision is not None:
                    from src.video_job_edit import revision
                    if revision(manifest) != expected_revision:
                        raise VideoJobChanged("This job was edited after confirmation; it was left unchanged")
                    if state.get("status") not in TERMINAL:
                        raise VideoJobChanged("This job is now queued or running; it was left unchanged")
                if state.get("status") not in TERMINAL:
                    raise RuntimeError("Stop the running or queued job before deleting it")
                if _live_record(state.get("supervisor")) or _owned_workers(directory):
                    raise RuntimeError("The video process is still exiting; wait for it to stop before deleting this job")
                expected = job_id + ".mp4"
                if state.get("id") != job_id or state.get("filename") != expected:
                    raise RuntimeError("The saved video job has inconsistent output metadata")
                self._delete_gallery(job_id, owner, expected)
                from src.video_job_inputs import delete_input_video_previews
                delete_input_video_previews(self, job_id)
                try:
                    for entry in directory.iterdir():
                        if entry.name in {"state.json", "manifest.json", "job.lock"}:
                            continue
                        if entry.is_dir() and not entry.is_symlink():
                            shutil.rmtree(entry)
                        else:
                            entry.unlink(missing_ok=True)
                    (directory / "manifest.json").unlink(missing_ok=True)
                    (directory / "state.json").unlink()
                    (directory / "job.lock").unlink()
                    directory.rmdir()
                except OSError as exc:
                    # Restore the receipt if final removal failed; a following
                    # request can retry, and cannot reinterpret it as active.
                    if directory.is_dir() and not (directory / "state.json").exists():
                        _write(directory / "state.json", state)
                    if directory.is_dir() and manifest and not (directory / "manifest.json").exists():
                        _write(directory / "manifest.json", manifest)
                    raise RuntimeError("Could not remove all video job files; retry Delete") from exc
        return {"deleted": True, "id": job_id}

    def _delete_gallery(self, job_id, owner, filename):
        from core.database import GalleryAlbum, GalleryImage, SessionLocal
        try:
            with SessionLocal() as db:
                row = db.query(GalleryImage).filter(GalleryImage.id == job_id).first()
                collision = db.query(GalleryImage).filter(GalleryImage.filename == filename).first()
                if (row is not None and (row.owner != owner or row.filename != filename)) or (collision is not None and collision.id != job_id):
                    raise RuntimeError("The Gallery output does not match this video's owner and job")
                if row is not None:
                    db.query(GalleryAlbum).filter(GalleryAlbum.owner == owner, GalleryAlbum.cover_id == job_id).update(
                        {GalleryAlbum.cover_id: None}, synchronize_session=False)
                    db.delete(row)
                    db.commit()
            # Only this manager's configured gallery directory is trusted, never
            # a path from a manifest. unlink removes a symlink, not its target.
            (self.gallery_directory / filename).unlink(missing_ok=True)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Could not remove the saved Gallery output; retry Delete") from exc

    def video_path(self, job_id, owner):
        state = self._state(job_id, owner)
        path = self.directory(job_id) / "output.mp4"
        if state["status"] != "completed" or not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Completed video not found")
        return path, state["filename"]


def _render(manifest_path, execution_lock):
    directory = manifest_path.parent
    try:
        # Dispatch commits under the same lock used by pause and bulk changes.
        # The execution lease is already held; never acquire it while holding a
        # submission or job lock. Release the submission lock after spawning so
        # queue controls remain responsive throughout a long render.
        with _locked(directory.parent.parent / "video-render.lock"), _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            if state.get("status") not in ACTIVE or (directory / "cancel").exists():
                if state.get("status") in ACTIVE:
                    state.update(status="stopped", phase="stopped", finished_at=time.time())
                    _write(directory / "state.json", state)
                return
            if _owner_queue_paused(directory, state.get("owner")):
                return False
            manifest = _read(manifest_path)
            env = os.environ.copy()
            env[MARKER] = directory.name
            env["CUDA_VISIBLE_DEVICES"] = str(manifest["config"]["gpu"])
            process = subprocess.Popen([sys.executable, manifest["worker_path"], "--job", str(manifest_path)],
                                       cwd=manifest["project_path"], env=env, stdin=subprocess.DEVNULL,
                                       start_new_session=True, pass_fds=(execution_lock.fileno(),))
            state.update(status="running", phase="loading model", started_at=time.time(), worker=_identity(process.pid))
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
                if manifest.get("config", {}).get("recipe") != "comfy_graph":
                    _save_gallery(manifest, state)
                state.update(status="completed", phase="completed", error=None)
            state["finished_at"] = state.get("finished_at") or time.time()
            _write(directory / "state.json", state)
    except BaseException as exc:
        log.exception("H3 supervisor failed")
        # Even if bookkeeping fails after spawning, do not hand the execution
        # slot to another job until shutdown has been checked. Surviving owned
        # descendants remain recorded as running and block every waiting job.
        stop_workers(directory, grace=0.25)
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            # If descendants remain, keep the job stoppable and block relaunch.
            alive = bool(_owned_workers(directory))
            state.update(status="running" if alive else "failed", phase="interrupted" if alive else "failed", error=_redact(exc))
            if not alive:
                state["finished_at"] = time.time()
            _write(directory / "state.json", state)


def supervise(manifest_path):
    """Wait durably without loading models, then run one job with exclusive VRAM."""
    manifest_path = Path(manifest_path).resolve()
    directory = manifest_path.parent
    while True:
        with _locked(directory / "job.lock"):
            state = _read(directory / "state.json")
            if state.get("status") not in ACTIVE:
                return
            if (directory / "cancel").exists():
                state.update(status="stopped", phase="stopped", error=None, finished_at=time.time())
                _write(directory / "state.json", state)
                return
        with _locked(directory.parent.parent / "video-execution.lock", blocking=False) as lease:
            if lease is not None and _has_turn(directory):
                if _render(manifest_path, lease) is not False:
                    return
        time.sleep(0.5)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--supervise":
        raise SystemExit("Usage: python -m src.h3_video --supervise manifest.json")
    supervise(sys.argv[2])
