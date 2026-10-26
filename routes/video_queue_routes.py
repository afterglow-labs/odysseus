"""Shared authenticated queue controls for H3 and BFS video workflows."""
import asyncio
import json

from fastapi import HTTPException, Request

from src import video_queue_bulk as bulk
from src.video_submission import UploadSpaceError
from src.video_history_delete import HistoryDeletionBusy

BODY_LIMIT = 512 * 1024


async def _body(request, allowed, required=()):
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > BODY_LIMIT:
            raise HTTPException(413, "Video queue request is too large")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(400, "Invalid video queue JSON") from exc
    if not isinstance(value, dict) or value.keys() - set(allowed) or set(required) - value.keys():
        raise HTTPException(400, "Invalid video queue request fields")
    return value


async def _call(function, *args, **kwargs):
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Video job not found") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except UploadSpaceError as exc:
        raise HTTPException(507, str(exc)) from exc
    except HistoryDeletionBusy as exc:
        raise HTTPException(423, str(exc), headers={'Retry-After': '2'}) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(507, "The queue operation could not be saved; check available disk space and retry") from exc


def add_queue_routes(router, manager, family):
    from routes.h3_video_routes import _owner
    from src.video_queue_control import queue_status, set_queue_paused, cancel_queued
    from src import video_history_delete as history_delete
    from src import video_job_actions as actions

    @router.get('/batches/{batch_id}')
    async def batch_detail(batch_id: str, request: Request, page: int = 1, page_size: int = 10, active_only: bool = False):
        return await _call(actions.batch_detail, manager, _owner(request), batch_id, page, page_size, active_only)

    @router.post('/batches/{batch_id}/actions')
    async def batch_action(batch_id: str, request: Request):
        body = await _body(request, {'action', 'request_id'}, {'action'})
        return await _call(actions.perform, manager, _owner(request), family, batch_id,
                           body['action'], body.get('request_id'), batch=True)

    @router.post('/jobs/{job_id}/actions')
    async def job_action(job_id: str, request: Request):
        body = await _body(request, {'action', 'request_id'}, {'action'})
        return await _call(actions.perform, manager, _owner(request), family, job_id,
                           body['action'], body.get('request_id'))

    @router.post('/history/delete-preview')
    async def history_preview(request: Request):
        owner = _owner(request)
        body = await _body(request, {'scope', 'recipe', 'stage'})
        return await _call(history_delete.preview, manager, owner, **body)

    @router.post('/history/delete')
    async def delete_history(request: Request):
        owner = _owner(request)
        body = await _body(request, {'snapshot_id', 'confirm', 'retry_errors'}, {'snapshot_id', 'confirm'})
        return await _call(history_delete.delete, manager, owner, **body)

    @router.get('/queue')
    async def status(request: Request):
        return await _call(queue_status, manager, _owner(request))

    @router.get('/queue/jobs')
    async def snapshot(request: Request, status: str = 'queued', job_id: str | None = None):
        owner = _owner(request)
        jobs = await _call(bulk.list_jobs, manager, owner, family, status, job_id=job_id)
        return {"jobs": jobs, "queue": await _call(queue_status, manager, owner)}

    @router.post('/queue/pause')
    async def pause(request: Request):
        owner = _owner(request)
        body = await _body(request, {'paused'}, {'paused'})
        if type(body['paused']) is not bool:
            raise HTTPException(400, "paused must be true or false")
        return await _call(set_queue_paused, manager, owner, body['paused'])

    async def remove(request, delete):
        owner = _owner(request)
        body = await _body(request, {'scope'})
        scope = body.get('scope', 'family')
        if not isinstance(scope, str) or scope not in {'family', 'all'}:
            raise HTTPException(400, "Choose family or all queue scope")
        return await _call(cancel_queued, manager, owner, delete=delete, scope=scope)

    @router.post('/queue/cancel')
    async def cancel(request: Request):
        return await remove(request, False)

    @router.post('/queue/delete')
    async def delete(request: Request):
        return await remove(request, True)

    @router.post('/queue/edit')
    async def edit(request: Request):
        owner = _owner(request)
        body = await _body(request, {'jobs', 'patch'}, {'jobs', 'patch'})
        result = await _call(bulk.edit_jobs, manager, owner, family, body['jobs'], body['patch'])
        result['queue'] = await _call(queue_status, manager, owner)
        return result

    @router.post('/queue/rerun')
    async def rerun(request: Request):
        owner = _owner(request)
        body = await _body(request, {'jobs', 'patch', 'base_config', 'request_id', 'retry_failed'}, {'jobs', 'request_id'})
        if ('patch' in body) == ('base_config' in body) or not isinstance(body.get('base_config', body.get('patch')), dict):
            raise HTTPException(400, 'Supply base settings or individual parameter changes, not both')
        result = await _call(bulk.rerun_jobs, manager, owner, family, body['jobs'], body.get('patch', {}), body['request_id'],
                             base_config=body.get('base_config'), retry_failed=body.get('retry_failed', False))
        result['queue'] = await _call(queue_status, manager, owner)
        return result
