"""Authenticated, bounded workflow transfer APIs for H3 and BFS."""
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import shutil
import uuid

import anyio
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.datastructures import UploadFile

from src.h3_video import _locked
from src.video_server_files import server_video_inputs
from src.video_submission import known_input_bytes
from src.upload_limits import get_chat_upload_max_bytes, format_byte_limit
from src.video_workflow import (CHUNK, JSON_LIMIT, WorkflowTransfers, export_options,
                                free_space, job_export, specification, stream_zip)


def _owner(request):
    from routes.h3_video_routes import _owner as video_owner
    return video_owner(request)


def _failure(exc):
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, 'Workflow transfer, saved job, or file not found')
    if isinstance(exc, OSError):
        return HTTPException(507, str(exc) if 'disk space' in str(exc) else 'Could not store or read workflow files; check available disk space and retry')
    return HTTPException(400, str(exc))


async def _complete_thread(function, *args):
    """Finish writes before releasing their lease or deleting staged files."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True), suppress(Exception):
            await asyncio.shield(task)
        raise


async def _acquire_download(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        with anyio.CancelScope(shield=True), suppress(Exception):
            _, handles = await asyncio.shield(task)
            for _, handle in handles:
                handle.close()
        raise


class WorkflowDownload(StreamingResponse):
    def __init__(self, document, handles, filename):
        self._zip_iterator = stream_zip(document, handles)
        self._handles = handles
        super().__init__(self._zip_iterator, media_type='application/zip', headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
        })

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Starlette does not close arbitrary sync generators on disconnect.
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(self._close)

    def _close(self):
        try:
            self._zip_iterator.close()
        finally:
            for _, handle in self._handles:
                handle.close()


def add_workflow_routes(router, manager, *, family):
    transfers = WorkflowTransfers(manager, family)

    @router.post('/workflow/export')
    async def prepare_export(request: Request):
        owner = _owner(request)
        from routes.h3_video_routes import _limited_request
        directory, success = None, False
        try:
            limit = get_chat_upload_max_bytes()
            limited = _limited_request(request, limit + 3 * JSON_LIMIT,
                                       f'Workflow input media exceeds {format_byte_limit(limit)}')
            async with limited.form(max_files=17, max_fields=3, max_part_size=JSON_LIMIT) as form:
                config_json, options_json = form.get('config'), form.get('options', '{}')
                if not isinstance(config_json, str) or not isinstance(options_json, str):
                    raise ValueError('Missing workflow config or export options')
                if len(config_json) > JSON_LIMIT or len(options_json) > JSON_LIMIT:
                    raise ValueError('Workflow settings exceed the metadata limit')
                config, options = json.loads(config_json), export_options(json.loads(options_json))
                if not isinstance(config, dict):
                    raise ValueError('Workflow config must be an object')
                _, _, extensions = specification(family, config)
                if set(form) - {'config', 'options', 'server_inputs', *extensions}:
                    raise ValueError('Unknown workflow upload field')
                if len(form.getlist('config')) != 1 or len(form.getlist('options')) > 1:
                    raise ValueError('Duplicate workflow config or export options')
                incoming = {field: form.getlist(field) for field in extensions}
                if any(not isinstance(item, UploadFile) for values in incoming.values() for item in values):
                    raise ValueError('Workflow media must be uploaded files')
                async with server_video_inputs(form, incoming, extensions) as incoming:
                    if known_input_bytes(incoming) > limit:
                        raise HTTPException(413, f'Workflow input media exceeds {format_byte_limit(limit)}')
                    inventory = await asyncio.to_thread(manager.inventory)
                    directory = await asyncio.to_thread(transfers.stage, owner)
                    with _locked(directory / 'lease.lock'):
                        uploads, total = {}, 0
                        for field, suffixes in extensions.items():
                            for item in incoming[field]:
                                if not options['include_attachments']:
                                    raise ValueError('Input media was sent without selecting Include input media')
                                original = Path(item.filename or '').name
                                suffix = Path(original).suffix.lower()
                                if suffix not in suffixes:
                                    raise ValueError(f'Unsupported media file for {field.replace("_", " ")}')
                                path = directory / (uuid.uuid4().hex + suffix)
                                with path.open('xb') as output:
                                    path.chmod(0o600)
                                    while chunk := await item.read(CHUNK):
                                        total += len(chunk)
                                        if total > limit:
                                            raise HTTPException(413, f'Workflow input media exceeds {format_byte_limit(limit)}')
                                        await asyncio.to_thread(free_space, directory, len(chunk))
                                        await asyncio.to_thread(output.write, chunk)
                                uploads.setdefault(field, []).append({'path': str(path), 'name': original})
                        receipt = await _complete_thread(transfers.finish_export, directory, owner, config, inventory, uploads, options)
                        success = True
                        return receipt
        except (ValueError, OSError) as exc:
            raise _failure(exc) from exc
        finally:
            if directory is not None and not success:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    @router.get('/workflow/exports/{ticket}')
    async def download_export(ticket: str, request: Request):
        owner = _owner(request)
        try:
            document, handles = await _acquire_download(transfers.export, ticket, owner)
        except (ValueError, OSError) as exc:
            raise _failure(exc) from exc
        return WorkflowDownload(document, handles, f'{family}-{ticket}.odysseus-workflow.zip')

    @router.get('/jobs/{job_id}/workflow')
    async def download_job(job_id: str, request: Request, include_weights: bool = False, include_attachments: bool = False):
        owner = _owner(request)
        try:
            document, handles = await _acquire_download(job_export, manager, job_id, owner, family,
                                                      {'include_weights': include_weights, 'include_attachments': include_attachments})
        except (ValueError, OSError) as exc:
            raise _failure(exc) from exc
        return WorkflowDownload(document, handles, f'{family}-{job_id}.odysseus-workflow.zip')

    @router.post('/workflow/import')
    async def import_workflow(request: Request):
        owner = _owner(request)
        directory, success = None, False
        try:
            directory = await asyncio.to_thread(transfers.stage, owner)
            with _locked(directory / 'lease.lock'):
                length = request.headers.get('content-length')
                if length is not None:
                    try:
                        length = int(length)
                    except ValueError as exc:
                        raise ValueError('Invalid workflow upload length') from exc
                    if length <= 0:
                        raise ValueError('Choose a nonempty workflow archive or JSON file')
                    await asyncio.to_thread(free_space, directory, length)
                source = directory / 'upload.zip'
                received = 0
                with source.open('xb') as output:
                    source.chmod(0o600)
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        received += len(chunk)
                        await asyncio.to_thread(free_space, directory, len(chunk))
                        await asyncio.to_thread(output.write, chunk)
                if not received or length is not None and received != length:
                    raise ValueError('Workflow upload was empty or incomplete')
                result = await _complete_thread(transfers.import_file, directory, owner, source)
                success = True
                return result
        except (ValueError, OSError) as exc:
            raise _failure(exc) from exc
        finally:
            if directory is not None and not success:
                await asyncio.to_thread(shutil.rmtree, directory, True)

    @router.get('/workflow/imports/{ticket}/attachments/{index}')
    async def imported_attachment(ticket: str, index: int, request: Request):
        owner = _owner(request)
        try:
            path, name = await asyncio.to_thread(transfers.attachment, ticket, index, owner)
        except (ValueError, OSError) as exc:
            raise _failure(exc) from exc
        return FileResponse(path, filename=name, content_disposition_type='attachment',
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
