"""Owner-scoped video history pages without hydrating off-page artifacts."""
import math
import time

from src import h3_video as h3


def list_options(query, family):
    """Keep unpaged clients compatible; pagination is explicitly opt-in."""
    recipe = query.get("recipe")
    if recipe is not None and (family != "h3" or recipe != "seed_hunter"):
        raise ValueError("Only the H3 seed_hunter recipe filter is supported")
    stage = query.get("stage")
    if stage is not None and (recipe != "seed_hunter" or stage not in {"scout", "not_scout"}):
        raise ValueError("stage requires recipe=seed_hunter and must be scout or not_scout")
    paged = "page" in query or "page_size" in query
    if not paged:
        if "before" in query:
            raise ValueError("before requires page or page_size")
        return {"recipe": recipe, "stage": stage}
    try:
        page = int(query.get("page", "1"))
        page_size = int(query.get("page_size", "10"))
    except (TypeError, ValueError):
        raise ValueError("page and page_size must be integers") from None
    if page < 1 or not 1 <= page_size <= 50:
        raise ValueError("page must be at least 1 and page_size must be between 1 and 50")
    before = None
    if "before" in query:
        try:
            before = float(query["before"])
        except (TypeError, ValueError):
            raise ValueError("before must be a finite, nonnegative Unix timestamp") from None
        if not math.isfinite(before) or before < 0:
            raise ValueError("before must be a finite, nonnegative Unix timestamp")
    return {"page": page, "page_size": page_size, "before": before, "recipe": recipe, "stage": stage}


def _created_at(state):
    value = state.get("created_at", 0)
    return value if isinstance(value, (int, float)) and math.isfinite(value) else 0


def history(manager, owner, *, page=None, page_size=10, before=None, recipe=None, stage=None):
    """Cheap state scan first, then full views for only the selected IDs.

    Active receipts are reconciled even when they fall outside the page. Old
    terminal jobs need no process checks, logs, progress or candidate reads.
    """
    from src.video_queue_control import queue_status

    if page is None and recipe is None:
        return {"jobs": manager.jobs(owner), "queue": queue_status(manager, owner)}

    anchor = time.time() if before is None else before
    states = []
    processes = None
    if manager.root.is_dir():
        for directory in manager.root.iterdir():
            if not h3.JOB_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                continue
            state = h3._read(directory / "state.json")
            if not state or state.get("owner") != owner:
                continue
            if state.get("status") in h3.ACTIVE:
                if processes is None:
                    processes = h3._snapshot()
                try:
                    state = manager._state(directory.name, owner, snapshot=processes)
                except FileNotFoundError:
                    continue
            if recipe is not None:
                config = None
                stored_recipe = state.get("recipe")
                if stored_recipe is None:
                    # Legacy receipts have the recipe only in their manifest.
                    config = h3._read(directory / "manifest.json").get("config")
                    stored_recipe = config.get("recipe") if isinstance(config, dict) else None
                if stored_recipe != recipe:
                    continue
                if stage is not None:
                    if config is None:
                        config = h3._read(directory / "manifest.json").get("config", {})
                    options = config.get("seed_hunter", {}) if isinstance(config, dict) else {}
                    is_scout = options.get("stage") == "scout" if isinstance(options, dict) else False
                    if is_scout != (stage == "scout"):
                        continue
            states.append((directory.name, state))

    counts = {status: 0 for status in ("running", "queued", "completed", "failed", "stopped")}
    for _, state in states:
        status = state.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    states.sort(key=lambda item: (_created_at(item[1]), item[0]), reverse=True)
    if page is not None:
        states = [item for item in states if _created_at(item[1]) <= anchor]
        total = len(states)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        states = states[(page - 1) * page_size:page * page_size]

    queued = h3._queue_entries(manager.root)
    waiting = [path.name for path, state in queued if state["status"] == "queued"]
    positions = {identifier: index + 1 for index, identifier in enumerate(waiting)}
    jobs = []
    for identifier, _ in states:
        try:
            jobs.append(manager.view(identifier, owner, queue_positions=positions, snapshot=processes or {}))
        except FileNotFoundError:
            # Deletion may race a read; the next poll recomputes the last page.
            continue
    result = {"jobs": jobs, "counts": counts, "queue": queue_status(manager, owner)}
    if page is not None:
        result["pagination"] = {"page": page, "page_size": page_size, "total": total,
                                "total_pages": total_pages, "has_next": page < total_pages,
                                "before": anchor}
    return result
