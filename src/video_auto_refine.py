"""Durable, opt-in candidate continuations and organized project outputs."""
import asyncio
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import uuid

from scripts.h3_auto_refine_controls import enabled, slots, final_size
from src.h3_video import JOB_ID, _locked, _read, _write

log = logging.getLogger(__name__)


def register(manager, directory, config, uploads):
    if not uploads.get("auto_refine_source") and not uploads.get("project_exports"):
        return
    root = manager.root / ".automatic"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write(root / (directory.name + ".json"), {"id": directory.name})


def _label(value, fallback):
    value = re.sub(r'[^\w .()-]', '_', str(value), flags=re.UNICODE).strip(' .')[:100]
    return value or fallback


def project_directory(project, owner, name):
    # Account suffix prevents two signed-in owners from sharing output folders.
    suffix = hashlib.sha256(str(owner).encode()).hexdigest()[:10]
    root = Path(project) / "outputs" / "video-projects"
    return root / f"{_label(name, 'Project')}--{suffix}"


def _output_path(manifest, state, source):
    if not isinstance(source, dict) or type(source.get("candidate")) is not int or not 1 <= source["candidate"] <= 32:
        return None
    name = source.get("project")
    if not isinstance(name, str) or not name:
        return None
    directory = project_directory(manifest["project_path"], state["owner"], name)
    date = source.get('date') or datetime.fromtimestamp(state['created_at']).strftime('%Y-%m-%d')
    if not isinstance(date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
        raise ValueError('Invalid output grouping date')
    seed = source.get('seed', manifest['config']['seed'])
    suffix = f"--candidate-{source['candidate']}--seed-{seed}--{state['id']}.mp4"
    label = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', Path(manifest.get('source_name') or 'video').stem).strip(' .') or 'video'
    # Preserve the input's spelling, spaces and punctuation, subject only to
    # the filesystem's filename limit and cross-platform invalid characters.
    label = label.encode('utf-8')[:255 - len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore')
    return directory / date / f"Candidate {source['candidate']}" / (label + suffix)


def _safe_output_parent(target, project, *, create=False):
    root = Path(project).resolve()
    relative = target.relative_to(Path(project))
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ValueError("Project output folder cannot be a symbolic link")
        if create:
            current.mkdir(exist_ok=True, mode=0o700)
    if target.is_symlink():
        raise ValueError("Project output file cannot be a symbolic link")


def _source_videos(manifest, *, strict=True):
    """Describe saved video snapshots without opening/probing the originals."""
    from src.video_job_inputs import VIDEO_FIELDS, _videos
    from src.video_server_files import VIDEO_EXTENSIONS
    names = manifest.get("input_names", {})
    result = []
    for field in VIDEO_FIELDS:
        labels = names.get(field, []) if isinstance(names, dict) else []
        for index, value in enumerate(_videos(manifest, field)):
            if not isinstance(value, str) or Path(value).suffix.lower() not in VIDEO_EXTENSIONS:
                if strict:
                    raise ValueError("A saved source video is invalid")
                continue
            path = Path(value)
            label = labels[index] if isinstance(labels, list) and index < len(labels) else path.name
            label = str(label).replace('\\', '/').rsplit('/', 1)[-1]
            result.append((field, index, path, label))
    return result


def _source_output_path(target, state, video):
    field, index, path, label = video
    role = "source-video" if field == "source_video" else "reference-video"
    suffix = f"--{role}-{index + 1}--{state['id']}{path.suffix.lower()}"
    label = re.sub(r'[\x00-\x1f/\\:*?"<>|]', '_', Path(label).stem).strip(' .') or role
    label = label.encode('utf-8')[:255 - len(suffix.encode('utf-8'))].decode('utf-8', errors='ignore')
    return target.with_name(label + suffix)


def publish_output(directory):
    from src.video_job_storage import publish_gallery_video
    with _locked(directory / "job.lock"):
        manifest, state = _read(directory / "manifest.json"), _read(directory / "state.json")
        exports = manifest.get("project_exports", [])
        if manifest.get("auto_refine_source"):
            exports = [*exports, manifest["auto_refine_source"]]
        if not exports or state.get("status") != "completed":
            return
        source = Path(manifest["output_path"])
        if source.is_symlink() or source.parent != directory.resolve() or not source.is_file():
            raise ValueError("Completed video is unavailable")
        videos = _source_videos(manifest)
        for _, _, path, _ in videos:
            if path.is_symlink() or path.parent != directory.resolve() or not path.is_file():
                raise ValueError("A saved source video is unavailable or outside its job")
        paths, source_paths = [], []
        for spec in exports:
            target = _output_path(manifest, state, spec)
            if target is None:
                continue
            _safe_output_parent(target, manifest["project_path"], create=True)
            if not target.exists():
                publish_gallery_video(source, target)
            paths.append(str(target))
            for video in videos:
                source_target = _source_output_path(target, state, video)
                _safe_output_parent(source_target, manifest["project_path"])
                if source_target.exists() and not source_target.is_file():
                    raise ValueError("Project source video must be a regular file")
                if not source_target.exists():
                    publish_gallery_video(video[2], source_target)
                source_paths.append(str(source_target))
        state["project_output_paths"] = list(dict.fromkeys(paths))
        state["project_source_paths"] = list(dict.fromkeys(source_paths))
        state.pop("automation_error", None)
        _write(directory / "state.json", state)


def delete_output(manifest, state):
    exports = list(manifest.get("project_exports", []))
    if manifest.get("auto_refine_source"):
        exports.append(manifest["auto_refine_source"])
    for spec in exports:
        target = _output_path(manifest, state, spec)
        if target is not None:
            _safe_output_parent(target, manifest["project_path"])
            target.unlink(missing_ok=True)
            # Invalid old inputs were never exported and must not prevent
            # deleting an otherwise owned, terminal job.
            for video in _source_videos(manifest, strict=False):
                source_target = _source_output_path(target, state, video)
                _safe_output_parent(source_target, manifest["project_path"])
                source_target.unlink(missing_ok=True)


def existing_refinements(manager, source_id, owner, config, candidate):
    seed = config["seed"] + candidate - 1
    scout = config["seed_hunter"]["stage"] == "scout"
    results = []
    for directory in manager.root.iterdir():
        if directory.is_symlink() or not directory.is_dir() or not JOB_ID.fullmatch(directory.name):
            continue
        state, manifest = _read(directory / "state.json"), _read(directory / "manifest.json")
        source = manifest.get("seed_hunter_source", {})
        if scout and directory.name == source_id and state.get("status") == "completed" and state.get("owner") == owner:
            receipt = _read(directory / "scout.json")
            if receipt.get("mode") != "scout" and receipt.get("selected_seed") == seed:
                results.append(directory)
        if (state.get("owner") == owner and state.get("status") in {"queued", "running", "completed"}
                and source.get("source_job_id") == source_id
                and source.get("source_seed" if scout else "source_candidate") == (seed if scout else candidate)):
            results.append(directory)
    return results


def apply_saved(manager, owner, body):
    """Explicit, idempotent action on saved preview parents, with per-job receipts."""
    from scripts.h3_auto_refine_controls import validate, validate_dimensions
    from src.video_submission import UUID
    if not isinstance(body, dict) or set(body) != {"jobs", "settings", "request_id"}:
        raise ValueError("Supply preview jobs, automatic refinement settings and a request ID")
    jobs, settings, key = body["jobs"], body["settings"], body["request_id"]
    if not isinstance(settings, dict):
        raise ValueError("Shared refinement settings must be an object")
    if (not isinstance(jobs, list) or not 1 <= len(jobs) <= 1000
            or any(not isinstance(i, str) or not JOB_ID.fullmatch(i) for i in jobs) or len(set(jobs)) != len(jobs)):
        raise ValueError("Choose 1–1000 distinct saved preview jobs")
    if not isinstance(key, str) or not UUID.fullmatch(key):
        raise ValueError("A request ID UUID is required")
    for identity in jobs:
        manager._state(identity, owner)
    root = manager.root / ".auto-refine-requests"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (hashlib.sha256(f"{owner}:{uuid.UUID(key).hex}".encode()).hexdigest() + ".json")
    fingerprint = hashlib.sha256(json.dumps({"jobs": sorted(jobs), "settings": settings}, sort_keys=True).encode()).hexdigest()
    with _locked(path.with_suffix(".lock")):
        receipt = _read(path, {"fingerprint": fingerprint, "jobs": {}, "errors": {}})
        if receipt["fingerprint"] != fingerprint:
            raise RuntimeError("This automatic refinement request ID was already used with different settings")
        for identity in jobs:
            directory = manager.directory(identity)
            try:
                state = manager._state(identity, owner)
                if state.get("status") not in {"completed", "failed", "stopped"}:
                    raise ValueError("Wait for previews to finish before processing saved candidates")
                manifest = _read(directory / "manifest.json")
                config = manifest.get("config", {})
                options = config.get("seed_hunter", {})
                if config.get("recipe") != "seed_hunter" or options.get("stage") not in {"preview", "scout"}:
                    raise ValueError("Choose a Seed Hunter or Seed Scout preview job")
                values = validate(settings, options)
                if not values["candidates"]:
                    raise ValueError("Choose candidates to refine automatically")
                manifest = {**manifest, "config": {**config, "seed_hunter": {**options, "auto_refine": values}}}
                validate_dimensions(manifest["config"])
                results = receipt["jobs"].setdefault(identity, [])
                for candidate in slots(manifest["config"]["seed_hunter"]):
                    if candidate not in {row["candidate"] for row in results}:
                        results.append(_queue_candidate(manager, identity, owner, manifest, candidate, uuid.UUID(key).hex))
                        _write(path, receipt)
                receipt["errors"].pop(identity, None)
            except (ValueError, RuntimeError, FileNotFoundError, OSError) as exc:
                from src.h3_video import _redact
                receipt["errors"][identity] = _redact(str(exc))
            _write(path, receipt)
        return {"jobs": receipt["jobs"], "errors": receipt["errors"],
                "created": sum(not row.get("skipped") for rows in receipt["jobs"].values() for row in rows),
                "skipped": sum(bool(row.get("skipped")) for rows in receipt["jobs"].values() for row in rows),
                "output_folder": str(project_directory(manager.project, owner, settings.get("project", "")))}


def organize_existing(manager, directory, owner, source_id, project, candidate):
    with _locked(directory / "job.lock"):
        state, manifest = _read(directory / "state.json"), _read(directory / "manifest.json")
        if state.get("owner") != owner:
            raise FileNotFoundError("Refinement not found")
        seed = manifest['config']['seed']
        if directory.name == source_id:
            seed = _read(directory / 'scout.json').get('selected_seed', seed)
        parent = manager._state(source_id, owner)
        spec = {"project": project, "candidate": candidate, "source_job_id": source_id, 'seed': seed,
                'date': datetime.fromtimestamp(parent['created_at']).strftime('%Y-%m-%d')}
        exports = manifest.setdefault("project_exports", [])
        if spec not in exports:
            exports.append(spec)
            _write(directory / "manifest.json", manifest)
        register(manager, directory, manifest["config"], manifest)
    if state.get("status") == "completed":
        publish_output(directory)


def _queue_candidate(manager, source_id, owner, manifest, candidate, run_id="automatic"):
    from src import h3_seed_hunter as hunter, h3_seed_scout as scout
    from src.video_submission import Submission
    from src.h3_video import _read
    options = manifest["config"]["seed_hunter"]
    settings = options["auto_refine"]
    key = uuid.uuid5(uuid.NAMESPACE_URL, f"odysseus-auto-refine:{owner}:{source_id}:{run_id}:{candidate}").hex
    pending = Submission(manager, owner, "h3-auto-refine-" + source_id, key)
    pending.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(pending.root / f"lock-{int(pending.job_id[:2], 16) % 64:02d}"):
        # A deleted automatically generated child is intentionally not recreated.
        receipt = _read(pending.receipt)
        if receipt and not manager.directory(pending.job_id).exists():
            return {"candidate": candidate, "job_id": pending.job_id, "deleted": True}
        existing = pending.existing()
        if existing is not None:
            return {"candidate": candidate, "job_id": existing["id"]}
        previous = existing_refinements(manager, source_id, owner, manifest["config"], candidate)
        for prior in previous:
            organize_existing(manager, prior, owner, source_id, settings["project"], candidate)
        if previous and settings["existing"] == "skip":
            return {"candidate": candidate, "existing_job_ids": [p.name for p in previous], "skipped": True}
        directory = pending.stage()
        try:
            if options["stage"] == "scout":
                seed = manifest["config"]["seed"] + candidate - 1
                config, inputs = scout.prepare_render(manager, source_id, owner,
                    {"seed": seed, "refinement": settings["refinement"]}, directory, key)
                if "seed_hunter_input" not in inputs:
                    raise ValueError(f"Candidate {candidate} has no saved partial latent; automatic continuation cannot restart its schedule")
            else:
                width, height = final_size(manifest["config"])
                config, inputs = hunter.prepare_final(manager, source_id, owner,
                    {"candidate": candidate, "config": {"width": width, "height": height,
                        "steps": 2, "sampler": "er_sde", "scheduler": "linear_quadratic", **settings["refinement"]}},
                    directory, key)
            inputs["auto_refine_source"] = {"source_job_id": source_id, "candidate": candidate,
                                             "project": settings["project"], 'date': datetime.fromtimestamp(
                                                 manager._state(source_id, owner)['created_at']).strftime('%Y-%m-%d')}
            job = manager.launch(directory, owner, config, inputs)
            return {"candidate": candidate, "job_id": job["id"]}
        finally:
            pending.cleanup()


def process(manager, identity):
    """Safe to repeat across supervisor recovery, web-server restarts or retries."""
    if not JOB_ID.fullmatch(identity):
        return
    ticket = manager.root / ".automatic" / (identity + ".json")
    if not ticket.is_file() or ticket.is_symlink():
        return
    with _locked(ticket.with_suffix(".lock"), blocking=False) as lease:
        if lease is None:
            return
        directory = manager.directory(identity)
        state, manifest = _read(directory / "state.json"), _read(directory / "manifest.json")
        if not state or state.get("status") in {"failed", "stopped"}:
            ticket.unlink(missing_ok=True)
            return
        if state.get("status") != "completed":
            return
        try:
            if manifest.get("auto_refine_source") or manifest.get("project_exports"):
                publish_output(directory)
            # Completion only publishes already-requested outputs. Even an old
            # persisted ticket cannot silently schedule another GPU job.
            ticket.unlink(missing_ok=True)
        except Exception as exc:
            # Retain successful candidates and retry only unfinished submissions.
            # A publication failure must never mark the GPU render as failed.
            from src.h3_video import _redact
            if directory.is_dir():
                receipt = _read(directory / "auto-refine.json", {"jobs": []})
                receipt.update(status="retrying", error=_redact(str(exc)))
                _write(directory / "auto-refine.json", receipt)
            log.warning("Automatic refinement/output pending for %s: %s", identity, type(exc).__name__)


def summary(directory, manifest, state):
    options = manifest.get("config", {}).get("seed_hunter", {})
    source = manifest.get("auto_refine_source")
    settings = options.get("auto_refine", {})
    exports = manifest.get("project_exports", [])
    if not enabled(options) and not source and not exports:
        return None
    source = source or (exports[0] if exports else None)
    receipt = _read(directory / "auto-refine.json")
    return {"project": (source or settings).get("project"), "candidates": slots(options) if enabled(options) else [source["candidate"]],
            "status": receipt.get("status", state.get("status")), "jobs": receipt.get("jobs", []),
            "error": receipt.get("error"), "output_paths": state.get("project_output_paths", []),
            "source_paths": state.get("project_source_paths", []),
            "output_folder": str(project_directory(manifest["project_path"], state["owner"], (source or settings)["project"]))}


async def recovery_loop():
    from src.h3_video import H3JobManager
    manager = H3JobManager()
    def tick():
        for ticket in (manager.root / ".automatic").glob("*.json"):
            try:
                process(manager, ticket.stem)
            except Exception:
                log.exception("Automatic video recovery failed for %s", ticket.stem)
    while True:
        await asyncio.to_thread(tick)
        await asyncio.sleep(15)
