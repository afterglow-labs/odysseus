"""Authenticated installation and profile management for local H3 adapters."""
import asyncio
import json
from pathlib import Path
import shutil

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from scripts.h3_lora_controls import PROFILE_LIMIT
from src.h3_lora_library import LoraLibrary, MAX_BYTES, require_space
from src.video_submission import store_upload_chunk, complete_operation


async def _json(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > PROFILE_LIMIT:
            raise HTTPException(413, "LoRA metadata exceeds 64 KiB")
    try:
        return json.loads(data)
    except ValueError as error:
        raise HTTPException(400, "Invalid LoRA JSON") from error


def add_lora_library_routes(router, manager, owner_callback):
    library = LoraLibrary(manager)

    def response(value, status_code=200):
        return JSONResponse(value, status_code=status_code, headers={"Cache-Control": "private, no-store"})

    async def call(function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except FileNotFoundError as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except RuntimeError as error:
            raise HTTPException(409, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "Unable to save the adapter. Check free space and directory permissions.") from error

    @router.get('/lora-library')
    async def listing(request: Request):
        owner = owner_callback(request)
        components = await call(library.components)
        return response({"components": components, "downloads": await call(library.downloads, owner)})

    @router.post('/lora-library/upload')
    async def upload(request: Request):
        owner_callback(request)
        path = None
        try:
            if request.headers.get('content-type', '').split(';', 1)[0].lower() != 'application/octet-stream':
                raise HTTPException(415, "Upload safetensors as application/octet-stream with a filename query parameter")
            length = request.headers.get('content-length')
            if length is not None:
                try:
                    length = int(length)
                except ValueError:
                    raise HTTPException(400, "Invalid Content-Length") from None
                if not 0 < length <= MAX_BYTES:
                    raise HTTPException(413, "Choose a nonempty adapter of at most 8 GiB")
            path = await call(library.stage, request.query_params.get('filename'))
            await call(require_space, path.parent, length or 0)
            total = 0
            with path.open('xb') as stream:
                path.chmod(0o600)
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise HTTPException(413, "Adapters must be at most 8 GiB")
                    await store_upload_chunk(stream, path.parent, chunk)
            if not total or length is not None and total != length:
                raise HTTPException(400, "The adapter upload was empty or incomplete")
            # Finish publication before disconnect cleanup touches its staging folder.
            component = await complete_operation(library.install, path)
            return response({"component": component})
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except OSError as error:
            raise HTTPException(507, "Unable to save the adapter. Check free space and directory permissions.") from error
        finally:
            if path is not None:
                shutil.rmtree(path.parent, ignore_errors=True)

    @router.post('/lora-library/download')
    async def download(request: Request):
        owner = owner_callback(request)
        return response(await call(library.start, owner, await _json(request)), 202)

    @router.get('/lora-library/downloads/{download_id}')
    async def download_status(download_id: str, request: Request):
        owner = owner_callback(request)
        return response(await call(library.status, download_id, owner))

    @router.delete('/lora-library/downloads/{download_id}')
    async def cancel_download(download_id: str, request: Request):
        owner = owner_callback(request)
        return response(await call(library.cancel, download_id, owner))

    @router.put('/lora-library/{component_id}')
    async def profile(component_id: str, request: Request):
        owner_callback(request)
        return response({"component": await call(library.profile, component_id, await _json(request))})

    return library
