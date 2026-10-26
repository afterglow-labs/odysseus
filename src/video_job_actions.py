"""Owner-scoped job trees, batch cards and explicit queue actions."""
from collections import Counter
import hashlib
import time
import uuid

from src import h3_video as h3
from src.video_queue_control import queue_status


def records(manager, owner):
    result = {}
    for directory in manager.root.iterdir() if manager.root.is_dir() else ():
        if directory.is_symlink() or not directory.is_dir() or not h3.JOB_ID.fullmatch(directory.name):
            continue
        state = h3._read(directory / 'state.json')
        if state.get('owner') == owner:
            result[directory.name] = (state, h3._read(directory / 'manifest.json'))
    return result


def parent_id(manifest):
    return (manifest.get('job_group_parent') or manifest.get('seed_hunter_source', {}).get('source_job_id')
            or manifest.get('auto_refine_source', {}).get('source_job_id'))


def members(saved, identity, batch=False):
    roots = [i for i, (_, m) in saved.items() if m.get('batch', {}).get('id') == identity and not parent_id(m)] if batch else [identity]
    if not roots or any(i not in saved for i in roots):
        raise FileNotFoundError('Video job not found')
    selected = set(roots)
    while True:
        descendants = {i for i, (_, m) in saved.items() if parent_id(m) in selected}
        if descendants <= selected:
            break
        selected |= descendants
    return roots, sorted(selected, key=lambda i: (saved[i][0].get('created_at', 0), i))


def batch_metadata(form):
    values = form.getlist('batch')
    if not values:
        return None
    import json
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError('Invalid batch metadata')
    value = json.loads(values[0])
    if not isinstance(value, dict) or set(value) != {'id', 'name', 'count'}:
        raise ValueError('Supply a batch ID, name and file count')
    try:
        identity = uuid.UUID(value['id']).hex
    except (ValueError, TypeError, AttributeError):
        raise ValueError('Batch ID must be a UUID') from None
    if (not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 120
            or type(value['count']) is not int or not 1 <= value['count'] <= 10000):
        raise ValueError('Invalid batch name or file count')
    return {**value, 'id': identity, 'name': value['name'].strip()}


def summary(saved, identity):
    roots, all_ids = members(saved, identity, batch=True)
    counts = dict(Counter(saved[i][0].get('status') for i in all_ids))
    metadata = saved[roots[0]][1]['batch']
    config = saved[roots[0]][1].get('config', {})
    status = next((s for s in ('running', 'queued', 'failed', 'stopped', 'completed') if counts.get(s)), 'queued')
    return {'id': 'batch-' + identity, 'batch': metadata, 'batch_count': len(roots), 'counts': counts,
            'source_name': metadata['name'], 'status': status, 'phase': 'batch',
            'created_at': min(saved[i][0].get('created_at', 0) for i in roots),
            'prompt': '', 'refinements': [], 'recipe': config.get('recipe', 'h3'),
            **({'seed_hunter': {'stage': config['seed_hunter'].get('stage')}} if config.get('seed_hunter') else {})}


def batch_detail(manager, owner, identity, page=1, page_size=10, active_only=False):
    if page < 1 or not 1 <= page_size <= 50:
        raise ValueError('Invalid batch page')
    saved = records(manager, owner)
    roots, _ = members(saved, identity, batch=True)
    roots.sort(key=lambda i: (saved[i][0].get('created_at', 0), i))
    from src.video_job_groups import refinement_groups, attach_refinements
    _, children = refinement_groups(manager, owner, saved)
    if active_only:
        roots = [i for i in roots if any(saved[child][0].get('status') in h3.ACTIVE
                                       for child in [i, *children.get(i, ())])]
    pages = max(1, (len(roots) + page_size - 1) // page_size)
    page = min(page, pages)
    jobs = []
    positions = {p.name: n + 1 for n, (p, s) in enumerate(h3._queue_entries(manager.root)) if s['status'] == 'queued'}
    for i in roots[(page - 1) * page_size:page * page_size]:
        try:
            jobs.append(attach_refinements(manager, owner, manager.view(i, owner, queue_positions=positions), children, queue_positions=positions))
        except FileNotFoundError:
            pass
    return {'batch': summary(saved, identity), 'jobs': jobs, 'root_ids': roots,
            'pagination': {'page': page, 'total': len(roots), 'total_pages': pages}, 'queue': queue_status(manager, owner)}


def process_now(manager, owner, identity):
    # Dispatch and priority updates use the same lock. Never interrupt a render.
    with h3._locked(manager.root.parent / 'video-render.lock'):
        directory = manager.directory(identity)
        with h3._locked(directory / 'job.lock'):
            state = h3._read(directory / 'state.json')
            if state.get('owner') != owner:
                raise FileNotFoundError('Video job not found')
            if state.get('status') != 'queued' or (directory / 'cancel').exists():
                raise RuntimeError('This job is no longer waiting in the queue')
            orders = [s.get('queue_order', int(s.get('created_at', 0) * 1e9)) for _, s in h3._queue_entries(manager.root)]
            state['queue_order'] = min(orders or [time.time_ns()]) - 1
            h3._write(directory / 'state.json', state)
    return {'prioritized': identity, 'queue': queue_status(manager, owner)}


def ensure_parent_available(manager, uploads):
    parent = parent_id(uploads)
    if parent and (manager.root / '.deleted-groups' / (parent + '.json')).exists():
        raise RuntimeError('This job was deleted; no more refinements can be added to it')
    batch = uploads.get('batch', {}).get('id')
    if batch and (manager.root / '.deleted-groups' / ('batch-' + batch + '.json')).exists():
        raise RuntimeError('This batch was deleted; no more files can be queued in it')


def wait_for_supervisors(manager, selected, timeout=10):
    """Let cancelled supervisors finish their bookkeeping before deleting it.

    No job or dispatch lock may be held while waiting: the supervisor needs
    those locks to save its terminal receipt. Worker shutdown is already
    confirmed by cancel(); delete() still checks the complete process tree.
    """
    deadline = time.monotonic() + timeout
    pending = set(selected)
    while pending:
        snapshot = h3._snapshot()
        pending = {i for i in pending if h3._live_record(
            h3._read(manager.directory(i) / 'state.json').get('supervisor'), snapshot)}
        if not pending:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('A video supervisor has not finished stopping. Retry Delete; its files are still saved.')
        time.sleep(.05)


def perform(manager, owner, family, identity, action, request_id=None, *, batch=False):
    if not h3.JOB_ID.fullmatch(identity):
        raise ValueError('Invalid video job ID')
    if action == 'process_now' and not batch:
        return process_now(manager, owner, identity)
    if not isinstance(action, str) or action not in {'stop', 'delete', 'duplicate', 'rerun_all'}:
        raise ValueError('Unknown job action')
    tombstones = manager.root / '.deleted-groups'
    tombstone = tombstones / (('batch-' if batch else '') + identity + '.json')
    if action in {'duplicate', 'rerun_all'}:
        if not request_id:
            raise ValueError('A request ID is required')
        from src.video_queue_bulk import rerun_jobs
        try:
            key = uuid.UUID(request_id).hex
        except (ValueError, TypeError, AttributeError):
            raise ValueError('A UUID request ID is required') from None
        receipts = manager.root / '.job-action-requests'
        receipts.mkdir(exist_ok=True, mode=0o700)
        path = receipts / (hashlib.sha256(f'{owner}:{key}'.encode()).hexdigest() + '.json')
        with h3._locked(path.with_suffix('.lock')):
            spec = {'identity': identity, 'action': action, 'batch': batch}
            old = h3._read(path)
            if old and old.get('spec') != spec:
                raise RuntimeError('This request ID was already used for another job action')
            targets = old.get('targets')
            if not targets:
                saved = records(manager, owner)
                roots, selected = members(saved, identity, batch)
                chosen = roots if action == 'duplicate' else selected
                targets = [{'id': i, 'revision': saved[i][1].get('revision', 0)} for i in chosen]
            h3._write(path, {'spec': spec, 'targets': targets})
            return rerun_jobs(manager, owner, family, targets, {}, request_id,
                              new_group=action == 'duplicate', in_place=action == 'rerun_all')
    saved = records(manager, owner)
    previous = h3._read(tombstone)
    if action == 'delete' and previous.get('owner') == owner:
        roots, selected = previous['roots'], previous['members']
    else:
        roots, selected = members(saved, identity, batch)
    if action == 'delete':
        # Persistent tombstones also prevent an upload/refinement prepared before
        # deletion from being launched after its parent has gone away.
        tombstones.mkdir(exist_ok=True, mode=0o700)
        with h3._locked(manager.root.parent / 'video-render.lock'):
            # Include submissions accepted while the initial read was in flight.
            # Launch takes this same lock and checks the tombstone below.
            fresh = records(manager, owner)
            try:
                current_roots, current_members = members(fresh, identity, batch)
                roots = list(dict.fromkeys([*roots, *current_roots]))
                selected = list(dict.fromkeys([*selected, *current_members]))
            except FileNotFoundError:
                pass
            receipt = {'owner': owner, 'roots': roots, 'members': selected}
            h3._write(tombstone, receipt)
            for i in selected:
                h3._write(tombstones / (i + '.json'), receipt)
    errors, stopped, deleted = [], [], []
    # Signal every member first. A queued sibling cannot start while an earlier
    # member is being stopped and its process tree is exiting.
    with h3._locked(manager.root.parent / 'video-render.lock'):
        for i in selected:
            directory = manager.directory(i)
            if directory.is_dir():
                with h3._locked(directory / 'job.lock'):
                    state = h3._read(directory / 'state.json')
                    if state.get('owner') == owner and state.get('status') in h3.ACTIVE:
                        (directory / 'cancel').touch(mode=0o600)
    for i in reversed(selected):
        try:
            if not manager.directory(i).exists():
                if action == 'delete': deleted.append(i)
                continue
            manager.cancel(i, owner)
            stopped.append(i)
        except (OSError, RuntimeError, ValueError) as exc:
            errors.append({'id': i, 'error': h3._redact(exc)})
    if action == 'delete' and not errors:
        try:
            wait_for_supervisors(manager, selected)
        except RuntimeError as exc:
            errors.append({'id': identity, 'error': str(exc)})
    if action == 'delete' and not errors:
        # Remove child artifacts before the parent snapshots. Only per-job
        # storage and registered project/Gallery outputs are deleted.
        ordered = [i for i in reversed(selected) if i not in roots] + roots
        for i in ordered:
            if i in deleted:
                continue
            try:
                manager.delete(i, owner)
                deleted.append(i)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append({'id': i, 'error': h3._redact(exc)})
                break
    return {'stopped': stopped, 'deleted': deleted, 'errors': errors,
            'succeeded': len(deleted if action == 'delete' else stopped), 'failed': len(errors),
            'queue': queue_status(manager, owner)}
