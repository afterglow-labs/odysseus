"""Authenticated conversion previews; imported graphs are never executed."""
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request

from routes.h3_video_routes import _owner
from src.comfy_workflow_conversion import convert_comfyui
from src.h3_video import H3JobManager
from src.video_workflow import JSON_LIMIT


def setup_workflow_conversion_routes(manager=None):
    manager = manager or H3JobManager()
    router = APIRouter(prefix="/api/video/workflows", tags=["video"])

    @router.post("/convert-comfyui")
    async def convert(request: Request):
        _owner(request)
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > JSON_LIMIT:
                raise HTTPException(413, "Workflow JSON exceeds 1 MiB")
        try:
            def reject_constant(_):
                raise ValueError("Workflow numbers must be finite")
            value = json.loads(body, parse_constant=reject_constant)
            if not isinstance(value, dict) or set(value) - {"workflow", "output_node"} or "workflow" not in value:
                raise ValueError("Supply workflow JSON and an optional output_node")
            if value.get("output_node") is not None and not isinstance(value["output_node"], str):
                raise ValueError("output_node must be a node ID string")
            # Semantic inspection precedes disk/model inventory work. Unsupported
            # nodes receive an actionable report without loading any runtime.
            result = await asyncio.to_thread(convert_comfyui, value["workflow"], output_node=value.get("output_node"))
            if result["convertible"]:
                inventory = await asyncio.to_thread(manager.inventory)
                result = await asyncio.to_thread(convert_comfyui, value["workflow"], inventory, output_node=value.get("output_node"))
            return result
        except (ValueError, TypeError, RecursionError) as exc:
            raise HTTPException(400, str(exc) if isinstance(exc, ValueError) else "Malformed workflow JSON") from exc

    return router
