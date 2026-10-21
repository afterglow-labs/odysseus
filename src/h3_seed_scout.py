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
                "url": f"/api/video/h3/jobs/{directory.name}/scout/previews/{seed}", "mime_type": "image/webp",
                "can_resume": all((directory / f"scout-{seed}{ext}").is_file() and not (directory / f"scout-{seed}{ext}").is_symlink() for ext in (".safetensors", ".json"))})
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


def prepare_render(manager, source_id, owner, body, directory, submission_id):
    import uuid
    from pathlib import Path
    from src.h3_loras import frozen_inventory
    from src.h3_video import validate_config, UPLOAD_EXTENSIONS, ACTIVE
    from src.video_job_edit import _inputs, _draft
    from src.video_queue_bulk import _clone_file
    from src.h3_seed_hunter import clone_snapshot, preflight
    manager._state(source_id, owner)
    current = manager.submission_inventory()
    source = manager.directory(source_id)
    seed = body.get('seed')
    with _locked(source / 'job.lock'):
        state = _read(source / 'state.json')
        if state.get('owner') != owner:
            raise FileNotFoundError('Seed Scout job not found')
        value = summary(source, state)
        if state.get('status') in ACTIVE and not (state.get('status') == 'running' and value and value.get('selected_seed') is not None and value.get('status') != 'waiting_for_selection'):
            raise RuntimeError('Choose the first seed or stop the Scout job before rendering another seed')
        if not value or type(seed) is not int or seed not in [r['seed'] for r in value['previews']]:
            raise ValueError('Choose one of this job’s saved seed previews')
        manifest = _read(source / 'manifest.json')
        inventory = frozen_inventory(manifest['config'], current)
        raw = _draft(manifest['config'], inventory, 'h3')
        raw['seed'] = seed
        raw['seed_hunter'].update(scout_mode='final', scout_count=1, preview_count=1)
        inputs = _inputs(source, manifest, UPLOAD_EXTENSIONS)
        config = validate_config(raw, inventory['components'], inventory['gpus'], inputs)
        preflight(config, current, inputs)
        uploads, names = {}, {}
        for field, values in inputs.items():
            paths = []
            for item in values:
                target = directory / (uuid.uuid4().hex + Path(item['path']).suffix)
                _clone_file(item['path'], target)
                paths.append(str(target.resolve()))
            uploads[field] = paths[0] if paths and field in {'first_frame','last_frame','source_video'} else paths
            if values:
                names[field] = [item['name'] for item in values]
        latent, metadata = source / f'scout-{seed}.safetensors', source / f'scout-{seed}.json'
        if latent.is_file() and metadata.is_file():
            uploads['seed_hunter_input'] = clone_snapshot(source, directory,
                {'candidate': seed, 'latent': str(latent), 'metadata': str(metadata)})
        uploads['seed_hunter_source'] = {'source_job_id': source_id, 'source_seed': seed,
            'strategy': 'resume' if 'seed_hunter_input' in uploads else 'full_schedule'}
        _write(directory / 'submission.json', {'submission_id': submission_id, 'input_names': names,
                                              'source_name': manifest.get('source_name')})
        return config, uploads
