"""Admin-only, allowlisted gateway into the separate Afterglow server."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src.afterglow_client import AfterglowError, get_afterglow_client


router = APIRouter(
    prefix="/api/cybersecurity", tags=["cybersecurity"],
    dependencies=[Depends(require_admin)],
)


def _client():
    try:
        return get_afterglow_client()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _forward(path: str, *, method="GET", body=None, params=None, auth=True):
    try:
        return await _client().request(path, method=method, body=body, params=params, auth=auth)
    except AfterglowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _require_browser_write(request: Request):
    # A custom header requires browser cross-origin writes to pass CORS
    # preflight; Odysseus's allowed origins exclude arbitrary sites.
    if request.headers.get("x-requested-with") != "XMLHttpRequest":
        raise HTTPException(status_code=403, detail="X-Requested-With required")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
        raise HTTPException(status_code=403, detail="Cross-origin write denied")


class TaskControl(BaseModel):
    action: Literal["pause", "resume"]


class InterceptDecision(BaseModel):
    decision: Literal["allowed", "denied"]


@router.get("/status")
async def status():
    health = await _forward("/api/health", auth=False)
    stats = await _forward("/api/stats")
    return {"connected": True, "health": health, "stats": stats}


@router.get("/tasks")
async def tasks():
    return await _forward("/api/tasks")


@router.get("/tasks/{task_id}")
async def task(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}")


@router.post("/tasks/{task_id}/control")
async def control_task(request: Request, body: TaskControl, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/control", method="POST", body=body.model_dump())


@router.get("/tasks/{task_id}/coverage")
async def coverage(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/coverage")


@router.get("/tasks/{task_id}/scope")
async def scope(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/scope")


@router.get("/tasks/{task_id}/activity")
async def activity(task_id: int = Path(gt=0), before: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=300)):
    # The incremental /activity route starts with the *oldest* event. History
    # returns the latest page of the current main session instead.
    return await _forward("/api/exploration/activity/history", params={
        "task": task_id, "session": "main", "before": before, "limit": limit,
    })


@router.get("/findings")
async def findings(
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    severity: str = Query("", max_length=30), status: str = Query("", max_length=40),
    q: str = Query("", max_length=200),
):
    return await _forward("/api/exploration/findings", params={
        "page": page, "limit": limit, "severity": severity, "status": status, "q": q,
    })


@router.get("/findings/{finding_id}")
async def finding(finding_id: int = Path(gt=0)):
    return await _forward(f"/api/exploration/findings/{finding_id}")


@router.get("/intercept/pending")
async def pending_intercepts():
    return await _forward("/api/intercept/pending")


@router.post("/intercept/pending/{pending_id}/decide")
async def decide_intercept(request: Request, body: InterceptDecision, pending_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(
        f"/api/intercept/pending/{pending_id}/decide", method="POST", body=body.model_dump(),
    )


@router.get("/traffic")
async def traffic(page: int = Query(0, ge=0), size: int = Query(50, ge=1, le=100), q: str = Query("", max_length=200)):
    return await _forward("/api/traffic", params={"page": page, "size": size, "q": q})
