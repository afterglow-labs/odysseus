"""Owner-scoped, revision-checked edits of jobs that have not started rendering."""
import hashlib
import os
from pathlib import Path
import uuid

from src.h3_video import _locked, _read, _write
from src.h3_vfx import normalize_vfx_inputs
from src.video_workflow import specification, compatible


def revision(manifest):
    value = manifest.get("revision", 0)
    return value if type(value) is int and value >= 0 else 0


def _queued(manager, job_id, owner, expected=None):
    directory = manager.directory(job_id)
    state = _read(directory / "state.json")
    if not state or state.get("owner") != owner:
        raise FileNotFoundError("Video job not found")
    if state.get("status") != "queued" or (directory / "cancel").exists():
        raise RuntimeError("This job is no longer queued. Only queued jobs can be edited.")
    manifest = _read(directory / "manifest.json")
    if expected is not None and revision(manifest) != expected:
        raise RuntimeError("This job was edited elsewhere. Reopen Edit to load its latest settings.")
    return directory, state, manifest


def _inputs(directory, manifest, extensions):
    source = manifest.get("uploads", manifest)
    names = manifest.get("input_names", {})
    normalized = normalize_vfx_inputs(manifest.get("config", {}), source)
    if "source_video" not in source and "source_video" in normalized and isinstance(names, dict):
        names = {**names, "source_video": names.get("reference_videos", []), "reference_videos": []}
    source = normalized
    result = {}
    for field in extensions:
        values = source.get(field) or []
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list):
            raise ValueError("Invalid saved video inputs")
        labels = names.get(field, []) if isinstance(names, dict) else []
        result[field] = []
        for index, value in enumerate(values):
            if not isinstance(value, str):
                raise ValueError("Invalid saved video input")
            path = Path(value)
            if (path.is_symlink() or not path.is_file()
                    or path.resolve().parent != directory.resolve()
                    or path.suffix.lower() not in extensions[field]):
                raise ValueError("A saved video input is unavailable or outside its job")
            name = labels[index] if isinstance(labels, list) and len(labels) > index else path.name
            name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
            name = "".join(c for c in name if c.isprintable())[:255] or path.name
            result[field].append({"index": index, "name": name, "size": path.stat().st_size, "path": str(path)})
    return result


def input_source_name(inputs, family):
    """The guide/target clip names a VFX/BFS job; references keep their roles."""
    field = "source_video" if inputs.get("source_video") or family == "bfs" else "reference_videos"
    return (inputs.get(field) or [{}])[-1].get("name")


def persist_legacy_inputs(manifest, inputs, family):
    """Save promoted roles only as part of an explicitly requested job edit."""
    source = manifest.get("uploads", manifest)
    if family != "h3" or "source_video" in source or not inputs.get("source_video"):
        return dict(manifest)
    result = dict(manifest)
    result.pop("uploads", None)
    for field, values in inputs.items():
        paths = [item["path"] for item in values]
        result[field] = paths if field not in {"first_frame", "last_frame", "source_video"} else paths[0] if paths else []
    result["input_names"] = {field: [item["name"] for item in values] for field, values in inputs.items() if values}
    result["source_name"] = input_source_name(inputs, family)
    return result


def _draft(config, inventory, family, *, resolved_paths=None):
    # Inventory paths can cross a network mount. Resolve each distinct path
    # once per snapshot request, not once per component per saved job. Keep this
    # cache request-scoped so a moved/relinked model is reconsidered next time.
    resolved_paths = {} if resolved_paths is None else resolved_paths

    def resolve(value):
        key = str(value)
        if key not in resolved_paths:
            resolved_paths[key] = Path(value).resolve()
        return resolved_paths[key]

    settings, roles, _ = specification(family, config)
    result = {key: value for key, value in config.items() if key in settings | {"gpu", "vae_gpu"}}
    choices = {}
    for role in roles:
        value = config.get(role)
        if not value:
            choices[role] = ""
            continue
        # IDs, not server paths, are the only accepted component selections.
        # Keep an unavailable selection visible instead of replacing it with a
        # different checkpoint when a cache has moved or disconnected.
        path = resolve(value)
        choices[role] = next((row["id"] for row in inventory["components"]
                              if compatible(row, role, family, config)
                              and resolve(row["path"]) == path),
                             hashlib.sha256(str(path).encode()).hexdigest()[:32])
    if family == "bfs":
        result["components"] = choices
        if "prompt_notes" in config:
            result["prompt"] = config["prompt_notes"]
        else:
            if config.get("family") == "h3":
                from scripts.bfs_h3 import build_prompt
                prefix = build_prompt("")
                separator = "\n\nAdditional user instructions:\n"
            else:
                from scripts.bfs_ltx_wan import build_prompt
                prefix = build_prompt(config["workflow_id"], "")
                separator = "\n"
            prompt = config.get("prompt", "")
            result["prompt"] = "" if prompt == prefix else prompt[len(prefix + separator):] if prompt.startswith(prefix + separator) else prompt
    else:
        result.update(choices)
        if "loras" in config:
            from src.h3_loras import lora_entries
            result["loras"] = []
            for row in lora_entries(config, "path"):
                path = resolve(row["path"])
                identity = next((item["id"] for item in inventory["components"]
                                 if item.get("role") == "lora" and resolve(item["path"]) == path),
                                hashlib.sha256(str(path).encode()).hexdigest()[:32])
                result["loras"].append({"id": identity, "strength": row["strength"]})
    return result


def snapshot(manager, job_id, owner, family, *, expected=None, retain=None, inventory=None):
    # Verify owner before scanning the model cache or creating any lock file.
    manager._state(job_id, owner)
    if inventory is None:
        inventory = manager.inventory()
    directory = manager.directory(job_id)
    with _locked(directory / "job.lock"):
        directory, _, manifest = _queued(manager, job_id, owner, expected)
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise ValueError("This job has no usable workflow settings")
        _, _, extensions = specification(family, config)
        inputs = _inputs(directory, manifest, extensions)
        if retain is not None:
            if not isinstance(retain, dict) or retain.keys() - extensions.keys():
                raise ValueError("Invalid retained video inputs")
            selected = {}
            for field, values in inputs.items():
                indices = retain.get(field, [])
                if (not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(values) for i in indices)
                        or len(indices) != len(set(indices))):
                    raise ValueError("Invalid retained input indices for " + field.replace("_", " "))
                selected[field] = [values[i] for i in indices]
            inputs = selected
        return {"id": job_id, "revision": revision(manifest), "config": _draft(config, inventory, family), "inputs": inputs}


def get_edit(manager, job_id, owner, family):
    result = snapshot(manager, job_id, owner, family)
    result["inputs"] = {field: [{k: item[k] for k in ("index", "name", "size")} for item in values]
                        for field, values in result["inputs"].items() if values or family == "h3" and field == "source_video"}
    return result


def validate(raw, inventory, uploads, family):
    if family == "h3":
        from src.h3_video import validate_config
        config = validate_config(raw, inventory["components"], inventory["gpus"], uploads)
        if not inventory["runtime_ready"]:
            raise ValueError(inventory["runtime_error"])
        return config
    from src.bfs_video import validate_config
    return validate_config(raw, inventory, uploads)


def commit_edit(manager, job_id, owner, family, raw, expected, retain, saved, names, stage, limit):
    """One manifest replacement publishes config, attachments, and revision.

    Uploads are staged outside the job; a render may start during an upload.
    The final status/revision check and publish share the worker-start lock.
    No edit enqueues a replacement job or changes the existing FIFO receipt.
    """
    manager._state(job_id, owner)
    inventory = manager.inventory()
    directory = manager.directory(job_id)
    moved = []
    with _locked(directory / "job.lock"):
        directory, state, manifest = _queued(manager, job_id, owner, expected)
        _, _, extensions = specification(family, manifest["config"])
        originals = _inputs(directory, manifest, extensions)
        if retain is None:
            retained = originals
        else:
            if not isinstance(retain, dict) or retain.keys() - extensions.keys():
                raise ValueError("Invalid retained video inputs")
            retained = {}
            for field, values in originals.items():
                indices = retain.get(field, [])
                if (not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(values) for i in indices)
                        or len(indices) != len(set(indices))):
                    raise ValueError("Invalid retained input indices for " + field.replace("_", " "))
                retained[field] = [values[i] for i in indices]
        inputs, labels, total = {}, {}, 0
        for field in extensions:
            values = saved.get(field) or []
            values = [values] if isinstance(values, str) else values
            inputs[field] = [item["path"] for item in retained[field]]
            labels[field] = [item["name"] for item in retained[field]]
            total += sum(item["size"] for item in retained[field])
            for index, value in enumerate(values):
                path = Path(value)
                if path.is_symlink() or not path.is_file() or path.resolve().parent != Path(stage).resolve():
                    raise ValueError("Invalid staged video input")
                total += path.stat().st_size
                inputs[field].append(str(path))
                labels[field].append(names[field][index])
        if total > limit:
            raise ValueError("Retained and new video inputs exceed the per-job upload limit")
        config = validate(raw, inventory, inputs, family)
        try:
            # Only validated local staging files can be moved into a job.
            for field, values in inputs.items():
                for index in range(len(retained[field]), len(values)):
                    source = Path(values[index])
                    target = directory / (uuid.uuid4().hex + source.suffix.lower())
                    os.replace(source, target)
                    moved.append(target)
                    values[index] = str(target.resolve())
            updated = dict(manifest)
            updated.pop("uploads", None)
            for field, values in inputs.items():
                updated[field] = values if family == "h3" and field not in {"first_frame", "last_frame", "source_video"} else values[0] if values else []
            source_field = "source_video" if labels.get("source_video") or family == "bfs" else "reference_videos"
            updated.update(config=config, input_names=labels, source_name=(labels[source_field] or [None])[-1],
                           revision=expected + 1)
            # Supervisors from before editable queues read the manifest before
            # taking job.lock. Upgrade only this verified, still-queued process
            # so its GPU environment and worker config cannot disagree.
            manager.upgrade_queued_supervisor(directory, state)
            _write(directory / "manifest.json", updated)
        except BaseException:
            for path in moved:
                path.unlink(missing_ok=True)
            raise
        # The atomic manifest is authoritative. Removed attachments are now
        # unreferenced; best-effort unlink never follows a caller-supplied path.
        kept = {value for values in inputs.values() for value in values}
        for values in originals.values():
            for item in values:
                if item["path"] not in kept:
                    try:
                        Path(item["path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
    return manager.view(job_id, owner)
