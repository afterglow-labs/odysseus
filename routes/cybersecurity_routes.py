"""Admin-only, allowlisted gateway into the separate Afterglow server."""

import os
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from core.middleware import require_admin
from src.afterglow_client import AfterglowError, get_afterglow_client, validate_origin


router = APIRouter(
    prefix="/api/cybersecurity", tags=["cybersecurity"],
    dependencies=[Depends(require_admin)],
)


def _client():
    try:
        return get_afterglow_client()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _forward(path: str, *, method="GET", body=None, params=None, auth=True, as_text=False, timeout=20):
    try:
        kwargs = {"method": method, "body": body, "params": params, "auth": auth}
        if as_text:
            kwargs["as_text"] = True
        if timeout != 20:
            kwargs["timeout"] = timeout
        return await _client().request(path, **kwargs)
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


class GoalInput(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    vulnclass: str = Field("", max_length=160)

    @model_validator(mode="after")
    def require_text(self):
        self.text = self.text.strip()
        self.vulnclass = self.vulnclass.strip()
        if not self.text:
            raise ValueError("Goal text is required")
        return self


class ConstraintInput(BaseModel):
    kind: Literal["allow", "deny"]
    text: str = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def require_text(self):
        self.text = self.text.strip()
        if not self.text:
            raise ValueError("Constraint text is required")
        return self


class InterceptDecision(BaseModel):
    decision: Literal["allowed", "denied"]


class TaskCreate(BaseModel):
    name: str = Field("", max_length=160)
    description: str = Field(min_length=1, max_length=5000)
    goal: str = Field(min_length=1, max_length=5000)
    coverage_enabled: bool = True
    seed_first_intent: bool = False
    llm_profile_id: int | None = Field(None, gt=0)

    @model_validator(mode="after")
    def require_content(self):
        self.name = self.name.strip()
        self.description = self.description.strip()
        self.goal = self.goal.strip()
        if not self.description or not self.goal:
            raise ValueError("Description and goal are required")
        return self


class ChatInput(BaseModel):
    message: str = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def require_content(self):
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("Message is required")
        return self


FindingStatus = Literal[
    "pending", "in_progress", "confirmed", "resolved", "fixed",
    "false_positive", "ignored", "duplicate", "risk_accepted",
]


class FindingTriage(BaseModel):
    status: FindingStatus | None = None
    severity: Literal["critical", "high", "medium", "low"] | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.status is None and self.severity is None:
            raise ValueError("Status or severity is required")
        return self


class ScopeAdd(BaseModel):
    kind: Literal["company", "root_domain", "subdomain", "ip", "cidr", "icp", "keyword"]
    value: str = Field(min_length=1, max_length=500)
    reason: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def require_content(self):
        self.value = self.value.strip()
        self.reason = self.reason.strip()
        if not self.value:
            raise ValueError("Scope value is required")
        return self


class AssetAdd(BaseModel):
    kind: Literal["root_domain", "subdomain", "ip", "service"]
    value: str = Field(min_length=1, max_length=2000)
    task_id: int = Field(gt=0)

    @model_validator(mode="after")
    def require_content(self):
        self.value = self.value.strip()
        if not self.value:
            raise ValueError("Asset value is required")
        if self.kind == "service":
            url = urlsplit(self.value)
            if url.scheme not in ("http", "https") or not url.hostname:
                raise ValueError("HTTP service needs a full http:// or https:// URL")
        return self


class TaskRule(BaseModel):
    enabled: bool = True
    action: Literal["block", "allow"]
    kind: Literal["exact_domain", "exact_url", "fuzzy_domain", "exact_ip", "fuzzy_ip", "fuzzy_url", "cidr"]
    pattern: str = Field(min_length=1, max_length=500)
    note: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def require_content(self):
        self.pattern = self.pattern.strip()
        self.note = self.note.strip()
        if not self.pattern:
            raise ValueError("Rule pattern is required")
        return self


class RuleToggle(BaseModel):
    enabled: bool


class ProfileActivate(BaseModel):
    id: int = Field(gt=0)


LocalModelEndpoint = Literal[
    "http://127.0.0.1:11434", "http://host.docker.internal:11434", "http://ollama:11434",
]
LOCAL_MODEL_ENDPOINTS = frozenset(LocalModelEndpoint.__args__)


class LocalModelTarget(BaseModel):
    base_url: LocalModelEndpoint


class LocalProfileCreate(LocalModelTarget):
    name: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_content(self):
        self.name = self.name.strip()
        self.model = self.model.strip()
        if not self.name or not self.model:
            raise ValueError("Profile name and model are required")
        return self


class ChatGPTConnect(BaseModel):
    account_id: str = Field("", max_length=200)

    @model_validator(mode="after")
    def trim_account(self):
        self.account_id = self.account_id.strip()
        return self


class ChatGPTAccount(BaseModel):
    account_id: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_account(self):
        self.account_id = self.account_id.strip()
        if not self.account_id:
            raise ValueError("Account is required")
        return self


class ChatGPTProfile(ChatGPTAccount):
    name: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def require_profile(self):
        self.name = self.name.strip()
        self.model = self.model.strip()
        if not self.name or not self.model:
            raise ValueError("Profile name and model are required")
        return self


@router.get("/status")
async def status():
    public_url = os.getenv("AFTERGLOW_PUBLIC_URL", "").strip()
    console_error = ""
    if public_url:
        try:
            public_url = validate_origin(public_url)
        except ValueError:
            public_url = ""
            console_error = "AFTERGLOW_PUBLIC_URL must be HTTPS or loopback HTTP"
    health = await _forward("/api/health", auth=False)
    stats = await _forward("/api/stats")
    return {"connected": True, "health": health, "stats": stats,
            "console_url": public_url, "console_error": console_error}


@router.get("/tasks")
async def tasks():
    return await _forward("/api/tasks")


@router.post("/tasks")
async def create_task(request: Request, body: TaskCreate):
    _require_browser_write(request)
    return await _forward("/api/tasks", method="POST", body=body.model_dump(exclude_none=True))


@router.get("/tasks/{task_id}")
async def task(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}")


@router.post("/tasks/{task_id}/control")
async def control_task(request: Request, body: TaskControl, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/control", method="POST", body=body.model_dump())


@router.get("/tasks/{task_id}/goals")
async def task_goals(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/goals")


@router.post("/tasks/{task_id}/goals")
async def add_task_goal(request: Request, body: GoalInput, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/goals", method="POST", body=body.model_dump())


@router.patch("/tasks/{task_id}/goals/{goal_id}")
async def edit_task_goal(request: Request, body: GoalInput, task_id: int = Path(gt=0), goal_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/goals/{goal_id}", method="PATCH", body=body.model_dump())


@router.delete("/tasks/{task_id}/goals/{goal_id}")
async def delete_task_goal(request: Request, task_id: int = Path(gt=0), goal_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/goals/{goal_id}", method="DELETE")


@router.get("/tasks/{task_id}/constraints")
async def task_constraints(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/constraints")


@router.post("/tasks/{task_id}/constraints")
async def add_task_constraint(request: Request, body: ConstraintInput, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/constraints", method="POST", body=body.model_dump())


@router.patch("/tasks/{task_id}/constraints/{constraint_id}")
async def edit_task_constraint(request: Request, body: ConstraintInput, task_id: int = Path(gt=0), constraint_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/constraints/{constraint_id}", method="PATCH", body=body.model_dump())


@router.delete("/tasks/{task_id}/constraints/{constraint_id}")
async def delete_task_constraint(request: Request, task_id: int = Path(gt=0), constraint_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/constraints/{constraint_id}", method="DELETE")


@router.get("/tasks/{task_id}/outcome")
async def task_outcome(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/outcome")


@router.get("/tasks/{task_id}/lessons")
async def task_lessons(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/lessons")


@router.post("/tasks/{task_id}/lessons/extract")
async def extract_task_lessons(request: Request, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/lessons/extract", method="POST", body={})


@router.get("/tasks/{task_id}/waves")
async def task_waves(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/waves")


@router.get("/tasks/{task_id}/artifacts")
async def task_artifacts(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/artifacts")


@router.get("/tasks/{task_id}/c2-handoffs")
async def task_c2_handoffs(task_id: int = Path(gt=0)):
    result = await _forward(f"/api/tasks/{task_id}/c2-handoffs/summary")
    rows = result.get("handoffs") if isinstance(result, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=502, detail="Invalid handoff response from Afterglow")
    fields = ("id", "vector", "target_host", "target_path", "binary_arch", "binary_sha256", "status", "created_at")
    return {"handoffs": [{key: row[key] for key in fields if key in row} for row in rows]}


@router.get("/tasks/{task_id}/business-map")
async def task_business_map(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/business-map")


@router.get("/tasks/{task_id}/campaign")
async def task_campaign(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/campaign")


@router.get("/tasks/{task_id}/hunt-classes")
async def task_hunt_classes(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/hunt-classes")


@router.get("/tasks/{task_id}/attack-chains")
async def task_attack_chains(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/attack-chains")


@router.get("/tasks/{task_id}/chat/status")
async def chat_status(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/chat/status")


@router.post("/tasks/{task_id}/chat")
async def send_chat(request: Request, body: ChatInput, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward("/api/chat", method="POST", body=body.model_dump(), params={"task": task_id})


@router.post("/tasks/{task_id}/chat/stop")
async def stop_chat(request: Request, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/chat/stop", method="POST", body={})


@router.post("/tasks/{task_id}/chat/new-session")
async def new_chat_session(request: Request, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward("/api/exploration/main-session/new", method="POST", body={}, params={"task": task_id})


@router.get("/tasks/{task_id}/coverage")
async def coverage(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/coverage")


@router.get("/tasks/{task_id}/scope")
async def scope(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/scope")


@router.post("/tasks/{task_id}/scope")
async def add_scope(request: Request, body: ScopeAdd, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/scope", method="POST", body=body.model_dump())


@router.delete("/tasks/{task_id}/scope/{scope_id}")
async def delete_scope(request: Request, task_id: int = Path(gt=0), scope_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/scope/{scope_id}", method="DELETE")


@router.get("/tasks/{task_id}/rules")
async def task_rules(task_id: int = Path(gt=0)):
    return await _forward(f"/api/tasks/{task_id}/intercept-rules")


@router.post("/tasks/{task_id}/rules")
async def add_task_rule(request: Request, body: TaskRule, task_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/intercept-rules", method="POST", body=body.model_dump())


@router.post("/tasks/{task_id}/rules/{rule_id}/toggle")
async def toggle_task_rule(request: Request, body: RuleToggle, task_id: int = Path(gt=0), rule_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(
        f"/api/tasks/{task_id}/intercept-rules/{rule_id}/toggle", method="POST", body=body.model_dump(),
    )


@router.delete("/tasks/{task_id}/rules/{rule_id}")
async def delete_task_rule(request: Request, task_id: int = Path(gt=0), rule_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(f"/api/tasks/{task_id}/intercept-rules/{rule_id}", method="DELETE")


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


@router.patch("/findings/{finding_id}")
async def triage_finding(request: Request, body: FindingTriage, finding_id: int = Path(gt=0)):
    _require_browser_write(request)
    return await _forward(
        f"/api/exploration/findings/{finding_id}", method="PATCH", body=body.model_dump(exclude_none=True),
    )


@router.get("/assets")
async def assets(
    task_id: int = Query(0, ge=0), asset_type: Literal["root_domain", "subdomain", "ip", "service", "endpoint", "app"] = Query("root_domain", alias="type"),
    limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0),
):
    data = await _forward("/api/assets", params={
        "task_id": task_id, "type": asset_type, "limit": limit, "offset": offset,
    })
    # Asset auth entries may contain credentials. This workbench does not need them.
    if isinstance(data, dict) and isinstance(data.get("assets"), list):
        for asset in data["assets"]:
            if isinstance(asset, dict):
                asset.pop("auth", None)
    return data


@router.post("/assets")
async def add_asset(request: Request, body: AssetAdd):
    _require_browser_write(request)
    field = {"root_domain": "domain", "subdomain": "domain", "ip": "ip", "service": "url"}[body.kind]
    return await _forward("/api/assets", method="POST", body={
        "task_id": body.task_id, "assets": [{"type": body.kind, field: body.value.strip()}],
    })


@router.get("/models")
async def models():
    return await _forward("/api/llm")


@router.get("/models/profiles")
async def model_profiles():
    return await _forward("/api/llm/profiles")


@router.post("/models/active")
async def activate_model_profile(request: Request, body: ProfileActivate):
    _require_browser_write(request)
    return await _forward("/api/llm/profiles/active", method="POST", body=body.model_dump())


@router.get("/models/pool")
async def model_pool():
    return await _forward("/api/llm/pool")


@router.post("/models/local/discover")
async def discover_local_models(request: Request, body: LocalModelTarget):
    _require_browser_write(request)
    return await _forward("/api/llm/models", method="POST", body={
        "provider": "ollama", "base_url": body.base_url,
    })


@router.post("/models/local/profiles")
async def create_local_profile(request: Request, body: LocalProfileCreate):
    _require_browser_write(request)
    return await _forward("/api/llm/profiles", method="POST", body={
        "name": body.name, "format": "ollama", "model": body.model,
        "base_url": body.base_url, "streaming": True, "context_window_k": 32,
        "max_tokens": 4096, "keep_alive": "30m", "llm_max_concurrent": 1,
    })


@router.post("/models/local/profiles/{profile_id}/test")
async def test_local_profile(request: Request, profile_id: int = Path(gt=0)):
    _require_browser_write(request)
    profiles = await _forward("/api/llm/profiles")
    profile = next((p for p in profiles.get("profiles", [])
                    if isinstance(p, dict) and str(p.get("id")) == str(profile_id)), None)
    if not profile or profile.get("format") != "ollama" or profile.get("base_url") not in LOCAL_MODEL_ENDPOINTS:
        raise HTTPException(status_code=404, detail="Local Ollama profile not found")
    model = profile.get("model")
    if not isinstance(model, str) or not model:
        raise HTTPException(status_code=404, detail="Local Ollama profile has no model")
    return await _forward("/api/llm/test", method="POST", timeout=120, body={
        "profile_id": profile_id, "provider": "ollama", "model": model,
        "base_url": profile["base_url"], "streaming": True,
        "context_window_k": 32,
    })


@router.get("/models/chatgpt")
async def chatgpt_status(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await _forward("/api/chatgpt/status")


def _require_local_chatgpt(request: Request):
    if request.url.hostname not in ("localhost", "127.0.0.1", "::1") or urlsplit(_client().origin).hostname not in ("localhost", "127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="ChatGPT account management requires both servers and this browser on the same machine")


@router.post("/models/chatgpt/connect")
async def chatgpt_connect(request: Request, response: Response, body: ChatGPTConnect):
    _require_browser_write(request)
    _require_local_chatgpt(request)
    result = await _forward("/api/chatgpt/login", method="POST", body=body.model_dump())
    url = result.get("url") if isinstance(result, dict) else None
    try:
        parsed = urlsplit(url) if isinstance(url, str) and len(url) <= 8192 else None
    except ValueError:
        parsed = None
    if not parsed or parsed.scheme != "https" or parsed.netloc != "auth.openai.com" or parsed.path != "/api/accounts/authorize" or not parsed.query or parsed.fragment:
        raise HTTPException(status_code=502, detail="Afterglow returned an invalid ChatGPT authorization URL")
    response.headers["Cache-Control"] = "no-store"
    return {"url": url, "expires_at": result.get("expires_at")}


@router.post("/models/chatgpt/disconnect")
async def chatgpt_disconnect(request: Request, body: ChatGPTAccount):
    _require_browser_write(request)
    _require_local_chatgpt(request)
    return await _forward("/api/chatgpt/logout", method="POST", body=body.model_dump())


@router.get("/models/chatgpt/catalog")
async def chatgpt_catalog(account_id: str = Query(min_length=1, max_length=200)):
    account_id = account_id.strip()
    if not account_id:
        raise HTTPException(status_code=422, detail="Account is required")
    return await _forward("/api/chatgpt/models", params={"account_id": account_id})


@router.post("/models/chatgpt/profiles")
async def chatgpt_profile(request: Request, body: ChatGPTProfile):
    _require_browser_write(request)
    status = await _forward("/api/chatgpt/status")
    accounts = status.get("accounts", []) if isinstance(status, dict) else []
    if not any(isinstance(a, dict) and a.get("id") == body.account_id and a.get("connected") and a.get("plan_enabled") for a in accounts):
        raise HTTPException(status_code=400, detail="Choose a connected ChatGPT plan account")
    catalog = await _forward("/api/chatgpt/models", params={"account_id": body.account_id})
    models = catalog.get("models", []) if isinstance(catalog, dict) else []
    if not any(isinstance(model, dict) and model.get("id") == body.model for model in models):
        raise HTTPException(status_code=400, detail="Choose an available ChatGPT model")
    return await _forward("/api/llm/profiles", method="POST", body={
        "name": body.name, "format": "openai-responses", "auth_mode": "chatgpt_plan",
        "chatgpt_account_id": body.account_id, "model": body.model,
        "pool_exclude": True, "streaming": True,
    })


@router.post("/models/chatgpt/profiles/{profile_id}/recheck")
async def chatgpt_profile_recheck(request: Request, profile_id: int = Path(gt=0)):
    _require_browser_write(request)
    _require_local_chatgpt(request)
    return await _forward("/api/chatgpt/recheck", method="POST", body={"profile_id": profile_id}, timeout=120)


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


@router.get("/traffic/exchange")
async def traffic_exchange(id: str = Query(min_length=1, max_length=120)):
    return await _forward("/api/traffic/exchange", params={"id": id})


@router.get("/tasks/{task_id}/report")
async def task_report(task_id: int = Path(gt=0)):
    return {"markdown": await _forward("/api/report", params={"task": task_id}, as_text=True)}
