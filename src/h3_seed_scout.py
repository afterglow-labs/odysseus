"""Owner-scoped selection and preview artifacts for the split-schedule scout."""
from src.h3_video import DEFAULTS as H3_DEFAULTS, _read, _write, _locked, _redact


def defaults(components, gpus):
    """Independent full-schedule Scout setup, without a Hunter/VFX adapter."""
    from scripts.h3_seed_hunter_controls import DEFAULTS
    gpu = next((row for row in gpus if row.get("nvfp4")), next(iter(gpus), None))
    result = {**H3_DEFAULTS, "mode": "ref2va", "recipe": "seed_hunter", "loras": [], "lora": "",
              "latent_upscaler": "", "vae_gpu": "", "seed_hunter": {
                  **DEFAULTS, "stage": "scout", "preview_count": 1, "scout_count": 3,
                  "scout_step": 3, "scout_mode": "interactive", "scout_timeout": 0,
                  "scout_preview_mode": "vae", "shift_enabled": True}}
    if gpu:
        result["gpu"] = gpu["id"]
    for role in ("model", "encoder", "video_vae", "audio_vae"):
        available = [row for row in components if row.get("role") == role
                     and (role != "model" or row.get("variant") == "ref2va")
                     and (not row.get("nvfp4") or gpu and gpu.get("nvfp4"))]
        def rank(row):
            name = row.get("name", "").lower()
            compact = 0 if row.get("nvfp4") else 1 if "int8" in name else 2
            return (compact, "singularity" in name, "pruned" not in name, name)
        result[role] = min(available, key=rank)["id"] if available else ""
    return result


def summary(directory, state=None):
    data = _read(directory / "scout.json")
    if not data:
        config = _read(directory / "manifest.json").get("config", {})
        if not isinstance(config, dict):
            return None
        options = config.get("seed_hunter", {})
        if config.get("recipe") != "seed_hunter" or not isinstance(options, dict) or options.get("stage") != "scout":
            return None
        first_seed, count = config.get("seed"), options.get("scout_count", 3)
        seeds = ([first_seed + index for index in range(count)]
                 if type(first_seed) is int and type(count) is int and 1 <= count <= 32 else [])
        data = {"version": 1, "mode": options.get("scout_mode", "interactive"),
                "status": (state or {}).get("status", "queued"), "seeds": seeds,
                "scout_step": options.get("scout_step"), "total_steps": config.get("steps")}
    result = {key: data.get(key) for key in ("version", "mode", "status", "seeds", "scout_step", "total_steps", "selected_seed")}
    result["job_id"] = directory.name
    result["previews"] = []
    seeds = data.get("seeds", [])
    for row in data.get("previews", []):
        seed = row.get("seed")
        if type(seed) is not int or seed not in seeds:
            continue
        path = directory / f"scout-{seed}.webp"
        if path.is_file() and not path.is_symlink():
            result["previews"].append({"seed": seed, "provisional": bool(row.get("provisional")),
                "url": f"/api/video/h3/jobs/{directory.name}/scout/previews/{seed}", "mime_type": "image/webp"})
    # Worker termination may occur before it can update the scout receipt.
    if state and state.get("status") in {"stopped", "failed"}:
        result["status"] = state["status"]
    elif state and state.get("status") == "completed":
        result["status"] = "scouted" if result["mode"] == "scout" else "completed"
    error = data.get("error") or (state or {}).get("error")
    if result["status"] == "failed" and not error:
        error = _read(directory / "progress.json").get("error")
    if isinstance(error, str) and error:
        result["error"] = _redact(error)[:2000]
    selected = _read(directory / "scout-selection.json").get("seed")
    if type(selected) is int and selected in seeds:
        result["selected_seed"] = selected
        if result["status"] == "waiting_for_selection":
            result["status"] = "continuing"
    return result


def detail(manager, identity, owner):
    state = manager._state(identity, owner)
    value = summary(manager.directory(identity), state)
    if value is None:
        raise FileNotFoundError("SeedScout previews are not available yet")
    return value


def artifact(manager, identity, owner, seed):
    value = detail(manager, identity, owner)
    if type(seed) is not int or seed not in [row["seed"] for row in value["previews"]]:
        raise FileNotFoundError("Seed preview not found")
    path = manager.directory(identity) / f"scout-{seed}.webp"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Seed preview not found")
    return path


def select(manager, identity, owner, seed):
    # Check ownership before taking the same job lock used by cancellation.
    manager._state(identity, owner)
    directory = manager.directory(identity)
    if type(seed) is not int:
        raise ValueError("Choose a seed number from this job")
    with _locked(directory / "job.lock"):
        state = _read(directory / "state.json")
        if state.get("owner") != owner:
            raise FileNotFoundError("Video job not found")
        current = _read(directory / "scout-selection.json")
        if current:
            if current.get("seed") == seed:
                return summary(directory, state)
            raise RuntimeError("A seed was already chosen for this job")
        data = _read(directory / "scout.json")
        if state.get("status") != "running" or data.get("status") != "waiting_for_selection" or (directory / "cancel").exists():
            raise RuntimeError("This job is not waiting for a seed selection")
        if seed not in data.get("seeds", []):
            raise ValueError("Choose one of this job's scouted seeds")
        _write(directory / "scout-selection.json", {"version": 1, "seed": seed})
        return summary(directory, state)
