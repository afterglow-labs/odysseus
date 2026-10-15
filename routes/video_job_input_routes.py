"""On-demand original video playback from authenticated saved job inputs."""
import asyncio
from functools import partial

import anyio
from fastapi import HTTPException, Request

from routes.video_server_file_routes import _byte_range, _VideoPreviewResponse
from src.video_job_inputs import open_input_video_proxy


def add_job_input_routes(router, manager, owner_callback):
    @router.api_route("/jobs/{job_id}/input-videos/{field}/{index}/preview", methods=["GET", "HEAD"])
    async def preview(job_id: str, field: str, index: int, request: Request, revision: int | None = None):
        owner = owner_callback(request)
        try:
            upload, descriptor = await anyio.to_thread.run_sync(partial(open_input_video_proxy,
                manager, job_id, owner, field, index, revision))
        except FileNotFoundError as error:
            raise HTTPException(404, "Saved input video not found") from error
        try:
            value = None if request.headers.get("if-range") else request.headers.get("range")
            start, end, status = _byte_range(value, upload.size)
            response = _VideoPreviewResponse(upload, start=start, end=end, status_code=status,
                                             head=request.method == "HEAD")
            response.headers["X-Preview-Start-Seconds"] = str(descriptor["start_seconds"])
            response.headers["X-Preview-Playback"] = descriptor["playback"]
            if descriptor["duration_seconds"] is not None:
                response.headers["X-Preview-Duration-Seconds"] = str(descriptor["duration_seconds"])
            return response
        except BaseException:
            await asyncio.shield(upload.close())
            raise
