"""Shared multipart editor routes for queued H3 and BFS jobs."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import tempfile

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile

from src.video_job_edit import get_edit, snapshot, validate, commit_edit
from src.video_submission import complete_operation, input_metadata, UploadSpaceError
from src.upload_limits import get_chat_upload_max_bytes, format_byte_limit


def add_job_edit_routes(router, manager, family, extensions, store_uploads):
    from routes.h3_video_routes import _owner, _limited_request

    @router.get('/jobs/{job_id}/edit')
    async def load_editor(job_id: str, request: Request):
        try:
            return await asyncio.to_thread(get_edit, manager, job_id, _owner(request), family)
        except FileNotFoundError as exc:
            raise HTTPException(404, 'Video job not found') from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.patch('/jobs/{job_id}')
    async def save_editor(job_id: str, request: Request):
        owner = _owner(request)
        stage = None
        try:
            # Ownership/status are checked before reading potentially large
            # multipart bodies, and checked again under the publish lock.
            await asyncio.to_thread(snapshot, manager, job_id, owner, family)
            limit = get_chat_upload_max_bytes()
            limited = _limited_request(request, limit + 96 * 1024,
                                       f'Video inputs exceed {format_byte_limit(limit)} per job',
                                       space_directory=manager.root)
            async with limited.form(max_files=17 if family == 'h3' else 4, max_fields=3, max_part_size=65536) as form:
                if any(key not in {'config', 'revision', 'retain_inputs', *extensions} for key in form):
                    raise ValueError('Unknown video edit field')
                for field in ('config', 'revision', 'retain_inputs'):
                    if len(form.getlist(field)) > 1:
                        raise ValueError('Duplicate video edit field: ' + field)
                raw, version = form.get('config'), form.get('revision')
                if not isinstance(raw, str) or not isinstance(version, str) or not re.fullmatch(r'0|[1-9][0-9]{0,15}', version):
                    raise ValueError('Supply config JSON and the queued job revision')
                raw = json.loads(raw)
                expected = int(version)
                retain = None
                if 'retain_inputs' in form:
                    value = form['retain_inputs']
                    if not isinstance(value, str):
                        raise ValueError('Retained inputs must be JSON')
                    retain = json.loads(value)
                    if not isinstance(retain, dict):
                        raise ValueError('Retained inputs must map input fields to index arrays')
                uploads = {field: form.getlist(field) for field in extensions}
                if any(not isinstance(upload, UploadFile) for values in uploads.values() for upload in values):
                    raise ValueError('Video media must be uploaded files')
                inventory = await asyncio.to_thread(manager.inventory)
                before = await asyncio.to_thread(snapshot, manager, job_id, owner, family,
                                                  expected=expected, retain=retain, inventory=inventory)
                combined = {field: before['inputs'][field] + uploads[field] for field in extensions}
                validate(raw, inventory, combined, family)
                retained_bytes = sum(item['size'] for values in before['inputs'].values() for item in values)
                if retained_bytes > limit:
                    raise HTTPException(413, 'Retained video inputs exceed the per-job upload limit')
                staging_root = manager.root / '.edits'
                staging_root.mkdir(mode=0o700, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix='edit-', dir=staging_root))
                saved = await store_uploads(stage, uploads, limit - retained_bytes)
                names = input_metadata(uploads, family)['input_names']
                # Cancellation/disconnection cannot remove staging halfway
                # through a successful atomic commit.
                return await complete_operation(commit_edit, manager, job_id, owner, family, raw, expected,
                                                 retain, saved, names, stage, limit)
        except FileNotFoundError as exc:
            raise HTTPException(404, 'Video job not found') from exc
        except ValueError as exc:
            raise HTTPException(400, 'Invalid video edit JSON' if isinstance(exc, json.JSONDecodeError) else str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)
