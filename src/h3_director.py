"""Independent Muse Director inventory, candidate history and explicit refinement."""
from copy import deepcopy
from pathlib import Path
import uuid

from scripts.h3_director_controls import DEFAULTS, SAMPLERS, SCHEDULERS
from src.h3_video import _read, _write


def capabilities(components):
    defaults = {"recipe": "director", "mode": "ref2va", "width": 1280, "height": 768,
                "frames": 243, "steps": 20, "sampler": "res_multistep", "scheduler": "simple",
                "shift_video": 12, "shift_audio": 3, "loras": [], "director": deepcopy(DEFAULTS)}
    for role, variant in (("model", "ref2va"), ("model_fl2va", "fl2va")):
        items = [c for c in components if c.get("role") == "model" and c.get("variant") == variant]
        selected = next((c for c in items if "pruned_int8" in c.get("name", "") and "singularity" not in c.get("name", "").lower()), next(iter(items), None))
        if selected:
            defaults[role] = selected["id"]
    return {"version": 1, "upstream_version": "1.2.0", "defaults": defaults,
            "modes": ["ref2va", "fl2va"], "samplers": list(SAMPLERS), "schedulers": list(SCHEDULERS),
            "timeline": True, "hybrid_continuation": True, "candidate_slots": [1, 2, 3, 4],
            "max_duration_seconds": 120, "single_chunk_refinement": True,
            "refinement_defaults": {"megapixels": 1.96, "steps": 20, "sampler": "res_multistep", "scheduler": "simple", "denoise": .2}}


def artifact(directory, index, suffix):
    if type(index) is not int or not 1 <= index <= 4:
        raise ValueError("Choose a Director candidate from 1 through 4")
    path = Path(directory) / f"candidate-{index}.{suffix}"
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError("Director candidate is unavailable")
    return path


def summary(directory, config, state):
    options = config.get("director", {})
    result = {"stage": options.get("stage", "generation"), "duration_seconds": options.get("duration_seconds"), "candidates": []}
    for row in _read(Path(directory) / "director-candidates.json").get("candidates", []):
        try:
            artifact(directory, row["index"], "mp4")
        except (KeyError, ValueError, FileNotFoundError):
            continue
        can_refine = bool(row.get("can_refine"))
        if can_refine:
            try:
                artifact(directory, row["index"], "safetensors")
                artifact(directory, row["index"], "json")
            except FileNotFoundError:
                can_refine = False
        result["candidates"].append({**row, "can_refine": can_refine,
            "url": f"/api/video/h3/jobs/{Path(directory).name}/director/candidates/{row['index']}/video"})
    result["source"] = _read(Path(directory) / "manifest.json").get("director_source")
    return result


def clone_snapshot(source, destination, snapshot):
    from src.video_queue_bulk import _clone_file
    result = {}
    for key, suffix in (("tensors", ".safetensors"), ("metadata", ".json")):
        path = Path(snapshot.get(key, ""))
        if path.is_symlink() or not path.is_file() or path.resolve().parent != Path(source).resolve() or path.suffix != suffix:
            raise ValueError("Director candidate snapshot is unavailable or outside its job")
        target = Path(destination) / ("director-source" + suffix)
        _clone_file(path, target)
        result[key] = str(target.resolve())
    return result


def prepare_refinement(manager, job_id, owner, body, destination, submission_id):
    from src.h3_video import UPLOAD_EXTENSIONS, validate_config
    from src.video_job_edit import _draft, _inputs
    from src.video_queue_bulk import _clone_file
    from scripts.h3_seed_hunter_presets import final_size

    if not isinstance(body, dict) or body.keys() - {"request_id", "candidate", "config"}:
        raise ValueError("Invalid Director refinement request")
    state = manager._state(job_id, owner)
    source = manager.directory(job_id)
    manifest = _read(source / "manifest.json")
    config = manifest.get("config", {})
    if config.get("recipe") != "director" or config.get("director", {}).get("stage") != "generation":
        raise ValueError("Select a Director generation candidate")
    index = body.get("candidate")
    row = next((row for row in summary(source, config, state)["candidates"] if row["index"] == index), None)
    if not row or not row["can_refine"]:
        raise ValueError("Muse Refine needs a saved single-chunk candidate; multi-chunk clips can be saved directly")
    patch = body.get("config", {})
    if not isinstance(patch, dict) or patch.keys() - {"width", "height", "steps", "sampler", "scheduler", "denoise", "gpu", "vae_gpu"}:
        raise ValueError("Unsupported Director refinement setting")
    inventory = manager.inventory()
    raw = _draft(config, inventory, "h3")
    raw.update(final_size(config, 1.96))
    raw.update(steps=20, sampler="res_multistep", scheduler="simple", seed=row["seed"])
    raw.update({key: value for key, value in patch.items() if key != "denoise"})
    raw["director"] = {**raw["director"], "stage": "refine", "candidate_2": False, "candidate_3": False,
                       "candidate_4": False, "denoise": patch.get("denoise", .2)}
    inputs = _inputs(source, manifest, UPLOAD_EXTENSIONS)
    resolved = validate_config(raw, inventory["components"], inventory["gpus"], inputs)
    saved, names = {}, {}
    for field, values in inputs.items():
        paths = []
        for value in values:
            target = destination / (uuid.uuid4().hex + Path(value["path"]).suffix)
            _clone_file(value["path"], target)
            paths.append(str(target.resolve()))
        saved[field] = paths[0] if paths and field in {"first_frame", "last_frame", "source_video"} else paths
        if values:
            names[field] = [value["name"] for value in values]
    saved["director_input"] = clone_snapshot(source, destination, {
        "tensors": str(artifact(source, index, "safetensors")), "metadata": str(artifact(source, index, "json"))})
    saved["director_source"] = {"source_job_id": job_id, "candidate": index, "seed": row["seed"]}
    _write(destination / "submission.json", {"submission_id": submission_id, "input_names": names})
    return resolved, saved


def guard_edit(original, updated):
    if original.get("recipe") != "director":
        return
    if updated.get("recipe") != "director" or original["director"]["stage"] != updated["director"]["stage"]:
        raise ValueError("Keep the saved Director job's workflow and stage")
    if original["director"]["stage"] == "refine":
        allowed = {"width", "height", "steps", "sampler", "scheduler", "gpu", "vae_gpu", "seed", "director"}
        if any(updated.get(key) != value for key, value in original.items() if key not in allowed):
            raise ValueError("Director refinement keeps its original prompt, models and candidate")
        if any(updated["director"].get(key) != value for key, value in original["director"].items() if key != "denoise"):
            raise ValueError("Director refinement keeps its original timeline")
