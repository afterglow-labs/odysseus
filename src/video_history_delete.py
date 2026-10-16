"""Confirmed, resumable deletion of an owner's terminal video history.

The preview records exact job identities/revisions across every history page.
Each confirmation call consumes a bounded chunk, with an on-disk receipt after
every item. New jobs and active renders never join that reviewed snapshot.
"""
import time
import uuid

from src import h3_video as h3
from src.video_queue_control import _directories, _managers, _revision

CHUNK_SIZE = 10
CHUNK_SECONDS = 8
CONFIRM_SECONDS = 3600


class HistoryDeletionBusy(RuntimeError):
    """An identical confirmed snapshot is currently processing another call."""


def _scope(manager, scope, recipe, stage):
    if not isinstance(scope, str) or scope not in {"workflow", "all"}:
        raise ValueError("Choose workflow or all history scope")
    if scope == "all":
        if recipe is not None or stage is not None:
            raise ValueError("All history scope does not accept workflow filters")
    elif recipe is not None:
        if manager.root.name != "h3" or not isinstance(recipe, str) or recipe != "seed_hunter":
            raise ValueError("Only H3 supports the seed_hunter history filter")
        if not isinstance(stage, str) or stage not in {"scout", "not_scout"}:
            raise ValueError("Choose scout or not_scout for Seed Hunter history")
    elif stage is not None:
        raise ValueError("A history stage requires the seed_hunter recipe")


def _matches(manager, state, manifest, scope, recipe, stage):
    if scope == "all" or manager.root.name == "bfs":
        return True
    config = manifest.get("config", {})
    config = config if isinstance(config, dict) else {}
    saved_recipe = state.get("recipe", config.get("recipe"))
    if recipe is None:
        return saved_recipe != "seed_hunter"
    options = config.get("seed_hunter", {})
    scout = isinstance(options, dict) and options.get("stage") == "scout"
    return saved_recipe == recipe and scout == (stage == "scout")


def _storage(manager):
    directory = manager.root.parent / ".history-deletions"
    if directory.is_symlink():
        raise RuntimeError("The history deletion store is unavailable")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def preview(manager, owner, *, scope="workflow", recipe=None, stage=None):
    _scope(manager, scope, recipe, stage)
    managers = _managers(manager, "all" if scope == "all" else "family")
    directory = _storage(manager)
    targets, active = [], 0
    counts = {status: 0 for status in sorted(h3.TERMINAL)}
    with h3._locked(manager.root.parent / "video-render.lock"):
        for current in managers:
            for path in _directories(current):
                state = h3._read(path / "state.json")
                if state.get("owner") != owner:
                    continue
                manifest = h3._read(path / "manifest.json")
                if not _matches(current, state, manifest, scope, recipe, stage):
                    continue
                status = state.get("status")
                if status in h3.ACTIVE:
                    active += 1
                elif status in h3.TERMINAL:
                    targets.append({"id": path.name, "family": current.root.name,
                                    "revision": _revision(manifest), "status": status})
                    counts[status] += 1
    targets.sort(key=lambda row: (row["family"], row["id"]))
    identifier = uuid.uuid4().hex
    saved = {"snapshot_id": identifier, "owner": owner, "family": manager.root.name,
             "scope": scope, "recipe": recipe, "stage": stage, "targets": targets,
             "results": {}, "preserved_active": active, "counts": counts,
             "expires_at": time.time() + CONFIRM_SECONDS}
    h3._write(directory / (identifier + ".json"), saved)
    return {key: saved[key] for key in ("snapshot_id", "scope", "expires_at", "preserved_active", "counts")} | {
        "count": len(targets)}


def _result(saved):
    rows = list(saved["results"].values())
    errors = [{"id": row["id"], "family": row["family"], "error": row["error"]}
              for row in rows if "error" in row]
    pending = len(saved["targets"]) - len(rows)
    return {"snapshot_id": saved["snapshot_id"], "scope": saved["scope"],
            "total": len(saved["targets"]), "processed": len(rows), "pending": pending,
            "done": pending == 0, "deleted": sum(bool(row.get("deleted")) for row in rows),
            "skipped": sum(bool(row.get("skipped")) for row in rows),
            "errors": errors, "results": rows, "preserved_active": saved["preserved_active"]}


def delete(manager, owner, snapshot_id, *, confirm=False, retry_errors=False):
    if confirm is not True or type(retry_errors) is not bool:
        raise ValueError("Confirm deletion of the reviewed video history")
    if not isinstance(snapshot_id, str) or not h3.JOB_ID.fullmatch(snapshot_id):
        raise ValueError("Invalid history deletion snapshot")
    directory = _storage(manager)
    path = directory / (snapshot_id + ".json")
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("History deletion snapshot not found")
    # Serialize retries/double taps without holding the render queue lock for
    # the whole operation. Individual job deletion owns its existing locks.
    with h3._locked(directory / (snapshot_id + ".lock"), blocking=False) as lease:
        if lease is None:
            raise HistoryDeletionBusy("This history deletion is already processing; retry shortly")
        saved = h3._read(path)
        if saved.get("owner") != owner or saved.get("family") != manager.root.name:
            raise FileNotFoundError("History deletion snapshot not found")
        if not saved.get("confirmed") and time.time() > saved["expires_at"]:
            raise RuntimeError("This history preview expired. Review the history count again")
        managers = {item.root.name: item for item in _managers(manager, "all" if saved["scope"] == "all" else "family")}
        if retry_errors:
            if len(saved["results"]) != len(saved["targets"]):
                raise ValueError("Finish the pending deletion before retrying failed items")
            saved["results"] = {key: row for key, row in saved["results"].items() if "error" not in row}
        saved["confirmed"] = True
        h3._write(path, saved)
        started, processed = time.monotonic(), 0
        for target in saved["targets"]:
            key = target["family"] + ":" + target["id"]
            if key in saved["results"]:
                continue
            if processed and (processed >= CHUNK_SIZE or time.monotonic() - started >= CHUNK_SECONDS):
                break
            row = {"id": target["id"], "family": target["family"], "deleted": False}
            try:
                managers[target["family"]].delete(target["id"], owner, expected_revision=target["revision"])
                row["deleted"] = True
            except FileNotFoundError:
                row.update(skipped=True, reason="This job was already removed")
            except h3.VideoJobChanged as exc:
                row.update(skipped=True, reason=str(exc))
            except (OSError, ValueError, RuntimeError) as exc:
                row["error"] = h3._redact(exc)
            saved["results"][key] = row
            h3._write(path, saved)
            processed += 1
        return _result(saved)
