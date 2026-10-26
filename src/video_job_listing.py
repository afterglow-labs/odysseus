"""Owner-scoped video history pages without hydrating off-page artifacts."""
import math
from pathlib import PurePosixPath
import time

from src import h3_video as h3


def list_options(query, family):
    """Keep unpaged clients compatible; pagination is explicitly opt-in."""
    search = query.get("q", "").strip()
    if len(search) > 512:
        raise ValueError("Job search must be at most 512 characters")
    search_options = {"q": search} if search else {}
    for key in ("created_after", "created_before"):
        if key not in query:
            continue
        try:
            value = float(query[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a finite, nonnegative Unix timestamp") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{key} must be a finite, nonnegative Unix timestamp")
        search_options[key] = value
    if ("created_after" in search_options and "created_before" in search_options
            and search_options["created_after"] >= search_options["created_before"]):
        raise ValueError("created_after must precede created_before")
    recipe = query.get("recipe")
    if recipe is not None and (family != "h3" or recipe != "seed_hunter"):
        raise ValueError("Only the H3 seed_hunter recipe filter is supported")
    stage = query.get("stage")
    if stage is not None and (recipe != "seed_hunter" or stage not in {"scout", "not_scout"}):
        raise ValueError("stage requires recipe=seed_hunter and must be scout or not_scout")
    grouped = query.get("group_refinements", "false")
    if grouped not in {"true", "false"} or grouped == "true" and (family != "h3" or recipe != "seed_hunter"):
        raise ValueError("group_refinements must be true or false and requires the H3 seed_hunter recipe")
    grouping = {"group_refinements": True} if grouped == "true" else {}
    if query.get('active_only', 'false') not in {'true', 'false'}:
        raise ValueError('active_only must be true or false')
    if query.get('active_only') == 'true':
        grouping['active_only'] = True
    if query.get('group_batches', 'false') not in {'true', 'false'}:
        raise ValueError('group_batches must be true or false')
    if query.get('group_batches') == 'true':
        grouping['group_batches'] = True
    paged = "page" in query or "page_size" in query
    if not paged:
        if "before" in query:
            raise ValueError("before requires page or page_size")
        return {"recipe": recipe, "stage": stage, **grouping, **search_options}
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
    return {"page": page, "page_size": page_size, "before": before, "recipe": recipe, "stage": stage, **grouping, **search_options}


def _search_text(identifier, state, manifest):
    """Search display metadata only, without opening media or render logs."""
    values = [identifier, state.get("status"), state.get("phase")]
    def filename(value):
        if isinstance(value, str):
            values.append(PurePosixPath(value.replace("\\", "/")).name)
    filename(state.get("filename"))
    filename(manifest.get("source_name"))
    names = manifest.get("input_names", {})
    if isinstance(names, dict):
        for labels in names.values():
            for name in labels if isinstance(labels, list) else [labels]:
                filename(name)
    config = manifest.get("config", {})
    if isinstance(config, dict):
        values.append(config.get("prompt"))
        options = config.get("seed_hunter", {})
        if isinstance(options, dict):
            values.append(options.get("stage"))
            automatic = options.get("auto_refine", {})
            if isinstance(automatic, dict):
                values.append(automatic.get("project"))
    exports = manifest.get("project_exports", [])
    for spec in [manifest.get("auto_refine_source"), *(exports if isinstance(exports, list) else [])]:
        if isinstance(spec, dict):
            values.append(spec.get("project"))
    return "\n".join(value for value in values if isinstance(value, str)).casefold()


def _created_at(state):
    value = state.get("created_at", 0)
    return value if isinstance(value, (int, float)) and math.isfinite(value) else 0


def history(manager, owner, *, page=None, page_size=10, before=None, recipe=None, stage=None, group_refinements=False, q="", group_batches=False, active_only=False, created_after=None, created_before=None):
    """Cheap state scan first, then full views for only the selected IDs.

    Active receipts are reconciled even when they fall outside the page. Old
    terminal jobs need no process checks, logs, progress or candidate reads.
    """
    from src.video_queue_control import queue_status

    terms = q.casefold().split()
    if page is None and recipe is None and not terms and not group_batches and not active_only and created_after is None and created_before is None:
        return {"jobs": manager.jobs(owner), "queue": queue_status(manager, owner)}

    anchor = time.time() if before is None else before
    states = []
    records = {}
    search_text = {}
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
            manifest = None
            if recipe is not None:
                config = None
                stored_recipe = state.get("recipe")
                if stored_recipe is None:
                    # Legacy receipts have the recipe only in their manifest.
                    manifest = h3._read(directory / "manifest.json")
                    config = manifest.get("config")
                    stored_recipe = config.get("recipe") if isinstance(config, dict) else None
                if stored_recipe != recipe:
                    continue
                if stage is not None:
                    if config is None:
                        if manifest is None:
                            manifest = h3._read(directory / "manifest.json")
                        config = manifest.get("config", {})
                    options = config.get("seed_hunter", {}) if isinstance(config, dict) else {}
                    is_scout = options.get("stage") == "scout" if isinstance(options, dict) else False
                    if manifest and manifest.get('result_edit', {}).get('workflow_stage') == 'scout':
                        is_scout = True
                    if is_scout != (stage == "scout"):
                        continue
            states.append((directory.name, state))
            if group_refinements or group_batches or terms:
                if manifest is None:
                    manifest = h3._read(directory / "manifest.json")
                if group_refinements or group_batches:
                    records[directory.name] = (state, manifest)
                if terms:
                    search_text[directory.name] = _search_text(directory.name, state, manifest)

    counts = {status: 0 for status in ("running", "queued", "completed", "failed", "stopped")}
    for _, state in states:
        status = state.get("status")
        if isinstance(status, str):
            counts[status] = counts.get(status, 0) + 1
    states.sort(key=lambda item: (_created_at(item[1]), item[0]), reverse=True)
    # Use the dispatcher's shared H3/BFS ordering for queue positions. Running
    # jobs are not waiting positions; they must not shift position 1 to 2.
    waiting = [path.name for path, state in h3._queue_entries(manager.root) if state['status'] == 'queued']
    positions = {identifier: index + 1 for index, identifier in enumerate(waiting)}
    def queue_rank(identifier, state):
        return (0, 0) if state.get('status') == 'running' else (1, positions.get(identifier, float('inf')))
    children = {}
    if group_refinements:
        from src.video_job_groups import refinement_groups, attach_refinements
        parents, children = refinement_groups(manager, owner, records)
        states = [item for item in states if item[0] not in parents]
    if terms:
        # Grouped cards retain all their refinements when their own metadata or
        # one child's metadata matches. Bulk selection uses this same endpoint.
        def matches(identifier):
            return any(all(term in search_text.get(member, "") for term in terms)
                       for member in [identifier, *children.get(identifier, ())])
        states = [item for item in states if matches(item[0])]
    batch_cards = {}
    batch_members = {}
    if group_batches:
        from src.video_job_actions import summary, members
        batch_ids = {records[i][1].get('batch', {}).get('id') for i, _ in states}
        covered = set()
        for batch_id in batch_ids - {None}:
            card = summary(records, batch_id)
            batch_cards[card['id']] = card
            batch_members[card['id']] = members(records, batch_id, batch=True)[1]
            covered.update(batch_members[card['id']])
            pending_positions = [positions[i] for i in batch_members[card['id']] if i in positions]
            if pending_positions:
                card['queue_position'] = min(pending_positions)
        states = [(i, s) for i, s in states if i not in covered]
        states.extend((i, card) for i, card in batch_cards.items())
        states.sort(key=lambda item: (_created_at(item[1]), item[0]), reverse=True)
    if active_only:
        states = [(i, s) for i, s in states if s.get('status') in h3.ACTIVE
                  or any(records[child][0].get('status') in h3.ACTIVE for child in children.get(i, ()))
                  or any(s.get('counts', {}).get(status) for status in h3.ACTIVE)]
        def group_rank(item):
            identifier, state = item
            entries = batch_members.get(identifier, [identifier, *children.get(identifier, ())])
            return min((queue_rank(i, records.get(i, (state,))[0]) for i in entries
                        if records.get(i, (state,))[0].get('status') in h3.ACTIVE), default=(2, float('inf')))
        states.sort(key=group_rank)
    # Bounds are supplied by the client using its local calendar. They apply to
    # the creation date of the visible job/batch; its saved children stay together.
    states = [(i, s) for i, s in states
              if (created_after is None or _created_at(s) >= created_after)
              and (created_before is None or _created_at(s) < created_before)]
    if page is not None:
        states = [item for item in states if _created_at(item[1]) <= anchor]
        total = len(states)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        states = states[(page - 1) * page_size:page * page_size]

    jobs = []
    for identifier, _ in states:
        if identifier in batch_cards:
            jobs.append(batch_cards[identifier])
            continue
        try:
            job = manager.view(identifier, owner, queue_positions=positions, snapshot=processes or {})
            if group_refinements:
                attach_refinements(manager, owner, job, children, queue_positions=positions, snapshot=processes or {})
            jobs.append(job)
        except FileNotFoundError:
            # Deletion may race a read; the next poll recomputes the last page.
            continue
    result = {"jobs": jobs, "counts": counts, "queue": queue_status(manager, owner)}
    if created_after is not None or created_before is not None:
        result["date_filter"] = {"created_after": created_after, "created_before": created_before}
    if terms:
        result["search_query"] = q.strip()
    if group_refinements:
        result["grouped_refinements"] = True
    if page is not None:
        result["pagination"] = {"page": page, "page_size": page_size, "total": total,
                                "total_pages": total_pages, "has_next": page < total_pages,
                                "before": anchor}
    return result
