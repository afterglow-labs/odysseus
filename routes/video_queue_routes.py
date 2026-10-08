"""Shared authenticated queue controls for H3 and BFS video workflows."""
import asyncio
import json

from fastapi import HTTPException, Request

from src import video_queue_bulk as bulk
from src.video_submission import UploadSpaceError

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
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(507, "The queue operation could not be saved; check available disk space and retry") from exc


def add_queue_routes(router, manager, family):
    from routes.h3_video_routes import _owner
    from src.video_queue_control import queue_status, set_queue_paused, cancel_queued

    @router.get('/queue')
    async def status(request: Request):
        return await _call(queue_status, manager, _owner(request))

    @router.get('/queue/jobs')
    async def snapshot(request: Request, status: str = 'queued'):
        owner = _owner(request)
        jobs = await _call(bulk.list_jobs, manager, owner, family, status)
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
        body = await _body(request, {'jobs', 'patch', 'request_id'}, {'jobs', 'patch', 'request_id'})
        result = await _call(bulk.rerun_jobs, manager, owner, family, body['jobs'], body['patch'], body['request_id'])
        result['queue'] = await _call(queue_status, manager, owner)
        return result
