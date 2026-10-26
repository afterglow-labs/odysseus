"""Revision-checked queue edits and durable reruns of saved video inputs."""
from contextlib import ExitStack
from copy import deepcopy
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid

from src.h3_video import ACTIVE, JOB_ID, TERMINAL, _locked, _read, _write
from src.video_job_edit import _draft, _inputs, revision, validate, persist_legacy_inputs, input_source_name
from src.video_submission import UUID, ensure_upload_space
from src.video_workflow import specification

MAX_TARGETS = 1000


def targets(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TARGETS:
        raise ValueError(f"Select from 1 to {MAX_TARGETS} video jobs")
    seen, result = set(), []
    for item in value:
        if (not isinstance(item, dict) or item.keys() - {"id", "revision"}
                or not isinstance(item.get("id"), str) or not JOB_ID.fullmatch(item["id"])
                or type(item.get("revision")) is not int or item["revision"] < 0):
            raise ValueError("Each selected job needs its ID and current revision")
        if item["id"] in seen:
            raise ValueError("A video job was selected more than once")
        seen.add(item["id"])
        result.append(dict(item))
    return result


def _owned(manager, job_id, owner):
    directory = manager.directory(job_id)
    state = _read(directory / "state.json")
    if directory.is_symlink() or not state or state.get("owner") != owner:
        raise FileNotFoundError("Video job not found")
    return directory, state


def _order(row):
    order = row.get("queue_order")
    if order is None:
        order = int((row.get("created_at") or 0) * 1_000_000_000)
    return order, row.get("id", "")


def _light(directory, state, inventory, family, *, resolved_paths=None):
    manifest = _read(directory / "manifest.json")
    config = manifest.get("config")
    result = {key: state.get(key) for key in ("id", "status", "created_at", "started_at", "finished_at", "queue_order")}
    result.update(revision=revision(manifest), source_name=manifest.get("source_name", state.get("source_name")),
                  config=_draft(config, inventory, family, resolved_paths=resolved_paths) if isinstance(config, dict) else {})
    if family == "h3" and isinstance(config, dict):
        from src.h3_loras import frozen_components
        result["lora_components"] = frozen_components(config, inventory, resolved_paths=resolved_paths)
        if config.get("recipe"):
            result["recipe"] = config["recipe"]
    return result


def list_jobs(manager, owner, family, status="queued"):
    if status not in {"queued", "finished", "all"}:
        raise ValueError("Choose queued, finished, or all jobs")
    entries = []
    if manager.root.is_dir():
        for directory in manager.root.iterdir():
            if not directory.is_dir() or directory.is_symlink() or not JOB_ID.fullmatch(directory.name):
                continue
            state = _read(directory / "state.json")
            if state.get("owner") != owner:
                continue
            if status == "queued" and state.get("status") != "queued":
                continue
            if status == "finished" and state.get("status") not in TERMINAL:
                continue
            entries.append((directory, state))
    inventory = manager.submission_inventory() if entries else {"components": []}
    jobs, resolved_paths = [], {}
    for directory, _ in entries:
        # Deletion/edit can change the snapshot while inventory is scanned.
        try:
            with _locked(directory / "job.lock"):
                _, state = _owned(manager, directory.name, owner)
                if status == "queued" and state.get("status") != "queued":
                    continue
                if status == "finished" and state.get("status") not in TERMINAL:
                    continue
                jobs.append(_light(directory, state, inventory, family, resolved_paths=resolved_paths))
        except FileNotFoundError:
            continue
    return sorted(jobs, key=_order)


def _patched(config, patch, inventory, family, inputs, *, resolved_paths=None):
    if family == "h3":
        from src.h3_loras import frozen_inventory
        inventory = frozen_inventory(config, inventory)
    if not isinstance(patch, dict):
        raise ValueError("Parameter changes must be a JSON object")
    settings, roles, _ = specification(family, config)
    allowed = settings - {"mode", "workflow_id", "recipe"} | {"gpu"}
    allowed |= roles | {"vae_gpu", "loras"} if family == "h3" else {"components"}
    if patch.keys() - allowed:
        raise ValueError("Cannot bulk edit setting: " + sorted(patch.keys() - allowed)[0])
    raw = _draft(config, inventory, family, resolved_paths=resolved_paths)
    if family == "bfs" and "components" in patch:
        changes = patch["components"]
        if not isinstance(changes, dict) or changes.keys() - roles:
            raise ValueError("Choose compatible BFS component roles")
        raw["components"] = {**raw["components"], **changes}
    raw.update({key: value for key, value in patch.items() if key not in {"components", "seed_hunter"}})
    if "seed_hunter" in patch:
        if not isinstance(patch["seed_hunter"], dict):
            raise ValueError("Seed Hunter changes must be a JSON object")
        raw["seed_hunter"] = {**raw.get("seed_hunter", {}), **patch["seed_hunter"]}
    if family == "h3" and "loras" not in patch:
        if "lora" in patch:
            # Older clients replace their single adapter explicitly.
            raw.pop("loras", None)
        elif "lora_scale" in patch and raw.get("loras"):
            raw["loras"][0]["strength"] = patch["lora_scale"]
    updated = validate(raw, inventory, inputs, family)
    if family == "h3":
        from src.h3_seed_hunter import guard_edit
        guard_edit(config, updated)
    return updated


def _from_base(config, base_config, inventory, family, inputs):
    """New-job settings with this source's media, never its cached conditioning."""
    raw = deepcopy(base_config)
    if family == "h3":
        if "seed_hunter" in raw and not isinstance(raw["seed_hunter"], dict):
            raise ValueError("Seed Hunter settings must be a JSON object")
        if raw.get("seed_hunter", {}).get("stage") == "final":
            raise ValueError("Choose base settings for a new job, or use Edit & rerun for a saved refinement")
        # Clip selections and RefMod strengths describe the saved files. The
        # base form may have unrelated attachments (or none); never substitute
        # their indices or crop offsets into this job's reference order.
        raw.pop("reference_video_ranges", None)
        if "reference_video_ranges" in config:
            raw["reference_video_ranges"] = deepcopy(config["reference_video_ranges"])
        if isinstance(raw.get("seed_hunter"), dict):
            raw["seed_hunter"]["refmod_strengths"] = deepcopy(config.get("seed_hunter", {}).get("refmod_strengths", []))
    return validate(raw, inventory, inputs, family)


def _prepare_locked(manager, owner, family, chosen, patch, inventory, *, queued, base_config=None, in_place=False):
    prepared, resolved_paths = [], {}
    for item in chosen:
        directory, state = _owned(manager, item["id"], owner)
        if queued and (state.get("status") != "queued" or (directory / "cancel").exists()):
            raise RuntimeError("A selected job has started or left the queue. Refresh before editing.")
        manifest = _read(directory / "manifest.json")
        if in_place and state.get('status') in ACTIVE:
            prepared.append({'directory': directory, 'state': state, 'manifest': manifest, 'already_active': True})
            continue
        if revision(manifest) != item["revision"]:
            raise RuntimeError("A selected job was edited elsewhere. Refresh to load its latest settings.")
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise ValueError("A selected job has no usable workflow settings")
        _, _, extensions = specification(family, config)
        inputs = _inputs(directory, manifest, extensions)
        updated = (_from_base(config, base_config, inventory, family, inputs) if base_config is not None
                   else _patched(config, patch, inventory, family, inputs, resolved_paths=resolved_paths))
        if family == "h3" and base_config is None:
            from src.h3_seed_hunter import guard_saved_refinement
            guard_saved_refinement(directory, manifest, updated)
        prepared.append({"directory": directory, "state": state, "manifest": manifest, "config": updated, "inputs": inputs})
    return sorted(prepared, key=lambda item: _order(item["state"]))


def edit_jobs(manager, owner, family, chosen, patch):
    chosen = targets(chosen)
    if not isinstance(patch, dict) or not patch:
        raise ValueError("Choose at least one parameter to change")
    # Ownership must be established before creating locks or scanning inventory.
    for item in chosen:
        _owned(manager, item["id"], owner)
    inventory = manager.inventory()
    with _locked(manager.root.parent / "video-render.lock"), ExitStack() as stack:
        for item in sorted(chosen, key=lambda item: item["id"]):
            stack.enter_context(_locked(manager.directory(item["id"]) / "job.lock"))
        prepared = _prepare_locked(manager, owner, family, chosen, patch, inventory, queued=True)
        for item in prepared:
            manager.upgrade_queued_supervisor(item["directory"], item["state"])
        written = []
        try:
            for item in prepared:
                original = item["manifest"]
                normalized = persist_legacy_inputs(original, item["inputs"], family)
                _write(item["directory"] / "manifest.json", {**normalized, "config": item["config"], "revision": revision(original) + 1})
                written.append(item)
        except OSError as exc:
            rollback_errors = []
            for item in reversed(written):
                try:
                    _write(item["directory"] / "manifest.json", item["manifest"])
                except OSError:
                    rollback_errors.append(item["state"]["id"])
            if rollback_errors:
                raise RuntimeError("Disk error while saving; refresh the queue to inspect revisions for: " + ", ".join(rollback_errors)) from exc
            raise RuntimeError("Could not save the bulk edit; all original settings were restored") from exc
        resolved_paths = {}
        jobs = [_light(item["directory"], item["state"], inventory, family, resolved_paths=resolved_paths) for item in prepared]
    from src.workflow_prompt_history import remember_job_prompts
    remember_job_prompts(manager, owner, [item["config"] for item in prepared])
    return {"updated": len(jobs), "succeeded": len(jobs), "failed": 0, "jobs": jobs}


def _clone_file(source, destination):
    # Saved uploads are immutable. A hardlink survives deleting either job and
    # avoids making 200 extra copies of large reference videos on rerun.
    try:
        os.link(source, destination)
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}:
            raise
        ensure_upload_space(destination.parent, Path(source).stat().st_size)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)


def _stage_rerun(manager, owner, family, chosen, patch, key, receipt_path, receipt, *, base_config=None, new_group=False, in_place=False, retry_failed=False):
    inventory = manager.inventory()
    staged = []
    with _locked(manager.root.parent / "video-render.lock"), ExitStack() as stack:
        for item in sorted(chosen, key=lambda item: item["id"]):
            stack.enter_context(_locked(manager.directory(item["id"]) / "job.lock"))
        prepared = _prepare_locked(manager, owner, family, chosen, patch, inventory, queued=False, base_config=base_config, in_place=in_place)
        try:
            entries = []
            for item in prepared:
                original_id = item["state"]["id"]
                if item.get('already_active'):
                    entries.append({'id': original_id, 'source_id': original_id, 'skipped_active': True, 'accepted': True})
                    continue
                if in_place:
                    entries.append({'id': original_id, 'source_id': original_id, 'config': item['config'],
                                    'retry': True, 'allow_completed': True, 'revision': revision(item['manifest']), 'accepted': False})
                    continue
                if (not patch or retry_failed) and base_config is None and not new_group and item['state']['status'] in {'failed', 'stopped'}:
                    entries.append({'id': original_id, 'source_id': original_id, 'config': item['config'],
                                    'retry': True, 'revision': revision(item['manifest']), 'accepted': False})
                    continue
                identifier = hashlib.sha256(f"rerun\0{family}\0{owner}\0{key}\0{original_id}".encode()).hexdigest()[:32]
                directory = manager.directory(identifier)
                # Only this owner's durable request receipt authorizes recovery
                # of abandoned staging; never replace a published job.
                if directory.exists():
                    if (directory / "state.json").exists() or identifier not in receipt.get("planned_ids", []):
                        raise RuntimeError("A rerun staging directory is already in use")
                    shutil.rmtree(directory)
                if identifier not in receipt.setdefault("planned_ids", []):
                    receipt["planned_ids"].append(identifier)
                _write(receipt_path, receipt)
                directory = manager.stage(identifier)
                staged.append(directory)
                uploads, names = {}, {}
                for field, values in item["inputs"].items():
                    paths = []
                    if values:
                        names[field] = [value["name"] for value in values]
                    for value in values:
                        target = directory / (uuid.uuid4().hex + Path(value["path"]).suffix.lower())
                        _clone_file(value["path"], target)
                        paths.append(str(target.resolve()))
                    uploads[field] = paths if family == "h3" and field not in {"first_frame", "last_frame", "source_video"} else paths[0] if paths else []
                if base_config is None and family == "h3" and (item["config"].get("seed_hunter", {}).get("stage") == "final" or item["manifest"].get("seed_hunter_input")):
                    from src.h3_seed_hunter import clone_snapshot
                    uploads["seed_hunter_input"] = clone_snapshot(item["directory"], directory, item["manifest"].get("seed_hunter_input"))
                    uploads["seed_hunter_source"] = item["manifest"].get("seed_hunter_source", {})
                if base_config is None and item['manifest'].get('result_edit'):
                    uploads['result_edit'] = deepcopy(item['manifest']['result_edit'])
                    uploads['job_group_parent'] = item['manifest'].get('job_group_parent')
                    uploads['seed_hunter_source'] = item['manifest'].get('seed_hunter_source', {})
                if new_group:
                    from src.video_job_actions import parent_id
                    parent = parent_id(item['manifest'])
                    if parent in {target['id'] for target in chosen}:
                        uploads['job_group_parent'] = hashlib.sha256(f"rerun\0{family}\0{owner}\0{key}\0{parent}".encode()).hexdigest()[:32]
                    batch = item['manifest'].get('batch')
                    if batch:
                        uploads['batch'] = {**batch, 'id': uuid.uuid5(uuid.NAMESPACE_URL, key + ':' + batch['id']).hex}
                _write(directory / "submission.json", {"input_names": names, "source_name": input_source_name(item["inputs"], family),
                                                       "submission_id": key, "rerun_of": original_id})
                entries.append({"id": identifier, "source_id": original_id, "config": item["config"], "uploads": uploads, "accepted": False})
            receipt.update(prepared=True, entries=entries)
            _write(receipt_path, receipt)
        except BaseException:
            for directory in staged:
                if not (directory / "state.json").exists():
                    shutil.rmtree(directory, ignore_errors=True)
            raise


def rerun_jobs(manager, owner, family, chosen, patch, request_id, *, base_config=None, new_group=False, in_place=False, retry_failed=False):
    chosen = targets(chosen)
    if type(retry_failed) is not bool or retry_failed and (base_config is not None or new_group or in_place):
        raise ValueError('Retry uses this job’s saved inputs and individual parameter changes')
    if not isinstance(patch, dict):
        raise ValueError("Parameter changes must be a JSON object")
    if base_config is not None and (not isinstance(base_config, dict) or patch):
        raise ValueError("Supply base settings or individual parameter changes, not both")
    if in_place and (patch or base_config is not None or new_group):
        raise ValueError('Rerun in place uses the saved settings and job group')
    if not isinstance(request_id, str) or not UUID.fullmatch(request_id):
        raise ValueError("Rerun requests need a UUID request_id")
    key = uuid.UUID(request_id).hex
    digest = hashlib.sha256(f"{family}\0{owner}\0{key}".encode()).hexdigest()
    parameters = {"base_config": base_config} if base_config is not None else {"patch": patch}
    if new_group:
        parameters['new_group'] = True
    if in_place:
        parameters['in_place'] = True
    if retry_failed:
        parameters['retry_failed'] = True
    fingerprint = hashlib.sha256(json.dumps({"jobs": sorted(chosen, key=lambda item: item["id"]), **parameters},
                                           sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    # First submission verifies every source. Retrying a persisted request can
    # succeed even after the source was removed because its inputs were cloned.
    root = manager.root / ".bulk-reruns"
    receipt_path = root / (digest + ".json")
    if not receipt_path.exists():
        for item in chosen:
            _owned(manager, item["id"], owner)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _locked(root / ("lock-" + str(int(digest[:2], 16) % 64))):
        receipt = _read(receipt_path)
        if receipt_path.exists():
            if (receipt.get("owner") != owner or receipt.get("request_id") != key
                    or receipt.get("fingerprint") != fingerprint):
                raise RuntimeError("This rerun request ID was already used with different jobs or parameters")
        else:
            receipt = {"owner": owner, "request_id": key, "fingerprint": fingerprint, "created_at": time.time()}
            _write(receipt_path, receipt)
        if not receipt.get("prepared"):
            _stage_rerun(manager, owner, family, chosen, patch, key, receipt_path, receipt, base_config=base_config, new_group=new_group, in_place=in_place, retry_failed=retry_failed)
        jobs, errors = [], []
        for entry in receipt["entries"]:
            directory = manager.directory(entry["id"])
            state = _read(directory / "state.json")
            if entry.get('retry'):
                try:
                    from src.video_job_retry import retry_job
                    if not entry.get('accepted'):
                        state = retry_job(manager, owner, entry['id'], entry['config'], entry['revision'], key,
                                          allow_completed=entry.get('allow_completed', False))
                        entry['skipped_active'] = state.get('retry_request_id') != key
                    entry['accepted'] = True
                except (FileNotFoundError, OSError, RuntimeError) as exc:
                    errors.append({'id': entry['id'], 'error': str(exc)})
                    continue
            elif state:
                if state.get("owner") != owner:
                    errors.append({"id": entry["id"], "error": "The rerun job is unavailable"})
                    continue
                entry["accepted"] = True
            elif entry.get("accepted"):
                errors.append({"id": entry["id"], "error": "This rerun job was deleted; choose Run again to make a new copy"})
                continue
            else:
                try:
                    manager.launch(directory, owner, entry["config"], entry["uploads"])
                    entry["accepted"] = True
                    state = _read(directory / "state.json")
                except (OSError, RuntimeError) as exc:
                    # launch may have published a failed receipt before the
                    # supervisor spawn failed. It is still an accepted job.
                    state = _read(directory / "state.json")
                    entry["accepted"] = bool(state)
                    errors.append({"id": entry["id"], "error": str(exc)})
                    _write(receipt_path, receipt)
                    continue
            _write(receipt_path, receipt)
            if not state or state.get("owner") != owner:
                # Another request can delete the accepted copy immediately
                # after launch returns. Retain its tombstone for retry safety,
                # but do not report a vanished job as a successful submission.
                errors.append({"id": entry["id"], "error": "This rerun job was deleted or is unavailable; choose Run again to make a new copy"})
                continue
            # No full view here: 200 jobs should not read 200 render log tails
            # or enumerate all system processes 200 times.
            jobs.append({"id": entry["id"], "source_id": entry["source_id"], "status": state.get("status"),
                         "retried": bool(entry.get('retry')) and not entry.get('skipped_active', False),
                         "skipped_active": bool(entry.get('skipped_active')),
                         "revision": revision(_read(directory / "manifest.json")), "source_name": state.get("source_name")})
        return {"rerun": len(jobs), "succeeded": len(jobs), "failed": len(errors), "jobs": jobs,
                "queued_again": sum(job['retried'] for job in jobs),
                "skipped_active": sum(job['skipped_active'] for job in jobs),
                "errors": errors, "request_id": key}
