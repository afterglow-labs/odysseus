"""Shared prompt library; authorization matches the video workflow APIs."""
import asyncio
import json
import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request

from routes.h3_video_routes import _owner
from src.workflow_prompt_history import PromptHistoryStore, PromptTooLarge

log = logging.getLogger(__name__)


def setup_workflow_prompt_history_routes(store=None):
    store = store if store is not None else PromptHistoryStore()
    router = APIRouter()

    @router.get("/api/workflows/prompt-history")
    async def history(request: Request):
        owner = _owner(request)
        try:
            limit = int(request.query_params.get("limit", "100"))
            offset = int(request.query_params.get("offset", "0"))
            return await asyncio.to_thread(store.list, owner, q=request.query_params.get("q", ""), limit=limit, offset=offset)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            log.warning("Prompt history read failed", exc_info=True)
            raise HTTPException(503, "Prompt history is temporarily unavailable; your draft is unchanged") from exc

    @router.post("/api/workflows/prompt-history")
    async def save(request: Request):
        owner = _owner(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 512 * 1024:
                raise HTTPException(413, "Prompt history request is too large")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict) or set(body) - {"prompt", "workflow"}:
                raise ValueError("Supply a prompt and optional workflow label")
            return await asyncio.to_thread(store.record, owner, body.get("prompt"), body.get("workflow"))
        except PromptTooLarge as exc:
            raise HTTPException(413, str(exc)) from exc
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OSError, sqlite3.Error) as exc:
            log.warning("Prompt history save failed", exc_info=True)
            raise HTTPException(503, "Could not save prompt history; please try again") from exc

    return router
