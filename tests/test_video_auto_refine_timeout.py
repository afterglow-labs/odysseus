"""Exercise the real timeout middleware without starting Odysseus services."""
import ast
import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def timeout_middleware(seconds):
    path = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    definitions = [node for node in tree.body if
                   isinstance(node, ast.ClassDef) and node.name == "_RequestTimeoutMiddleware"
                   or isinstance(node, ast.Assign) and any(
                       isinstance(target, ast.Name) and target.id == "_TIMEOUT_EXEMPT_PREFIXES"
                       for target in node.targets)]
    namespace = {"_asyncio": asyncio, "_BaseHTTPMiddleware": BaseHTTPMiddleware,
                 "_JSONResponse": JSONResponse, "REQUEST_HARD_TIMEOUT": seconds}
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_RequestTimeoutMiddleware"]


def slow_app():
    app = FastAPI()
    calls = []

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def apply_once(path):
        calls.append(path)
        await asyncio.sleep(0.04)
        return {"created": 3}

    app.add_middleware(timeout_middleware(0.005))
    return app, calls


@pytest.mark.asyncio
async def test_bulk_refinement_completes_past_generic_deadline_without_resubmission():
    app, calls = slow_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/video/h3/auto-refine")
    assert response.status_code == 200
    assert response.json() == {"created": 3}
    assert calls == ["api/video/h3/auto-refine"]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/video/h3/auto-refine"),
    ("POST", "/api/video/h3/auto-refine/other"),
    ("POST", "/api/video/h3/auto-refine-other"),
    ("GET", "/api/video/h3/jobs"),
    ("POST", "/api/video/h3/jobs"),
])
async def test_other_video_routes_keep_generic_timeout(method, path):
    app, calls = slow_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.request(method, path)
    assert response.status_code == 504
    assert "timeout" in response.json()["detail"]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_existing_streaming_exemption_still_completes():
    app, calls = slow_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/chat/stream")
    assert response.status_code == 200
    assert calls == ["api/chat/stream"]
