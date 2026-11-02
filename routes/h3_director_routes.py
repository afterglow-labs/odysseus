"""Authenticated Director candidate playback and idempotent explicit refinement."""
import asyncio
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from src import h3_director as director
from src.video_submission import submission, launch_job, complete_operation, UUID, UploadSpaceError


def add_director_routes(router, manager, owner_callback):
    @router.get("/jobs/{job_id}/director/candidates/{index}/video")
    async def video(job_id: str, index: int, request: Request):
        try:
            await asyncio.to_thread(manager._state, job_id, owner_callback(request))
            path = director.artifact(manager.directory(job_id), index, "mp4")
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Director candidate not found") from exc
        return FileResponse(path, media_type="video/mp4", content_disposition_type="inline",
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @router.post("/jobs/{job_id}/director/refine", status_code=201)
    async def refine(job_id: str, request: Request):
        from routes.video_queue_routes import _body
        body = await _body(request, {"request_id", "candidate", "config"}, {"request_id", "candidate"})
        owner = owner_callback(request)
        try:
            key = body.get("request_id")
            if not isinstance(key, str) or not UUID.fullmatch(key):
                raise ValueError("A request ID is required for safe refinement retries")
            manager.directory(job_id)
            async with submission(manager, owner, "h3-director-refine-" + job_id, uuid.UUID(key).hex) as pending:
                existing = await asyncio.to_thread(pending.existing)
                if existing is not None:
                    return existing
                directory = pending.stage()
                config, inputs = await complete_operation(director.prepare_refinement, manager, job_id, owner, body, directory, pending.key)
                return await launch_job(manager, directory, owner, config, inputs)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Director job not found") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        except UploadSpaceError as exc:
            raise HTTPException(507, str(exc)) from exc
