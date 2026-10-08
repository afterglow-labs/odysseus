"""Persistent, owner-scoped controls for the shared detached video queue.

Submission and dispatch share video-render.lock. Controls take that lock, then
individual job locks, and never wait for a running job's execution lease.
"""
import time

from src import h3_video as h3


def _managers(manager, scope="family"):
    if scope not in {"family", "all"}:
        raise ValueError("Queue scope must be family or all")
    result = [manager]
    if scope == "all" and manager.root.name in {"h3", "bfs"}:
        sibling = manager.root.with_name("bfs" if manager.root.name == "h3" else "h3")
        result.append(h3.H3JobManager(sibling, project=manager.project,
                                     runtime=manager.runtime, gallery_directory=manager.gallery_directory))
    return result


def _directories(manager):
    if manager.root.is_dir():
        for path in manager.root.iterdir():
            if path.is_dir() and not path.is_symlink() and h3.JOB_ID.fullmatch(path.name):
                yield path


def _revision(manifest):
    value = manifest.get("revision", 0)
    return value if type(value) is int and value >= 0 else 0


def queue_status(manager, owner):
    counts = {key: 0 for key in ("queued", "running", "completed", "failed", "stopped")}
    shared = dict(counts)
    for current in _managers(manager, "all"):
        for directory in _directories(current):
            state = h3._read(directory / "state.json")
            status = state.get("status")
            if state.get("owner") == owner and status in shared:
                shared[status] += 1
                if current.root == manager.root:
                    counts[status] += 1
    control = h3._read(manager.root.parent / "video-queue-control.json")
    paused = control.get("paused_owners", {})
    return {"paused": bool(isinstance(paused, dict) and paused.get(str(owner))),
            "family": manager.root.name, "counts": counts, "shared_counts": shared,
            "queued": counts["queued"], "running": counts["running"],
            "total_queued": shared["queued"], "total_running": shared["running"]}


def _targets_unlocked(manager, owner, scope):
    result = []
    for current in _managers(manager, scope):
        for directory in _directories(current):
            state = h3._read(directory / "state.json")
            if state.get("owner") != owner or state.get("status") != "queued":
                continue
            result.append({"id": directory.name, "family": current.root.name,
                           "revision": _revision(h3._read(directory / "manifest.json")),
                           "order": state.get("queue_order", state.get("created_at", 0))})
    result.sort(key=lambda row: (row["order"], row["id"]))
    return [{key: value for key, value in row.items() if key != "order"} for row in result]


def queued_targets(manager, owner, *, scope="family"):
    """An exact, FIFO-ordered owner/family snapshot for review and mutation."""
    manager.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with h3._locked(manager.root.parent / "video-render.lock"):
        return _targets_unlocked(manager, owner, scope)


def _receipt(results, manager, owner):
    successes = [row["id"] for row in results if row["ok"]]
    errors = [{key: row[key] for key in ("id", "family", "error") if key in row}
              for row in results if not row["ok"]]
    return {"results": results, "successes": successes, "errors": errors,
            "succeeded": len(successes), "failed": len(errors), "queue": queue_status(manager, owner)}


def _upgrade_unlocked(managers, owner=None):
    results = []
    for manager in managers:
        for directory in _directories(manager):
            state = h3._read(directory / "state.json")
            if state.get("status") != "queued" or (owner is not None and state.get("owner") != owner):
                continue
            if (state.get("supervisor_edit_version", 0) >= 1
                    and state.get("supervisor_control_version", 0) >= h3.SUPERVISOR_CONTROL_VERSION):
                continue
            row = {"id": directory.name, "family": manager.root.name}
            try:
                with h3._locked(directory / "job.lock"):
                    state = h3._read(directory / "state.json")
                    if state.get("status") != "queued" or (owner is not None and state.get("owner") != owner):
                        continue
                    manager.upgrade_queued_supervisor(directory, state)
                row.update(ok=True, upgraded=True)
            except (OSError, RuntimeError, ValueError) as exc:
                row.update(ok=False, error=h3._redact(exc))
            results.append(row)
    return results


def upgrade_waiting_supervisors(managers, owner=None):
    """Upgrade only queued waiters; leave running workers, inputs and order intact.

    Deployment may pass both concrete managers. One shared submission lock
    prevents a queued waiter from committing a new render during its upgrade.
    No pause state is changed by migration.
    """
    if isinstance(managers, h3.H3JobManager):
        managers = _managers(managers, "all")
    managers = list(managers)
    if not managers:
        return {"results": [], "upgraded": 0, "errors": []}
    parent = managers[0].root.parent
    if any(manager.root.parent != parent for manager in managers):
        raise ValueError("Video queue managers must share one queue directory")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with h3._locked(parent / "video-render.lock"):
        results = _upgrade_unlocked(managers, owner)
    return {"results": results, "upgraded": sum(row["ok"] for row in results),
            "errors": [row for row in results if not row["ok"]]}


def set_queue_paused(manager, owner, paused):
    if type(paused) is not bool:
        raise ValueError("Paused must be true or false")
    if not isinstance(owner, str) or not owner:
        raise ValueError("A queue owner is required")
    manager.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = manager.root.parent / "video-queue-control.json"
    with h3._locked(manager.root.parent / "video-render.lock"):
        # Write pause first so replacement waiters also see it. Holding this
        # lock blocks all dispatch while legacy waiters are being replaced.
        control = h3._read(path)
        owners = control.get("paused_owners", {})
        owners = owners if isinstance(owners, dict) else {}
        owners[str(owner)] = paused
        control.update(paused_owners=owners, updated_at=time.time())
        h3._write(path, control)
        upgraded = _upgrade_unlocked(_managers(manager, "all"), owner)
    errors = [row for row in upgraded if not row["ok"]]
    if errors:
        # A partial migration must not report success: old imported code does
        # not understand pause. Its individual failure is actionable.
        raise RuntimeError("Could not update all queued workers for pause/resume: " +
                           "; ".join(row["id"] + ": " + row["error"] for row in errors[:5]))
    return queue_status(manager, owner)


def cancel_queued(manager, owner, targets=None, *, delete=False, scope="family"):
    """Cancel/delete exactly the queued snapshot, never an active render.

    Explicit targets may carry revisions to reject stale browser selections.
    Cancellation commits every target under the submission lock; deletion then
    uses the existing terminal-job cleanup outside that non-reentrant lock.
    """
    managers = {current.root.name: current for current in _managers(manager, scope)}
    manager.root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    results, to_delete = [], []
    with h3._locked(manager.root.parent / "video-render.lock"):
        if targets is None:
            targets = _targets_unlocked(manager, owner, scope)
        if not isinstance(targets, list):
            raise ValueError("Queue targets must be a list")
        seen = set()
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("Each queued target must contain a job id")
            identifier, family = target.get("id"), target.get("family", manager.root.name)
            row = {"id": identifier, "family": family}
            try:
                if (not isinstance(family, str) or family not in managers
                        or not isinstance(identifier, str) or not h3.JOB_ID.fullmatch(identifier)):
                    raise ValueError("Invalid queued job target")
                if "revision" in target and (type(target["revision"]) is not int or target["revision"] < 0):
                    raise ValueError("Invalid queued job revision")
                if (family, identifier) in seen:
                    continue
                seen.add((family, identifier))
                current = managers[family]
                directory = current.directory(identifier)
                if not directory.is_dir():
                    raise FileNotFoundError("Video job not found")
                with h3._locked(directory / "job.lock"):
                    state = h3._read(directory / "state.json")
                    if state.get("owner") != owner:
                        raise FileNotFoundError("Video job not found")
                    if state.get("status") != "queued":
                        raise RuntimeError("Job is no longer queued; it was left unchanged")
                    if "revision" in target and target["revision"] != _revision(h3._read(directory / "manifest.json")):
                        raise RuntimeError("Job was edited after this selection; refresh the queue")
                    if state.get("worker") or h3._owned_workers(directory):
                        raise RuntimeError("Job has started rendering; it was left unchanged")
                    h3._verify_waiting_supervisor(directory, state.get("supervisor"))
                    (directory / "cancel").touch(mode=0o600)
                    h3._stop_supervisor(state.get("supervisor"))
                    state.update(status="stopped", phase="stopped", error=None, finished_at=time.time())
                    h3._write(directory / "state.json", state)
                row.update(ok=True, status="stopped")
                if delete:
                    to_delete.append((current, identifier, row))
            except (OSError, RuntimeError, ValueError) as exc:
                row.update(ok=False, error=h3._redact(exc))
            results.append(row)
    for current, identifier, row in to_delete:
        try:
            current.delete(identifier, owner)
            row.update(deleted=True)
        except (OSError, RuntimeError, ValueError) as exc:
            row.update(ok=False, error=h3._redact(exc))
    return _receipt(results, manager, owner)
