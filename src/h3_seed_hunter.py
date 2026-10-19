"""Owner-scoped saved candidates and explicit refinement for H3 Seed Hunter."""
import uuid
from pathlib import Path

from scripts.h3_seed_hunter_controls import DEFAULTS, SAMPLERS, SCHEDULERS, scout_upscale_enabled, scout_output_size
from src.h3_video import ACTIVE, _read, _write, _locked


def capabilities(project, runtime, components, gpus=()):
    from src.h3_seed_scout import defaults as scout_defaults
    project, runtime = Path(project), Path(runtime)
    extras = project / "runtimes/minimax-h3/seed-hunter"
    defaults = {"mode": "ref2va", "width": 480, "height": 864, "frames": 73, "steps": 6,
                "seed": 475620997637978, "sampler": "er_sde", "scheduler": "beta",
                "shift_video": 12, "shift_audio": 6, "seed_hunter": dict(DEFAULTS)}
    authored = next((c for c in components if c.get("role") == "model" and "singularity" in c.get("name", "").lower()
                     and c.get("variant") == "ref2va" and "pruned" in c.get("name", "").lower()), None)
    if authored:
        defaults["model"] = authored["id"]
    main = next((gpu for gpu in gpus if gpu.get("nvfp4")), None)
    vae = next((gpu for gpu in gpus if "4090" in gpu.get("name", "") and gpu is not main), None)
    if main:
        defaults["gpu"] = main["id"]
    if vae:
        defaults["vae_gpu"] = vae["id"]
    return {
        "version": 1, "stages": ["preview", "single", "continuation", "dialogue"],
        "samplers": list(SAMPLERS), "schedulers": list(SCHEDULERS),
        "defaults": defaults,
        "scout_defaults": scout_defaults(components, gpus),
        "final_defaults": {"megapixels": 2, "steps": 2, "sampler": "er_sde",
                           "scheduler": "linear_quadratic", "denoise": .4},
        "features": {"latent_upscaler": (extras / "latent-upscaler").is_dir()
                     and any(c.get("role") == "latent_upscaler" for c in components),
                     "scout_upscale": (extras / "latent-upscaler/nodes/minimax_h3_latent_upscaler_3d.py").is_file()
                     and any(c.get("role") == "latent_upscaler" for c in components),
                     "guides": True, "force_audio": True, "continuation": True, "dialogue": True,
                     "separate_final_length": True,
                     "seed_scout": (runtime.parent / "seed-scout/nodes.py").is_file(),
                     "attention_backend": True, "sparse_attention": True,
                     "refmods": (extras / "refmods/core.py").is_file(),
                     "rife": (runtime / "comfy_extras/nodes_frame_interpolation.py").is_file()
                     and (project / "models/hzwer/RIFE/rife-v4.26.safetensors").is_file(),
                     "ff_chunking": (extras / "kjnodes/nodes/minimax_nodes.py").is_file(),
                     "low_vram_attention": (extras / "kjnodes/nodes/minimax_nodes.py").is_file()},
    }


def preflight(config, inventory, inputs):
    if config.get("recipe") != "seed_hunter":
        return
    features = inventory.get("seed_hunter", {}).get("features", {})
    options = config["seed_hunter"]
    wanted = {"latent_upscaler": options["stage"] == "final" or scout_upscale_enabled(config),
              "scout_upscale": scout_upscale_enabled(config),
              "seed_scout": options["stage"] == "scout",
              "refmods": bool(inputs.get("reference_mods")), "rife": options["output_fps"] == 48,
              "ff_chunking": bool(options["ff_chunk_size"]),
              "low_vram_attention": bool(options["low_vram_chunks"]),
              "sparse_attention": options["sparse_method"] != "none"}
    for key, required in wanted.items():
        if required and not features.get(key):
            raise ValueError(f"Seed Hunter {key.replace('_', ' ')} is not installed on this server")
    if options["stage"] == "scout":
        if options["scout_mode"] != "final" and options["scout_step"] >= config["steps"]:
            raise ValueError("Scout step must be before the last step of the full schedule")
        if config["seed"] + options["scout_count"] - 1 > 2**53 - 1:
            raise ValueError("Scout seeds must fit a 53-bit integer")


def artifact(directory, candidate, kind):
    if type(candidate) is not int or not 1 <= candidate <= 3:
        raise ValueError("Choose preview 1, 2, or 3")
    suffix = {"video": "mp4", "audio": "flac", "latent": "safetensors", "metadata": "json"}[kind]
    path = Path(directory) / f"candidate-{candidate}.{suffix}"
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError("This saved preview is unavailable")
    return path


def summary(directory, config, state):
    if not isinstance(config, dict) or config.get("recipe") != "seed_hunter":
        return None
    options = config.get("seed_hunter", {})
    result = {"stage": options.get("stage", "preview"),
              **{key: config.get(key) for key in ("width", "height", "frames")}, "candidates": []}
    result["final_frames"] = options.get("final_frames")
    if scout_upscale_enabled(config):
        result.update(final_width=scout_output_size(config)[0], final_height=scout_output_size(config)[1],
                      upscale_requested=True)
    saved = _read(Path(directory) / "candidates.json")
    rows = saved.get("candidates", [])
    for row in rows[:3] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("status") != "completed":
            continue
        index = row.get("id")
        try:
            artifact(directory, index, "video")
        except (ValueError, FileNotFoundError):
            continue
        can_refine = result["stage"] == "preview" and state.get("status") == "completed"
        if can_refine:
            try:
                artifact(directory, index, "latent")
                artifact(directory, index, "metadata")
            except FileNotFoundError:
                can_refine = False
        public = {"index": index, "seed": row.get("seed"), "can_refine": can_refine,
            "url": f"/api/video/h3/jobs/{Path(directory).name}/seed-hunter/candidates/{index}/video"}
        public.update({key: row[key] for key in ("width", "height", "latent_upscaled") if key in row})
        if result["stage"] == "dialogue":
            try:
                artifact(directory, index, "audio")
                public["audio_url"] = public["url"].removesuffix("video") + "audio"
            except FileNotFoundError:
                pass
        result["candidates"].append(public)
    provenance = _read(Path(directory) / "manifest.json").get("seed_hunter_source", {})
    if isinstance(provenance, dict):
        result.update({key: provenance[key] for key in ("source_job_id", "source_candidate", "source_seed", "preview_frames", "regeneration_required") if key in provenance})
        if type(provenance.get("preview_frames")) is int:
            result["regeneration_required"] = config["frames"] != provenance["preview_frames"]
    return result


def final_length(directory, manifest):
    """Resolve final duration independently from the inexpensive preview length."""
    from src.h3_video import UPLOAD_EXTENSIONS
    from src.h3_video_length import select_batch_video_length
    from src.video_job_edit import _inputs
    config = manifest["config"]
    preview_frames = config["frames"]
    frames = config.get("seed_hunter", {}).get("final_frames")
    metadata = {}
    if frames is not None:
        basis = "configured"
        saved = _read(Path(directory) / "submission.json").get("batch_video_length", {})
        if isinstance(saved, dict) and saved.get("target") == "final" and saved.get("frames") == frames:
            metadata, basis = saved, "source"
    elif manifest.get("uploads", manifest).get("reference_videos") and config.get("mode") == "ref2va":
        inputs = _inputs(directory, manifest, UPLOAD_EXTENSIONS)
        paths = {field: [item["path"] for item in values] for field, values in inputs.items()}
        _, metadata = select_batch_video_length(config, paths)
        frames, basis = metadata["frames"], "source"
    else:
        frames, basis = preview_frames, "preview"
    return {**metadata, "frames": frames, "selected_seconds": frames / 24, "target": "final",
            "preview_frames": preview_frames, "basis": basis,
            "regeneration_required": frames != preview_frames}


def detail(manager, job_id, owner):
    from src.h3_loras import frozen_components
    from src.video_job_edit import _draft
    state = manager._state(job_id, owner)
    directory = manager.directory(job_id)
    manifest = _read(directory / "manifest.json")
    config = manifest.get("config", {})
    data = summary(directory, config, state)
    if data is None:
        raise FileNotFoundError("Seed Hunter job not found")
    inventory = manager.submission_inventory()
    result = {"config": _draft(config, inventory, "h3"), "seed_hunter": data,
              "lora_components": frozen_components(config, inventory)}
    if data["stage"] == "preview":
        try:
            length = final_length(directory, manifest)
        except (ValueError, FileNotFoundError):
            length = {"frames": None, "requires_manual": True,
                      "error": "Could not read the source duration. Choose the final output length manually."}
        result.update(final_length=length, refinement_defaults={"frames": length["frames"] or config["frames"]})
    return result


def clone_snapshot(source_directory, destination, snapshot):
    """Final reruns own immutable hardlinks; parent deletion cannot break them."""
    from src.video_queue_bulk import _clone_file
    if not isinstance(snapshot, dict) or type(snapshot.get("candidate")) is not int:
        raise ValueError("This refinement has no saved preview latent")
    result = {"candidate": snapshot["candidate"]}
    for key, suffix in (("latent", ".safetensors"), ("metadata", ".json")):
        path = Path(snapshot.get(key, ""))
        if (path.is_symlink() or not path.is_file() or path.resolve().parent != Path(source_directory).resolve()
                or path.suffix != suffix):
            raise ValueError("The saved preview latent is unavailable or outside its job")
        target = Path(destination) / ("source-latent" + suffix)
        _clone_file(path, target)
        result[key] = str(target.resolve())
    return result


FINAL_EDIT_SETTINGS = {"width", "height", "frames", "steps", "seed", "sampler", "scheduler", "gpu", "vae_gpu",
                       "loras", "lora", "lora_scale", "latent_upscaler", "seed_hunter", "shift_video", "shift_audio"}
FINAL_OPTIONS = {"denoise", "output_fps", "final_frames", "upscaler_chunking", "upscaler_force_unload", "shift_enabled",
                 "attention_backend", "sparse_method", "sparse_keep_percent", "sparse_start", "sparse_end",
                 "sparse_min_tokens", "sparse_extra_tokens", "sparse_sink_conditioning",
                 "ff_chunk_size", "ff_chunk_threshold", "low_vram_chunks"}


def guard_edit(original, updated):
    if original.get("recipe") != updated.get("recipe"):
        raise ValueError("A queued job cannot change workflow recipe")
    if original.get("recipe") != "seed_hunter":
        return
    old, new = original["seed_hunter"], updated["seed_hunter"]
    if old["stage"] != new["stage"]:
        raise ValueError("A queued Seed Hunter job cannot change stage")
    if old["stage"] == "final":
        allowed = FINAL_EDIT_SETTINGS | {"lora_profiles"}
        if any(original.get(k) != updated.get(k) for k in original.keys() | updated.keys() if k not in allowed):
            raise ValueError("Seed Hunter refinement keeps its source prompt, model, encoder, and inputs")
        if any(old.get(k) != new.get(k) for k in old.keys() | new.keys() if k not in FINAL_OPTIONS):
            raise ValueError("Saved-latent refinement keeps its source conditioning")


def guard_refinement_dimensions(metadata_path, config):
    """Reject impossible resizing before enqueueing, using the actual preview."""
    path = Path(metadata_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("The saved preview metadata is unavailable")
    original = _read(path).get("config", {})
    if not isinstance(original, dict) or any(type(original.get(key)) is not int or original[key] < 32
                                              for key in ("width", "height")):
        raise ValueError("The saved preview metadata has no usable dimensions")
    if config["width"] < original["width"] or config["height"] < original["height"]:
        raise ValueError("Refinement dimensions must be at least the original preview dimensions")


def guard_saved_refinement(directory, manifest, updated):
    if manifest.get("config", {}).get("seed_hunter", {}).get("stage") != "final":
        return
    snapshot = manifest.get("seed_hunter_input")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metadata"), str):
        raise ValueError("This refinement has no saved preview metadata")
    path = Path(snapshot["metadata"])
    if path.suffix != ".json" or path.resolve().parent != Path(directory).resolve():
        raise ValueError("The saved preview metadata is outside its job")
    guard_refinement_dimensions(path, updated)


def prepare_final(manager, source_id, owner, body, directory, submission_id):
    from src.h3_loras import frozen_inventory
    from src.h3_video import validate_config, UPLOAD_EXTENSIONS
    from src.video_job_edit import _inputs, _draft
    from src.video_queue_bulk import _clone_file
    if not isinstance(body, dict) or body.keys() - {"candidate", "request_id", "config"}:
        raise ValueError("Invalid refinement request")
    patch = body.get("config", {})
    if not isinstance(patch, dict) or patch.keys() - (FINAL_EDIT_SETTINGS | {"denoise"}):
        raise ValueError("Unsupported refinement setting")
    candidate = body.get("candidate")
    manager._state(source_id, owner)
    current = manager.submission_inventory()
    source = manager.directory(source_id)
    with _locked(source / "job.lock"):
        state = _read(source / "state.json")
        if state.get("owner") != owner:
            raise FileNotFoundError("Seed Hunter job not found")
        if state.get("status") in ACTIVE:
            raise RuntimeError("Wait for the preview job to finish before refining a candidate")
        manifest = _read(source / "manifest.json")
        config = manifest.get("config", {})
        data = summary(source, config, state)
        if data is None or not any(row["index"] == candidate and row["can_refine"] for row in data["candidates"]):
            raise ValueError("Choose an available saved preview")
        inventory = frozen_inventory(config, current)
        raw = _draft(config, inventory, "h3")
        chosen = next(row for row in data["candidates"] if row["index"] == candidate)
        selected_seed = _read(artifact(source, candidate, "metadata")).get("seed", chosen["seed"])
        raw["seed"] = selected_seed
        if "frames" not in patch:
            try:
                raw["frames"] = final_length(source, manifest)["frames"]
            except (ValueError, FileNotFoundError) as error:
                raise ValueError("Could not read the source duration. Choose the final output length manually.") from error
        changes = patch.get("seed_hunter", {})
        if not isinstance(changes, dict) or changes.keys() - FINAL_OPTIONS:
            raise ValueError("Unsupported refinement option")
        options = {**raw["seed_hunter"], **changes, "stage": "final", "preview_count": 1}
        if "denoise" in patch:
            options["denoise"] = patch["denoise"]
        raw.update({key: value for key, value in patch.items() if key not in {"denoise", "seed_hunter"}})
        if "loras" not in patch:
            if "lora" in patch:
                raw.pop("loras", None)
            elif "lora_scale" in patch and raw.get("loras"):
                raw["loras"][0]["strength"] = patch["lora_scale"]
        raw["seed_hunter"] = options
        inputs = _inputs(source, manifest, UPLOAD_EXTENSIONS)
        resolved = validate_config(raw, inventory["components"], inventory["gpus"], inputs)
        guard_refinement_dimensions(artifact(source, candidate, "metadata"), resolved)
        preflight(resolved, current, inputs)
        saved, names = {}, {}
        for field, values in inputs.items():
            paths = []
            for item in values:
                target = directory / (uuid.uuid4().hex + Path(item["path"]).suffix)
                _clone_file(item["path"], target)
                paths.append(str(target.resolve()))
            saved[field] = paths[0] if paths and field in {"first_frame", "last_frame", "source_video"} else paths
            if values:
                names[field] = [item["name"] for item in values]
        saved["seed_hunter_input"] = clone_snapshot(source, directory, {
            "candidate": candidate, "latent": str(artifact(source, candidate, "latent")),
            "metadata": str(artifact(source, candidate, "metadata"))})
        saved["seed_hunter_source"] = {"source_job_id": source_id, "source_candidate": candidate,
            "source_seed": selected_seed, "preview_frames": config["frames"],
            "regeneration_required": resolved["frames"] != config["frames"]}
        _write(directory / "submission.json", {"submission_id": submission_id, "input_names": names,
                                                "source_name": manifest.get("source_name")})
        return resolved, saved
