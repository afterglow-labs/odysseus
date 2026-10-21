"""SeedScout previews never select a seed; continuing is an explicit action."""
import asyncio
import json
import uuid
from src.video_submission import submission, launch_job, complete_operation, UUID, UploadSpaceError
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from src import h3_seed_scout as scout


def add_seed_scout_routes(router, manager, owner_callback):
    @router.get("/jobs/{job_id}/scout")
    async def detail(job_id: str, request: Request):
        try:
            return await asyncio.to_thread(scout.detail, manager, job_id, owner_callback(request))
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/jobs/{job_id}/scout/previews/{seed}")
    async def preview(job_id: str, seed: int, request: Request):
        try:
            path = await asyncio.to_thread(scout.artifact, manager, job_id, owner_callback(request), seed)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type="image/webp", content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/jobs/{job_id}/scout/select")
    async def select(job_id: str, request: Request):
        owner = owner_callback(request)
        try:
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 1024:
                    raise HTTPException(413, "Selection is too large")
            value = json.loads(body)
            if not isinstance(value, dict) or set(value) != {"seed"}:
                raise ValueError("Supply the selected seed")
            return await asyncio.to_thread(scout.select, manager, job_id, owner, value["seed"])
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/jobs/{job_id}/scout/render", status_code=201)
    async def render(job_id: str, request: Request):
        owner = owner_callback(request)
        try:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 4096:
                    raise HTTPException(413, "Seed request is too large")
            body = json.loads(data)
            if not isinstance(body, dict) or set(body) != {"seed", "request_id"}:
                raise ValueError("Supply a seed and request_id")
            key = body['request_id']
            if not isinstance(key, str) or not UUID.fullmatch(key):
                raise ValueError("A request_id UUID is required")
            manager.directory(job_id)
            async with submission(manager, owner, "h3-scout-render-" + job_id, uuid.UUID(key).hex) as pending:
                existing = await asyncio.to_thread(pending.existing)
                if existing is not None:
                    return existing
                directory = pending.stage()
                config, inputs = await complete_operation(scout.prepare_render, manager, job_id, owner, body, directory, pending.key)
                return await launch_job(manager, directory, owner, config, inputs)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Seed Scout job not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
