"""Named local image recipes with private, detached, owner-scoped jobs.

The API resolves installed components; clients never supply filesystem paths.
Workers share the existing video execution lease so loading an image model
cannot overlap a video render. GPU defaults are stored separately from video.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

from src.constants import BASE_DIR, IMAGE_WORKFLOW_JOBS_DIR, IMAGE_WORKFLOW_DEFAULTS_FILE, IMAGE_WORKFLOW_EXECUTION_LOCK
from src.h3_video import _read, _write, _locked, _identity, _redact, cache_roots, gpu_telemetry

RECIPES = {
    "lustify_inpaint": {
        "name": "LUSTIFY SDXL Inpainting", "description": "Replace the painted area of an image, or generate a new image.",
        "modes": ["inpaint", "generate"], "max_images": 1,
        "defaults": {"mode": "inpaint", "steps": 30, "cfg": 7., "sampler": "dpmpp_2m", "scheduler": "karras"},
        "components": {"checkpoint": "andro-flock/LUSTIFY-SDXL-NSFW-checkpoint-v2-0-INPAINTING/lustifySDXLNSFW_v20-inpainting.safetensors"},
        "encoder": "Both CLIP encoders and the VAE are included in the checkpoint.",
        "adapters": "No additional LoRA is required.",
        "source": "https://huggingface.co/andro-flock/LUSTIFY-SDXL-NSFW-checkpoint-v2-0-INPAINTING",
    },
    "qwen_rapid": {
        "name": "Qwen Rapid Image Edit", "description": "Edit with up to four reference images, or generate from a text prompt.",
        "modes": ["edit", "generate"], "max_images": 4,
        "defaults": {"mode": "edit", "steps": 4, "cfg": 1., "sampler": "euler_ancestral", "scheduler": "beta"},
        "components": {"checkpoint": "Phr00t/Qwen-Image-Edit-Rapid-AIO/v23/Qwen-Rapid-AIO-NSFW-v23.safetensors"},
        "encoder": "The AIO checkpoint includes its Qwen encoder and VAE.",
        "adapters": "Acceleration and the author's adapter mix are already merged into v23 NSFW.",
        "source": "https://huggingface.co/Phr00t/Qwen-Image-Edit-Rapid-AIO",
    },
    "snofs_krea": {
        "name": "SNOFS Krea 2 Turbo", "description": "Generate images with your Krea 2 Turbo v1.4 INT8 checkpoint.",
        "modes": ["generate"], "max_images": 0,
        "defaults": {"mode": "generate", "steps": 8, "cfg": 1., "sampler": "euler", "scheduler": "simple"},
        "components": {
            "model": "snofs_krea_v1_4_TURBO_int8_convrot.safetensors",
            "encoder": "DreamFast/Qwen3-VL-4b-Heretic-ComfyUI/qwen3-vl-4b-heretic_int8.safetensors",
            "vae": "Comfy-Org/Krea-2/vae/qwen_image_vae.safetensors",
        },
        "encoder": "Uses the Qwen3-VL 4B Heretic INT8 text encoder and Qwen Image VAE.",
        "adapters": "SNOFS and Turbo are already merged into this checkpoint. No extra acceleration LoRA is required.",
        "source": "https://civitai.com/models/2416142?modelVersionId=3333138",
    },
}
SAMPLERS = ("euler", "euler_ancestral", "dpmpp_2m", "dpmpp_2m_sde", "er_sde", "sa_solver")
SCHEDULERS = ("simple", "beta", "karras", "normal", "sgm_uniform")
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
GPU = re.compile(r"^(?:[0-9]{1,2}|GPU-[0-9a-fA-F-]{36})$")
ACTIVE = {"queued", "running", "stopping"}


def validate_config(value, default_gpu=""):
    if not isinstance(value, dict) or value.get("recipe") not in RECIPES:
        raise ValueError("Choose an installed image workflow")
    allowed = {"recipe", "mode", "prompt", "negative_prompt", "width", "height", "steps", "cfg", "seed", "gpu", "sampler", "scheduler", "denoise"}
    if set(value) - allowed:
        raise ValueError("Unknown image workflow setting")
    recipe = RECIPES[value["recipe"]]
    result = {"width": 1024, "height": 1024, "seed": 42, "denoise": 1., "negative_prompt": "", **recipe["defaults"], **value}
    if result["mode"] not in recipe["modes"]:
        raise ValueError("This workflow does not support the selected operation")
    for key in ("prompt", "negative_prompt"):
        text = result.get(key, "")
        if not isinstance(text, str) or len(text) > 12000:
            raise ValueError("Prompts must be text of at most 12,000 characters")
        result[key] = text.strip()
    if not result["prompt"]:
        raise ValueError("Enter a prompt")
    for key in ("width", "height"):
        n = result[key]
        if type(n) is not int or n < 256 or n > 2048 or n % 16:
            raise ValueError("Image dimensions must be multiples of 16 from 256 to 2048")
    for key, low, high in (("steps", 1, 100), ("seed", 0, 2**53 - 1)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError(f"{key.title()} must be an integer between {low} and {high}")
    for key, low, high in (("cfg", 0., 20.), ("denoise", .01, 1.)):
        n = result[key]
        if type(n) not in (int, float) or not math.isfinite(n) or not low <= n <= high:
            raise ValueError(f"Invalid {key}")
    if result["sampler"] not in SAMPLERS or result["scheduler"] not in SCHEDULERS:
        raise ValueError("Unsupported image sampler or scheduler")
    result["gpu"] = result.get("gpu") or default_gpu
    if not isinstance(result["gpu"], str) or not GPU.fullmatch(result["gpu"]):
        raise ValueError("Choose an available NVIDIA GPU")
    return result


def validate_inputs(config, images, mask):
    recipe = RECIPES[config["recipe"]]
    if len(images) > recipe["max_images"]:
        raise ValueError(f"This workflow accepts at most {recipe['max_images']} reference images")
    if config["mode"] == "inpaint" and (len(images) != 1 or not mask):
        raise ValueError("Inpainting needs one source image and a mask; white marks the area to replace")
    if config["mode"] == "edit" and not images:
        raise ValueError("Attach at least one image to edit")
    if config["mode"] == "generate" and (images or mask):
        raise ValueError("Choose Edit or Inpaint to use source images")
    if mask and config["mode"] != "inpaint":
        raise ValueError("Masks are only supported by the inpainting workflow")


class ImageJobManager:
    def __init__(self, root=IMAGE_WORKFLOW_JOBS_DIR, *, project=BASE_DIR, roots=None, defaults_file=IMAGE_WORKFLOW_DEFAULTS_FILE, execution_lock=IMAGE_WORKFLOW_EXECUTION_LOCK):
        self.root, self.project = Path(root), Path(project)
        self.roots = roots
        self.defaults_file = Path(defaults_file)
        self.execution_lock = Path(execution_lock)

    def components(self, recipe):
        roots = self.roots if self.roots is not None else cache_roots(self.project)
        result = {}
        for role, relative in RECIPES[recipe]["components"].items():
            for root in roots:
                candidate = Path(root) / relative
                if candidate.is_file() and candidate.stat().st_size > 1024:
                    result[role] = str(candidate.resolve())
                    break
        return result

    def default_gpu(self, devices):
        saved = _read(self.defaults_file).get("gpu", "")
        if saved:
            return saved
        match = next((g for g in devices if "4090" in str(g.get("name", ""))), None)
        device = match or (devices[0] if devices else {})
        return str(device.get("id") or device.get("uuid") or device.get("index", ""))

    def inventory(self):
        telemetry = gpu_telemetry()
        devices = telemetry.get("gpus", [])
        gpu = self.default_gpu(devices)
        rows = []
        for key, recipe in RECIPES.items():
            paths = self.components(key)
            missing = [Path(v).name for k, v in recipe["components"].items() if k not in paths]
            rows.append({k: v for k, v in recipe.items() if k != "components"} | {
                "id": key, "ready": not missing, "missing": missing,
                "components": [{"role": k, "name": Path(v).name, "available": k in paths} for k, v in recipe["components"].items()],
                "defaults": {"width": 1024, "height": 1024, "seed": 42, "denoise": 1., "gpu": gpu, **recipe["defaults"]},
            })
        return {"workflows": rows, "gpus": devices, "default_gpu": gpu, "samplers": SAMPLERS, "schedulers": SCHEDULERS}

    def directory(self, job_id):
        if not JOB_ID.fullmatch(str(job_id)):
            raise FileNotFoundError("Image job not found")
        path = self.root / job_id
        if path.is_symlink():
            raise FileNotFoundError("Image job not found")
        return path

    def state(self, job_id, owner):
        path = self.directory(job_id)
        value = _read(path / "state.json")
        if not value or value.get("owner") != owner:
            raise FileNotFoundError("Image job not found")
        process = _read(path / "process.json")
        if value.get("status") in ACTIVE and process and _identity(process.get("pid", 0)) != process:
            value.update(status="stopped" if (path / "cancel").exists() else "failed", phase="Worker exited", finished_at=time.time())
            _write(path / "state.json", value)
        result = {k: v for k, v in value.items() if k != "owner"}
        result["progress"] = _read(path / "progress.json")
        if value.get("status") not in ACTIVE:
            result["progress"]["phase"] = value.get("phase", value.get("status", ""))
        if value.get("status") == "completed":
            result["image_url"] = f"/api/image-workflows/jobs/{job_id}/image"
        return result

    def jobs(self, owner):
        result = []
        if self.root.is_dir():
            for directory in self.root.iterdir():
                if JOB_ID.fullmatch(directory.name) and directory.is_dir():
                    try:
                        result.append(self.state(directory.name, owner))
                    except FileNotFoundError:
                        pass
        return sorted(result, key=lambda r: r.get("created_at", 0), reverse=True)[:100]

    def submit(self, owner, config, input_directory, images, mask, request_id):
        if not isinstance(owner, str) or not owner:
            raise ValueError("An owner is required")
        try:
            request_id = str(uuid.UUID(request_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("A valid submission ID is required") from exc
        job_id = hashlib.sha256((owner + "\0" + request_id).encode()).hexdigest()[:32]
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _locked(self.root / "submission.lock"):
            directory = self.directory(job_id)
            if (directory / "state.json").is_file():
                return self.state(job_id, owner)
            devices = gpu_telemetry().get("gpus", [])
            config = validate_config(config, self.default_gpu(devices))
            available = {str(g.get("id") or g.get("uuid") or g.get("index")) for g in devices}
            if config["gpu"] not in available:
                raise ValueError("The selected GPU is unavailable; select another GPU explicitly")
            validate_inputs(config, images, mask)
            components = self.components(config["recipe"])
            missing = set(RECIPES[config["recipe"]]["components"]) - set(components)
            if missing:
                raise ValueError("Missing workflow components: " + ", ".join(sorted(missing)))
            directory.mkdir(mode=0o700)
            input_directory = Path(input_directory)
            (input_directory).rename(directory / "inputs")
            image_paths = [str((directory / "inputs" / Path(p).name).resolve()) for p in images]
            mask_path = str((directory / "inputs" / Path(mask).name).resolve()) if mask else None
            _write(directory / "manifest.json", {"id": job_id, "owner": owner, "config": config, "components": components,
                "images": image_paths, "mask": mask_path, "runtime": str(self.project / "runtimes/minimax-h3/ComfyUI"),
                "project": str(self.project), "execution_lock": str(self.execution_lock)})
            _write(directory / "state.json", {"id": job_id, "owner": owner, "status": "queued", "phase": "Waiting for GPU", "config": config, "created_at": time.time()})
            env = os.environ.copy()
            env["CUDA_VISIBLE_DEVICES"] = config["gpu"]
            try:
                with (directory / "worker.log").open("ab") as log:
                    process = subprocess.Popen([sys.executable, str(self.project / "scripts/image_workflow_worker.py"), "--job", str(directory / "manifest.json")],
                        cwd=self.project, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as exc:
                state = _read(directory / "state.json")
                state.update(status="failed", phase="Worker could not start", error=_redact(exc), finished_at=time.time())
                _write(directory / "state.json", state)
                return self.state(job_id, owner)
            identity = _identity(process.pid)
            if identity:
                _write(directory / "process.json", identity)
            return self.state(job_id, owner)

    def stop(self, job_id, owner):
        current = self.state(job_id, owner)
        if current["status"] not in ACTIVE:
            return current
        directory = self.directory(job_id)
        (directory / "cancel").touch(mode=0o600)
        process = _read(directory / "process.json")
        if process and _identity(process.get("pid", 0)) == process:
            try:
                os.kill(process["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        return self.state(job_id, owner)

    def image(self, job_id, owner):
        state = self.state(job_id, owner)
        path = self.directory(job_id) / "output.png"
        if state["status"] != "completed" or not path.is_file() or path.is_symlink():
            raise FileNotFoundError("Completed image not found")
        return path
