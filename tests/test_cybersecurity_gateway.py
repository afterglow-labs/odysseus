"""Security and contract checks for the Odysseus → Afterglow boundary."""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import core.middleware as middleware
import routes.cybersecurity_routes as cyber_routes
from src.afterglow_client import AfterglowClient, AfterglowError, MAX_RESPONSE_BYTES, validate_origin


@pytest.mark.parametrize("origin", [
    "http://127.0.0.1:8787", "http://localhost:8787", "http://[::1]:8787", "https://afterglow.example",
])
def test_allowed_afterglow_origins(origin):
    assert validate_origin(origin) == origin


@pytest.mark.parametrize("origin", [
    "http://afterglow.example", "http://127.0.0.1:8787/api", "https://user:pass@afterglow.example",
    "https://afterglow.example?x=1", "http://127.0.0.1:99999", "file:///tmp/secret",
])
def test_rejected_afterglow_origins(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_private_http_requires_explicit_opt_in():
    assert validate_origin("http://scopeweaver:8787", allow_http=True) == "http://scopeweaver:8787"


@pytest.mark.asyncio
async def test_client_does_not_follow_redirect_or_expose_token():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.example/steal"})

    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="secret-token", transport=httpx.MockTransport(respond),
    )
    with pytest.raises(AfterglowError, match="unexpected redirect"):
        await client.request("/api/tasks")
    assert len(requests) == 1
    assert requests[0].url.host == "127.0.0.1"
    assert requests[0].headers["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_password_login_reuses_token_then_refreshes_expired_token():
    seen = []
    logins = 0

    def respond(request):
        nonlocal logins
        seen.append(request)
        if request.url.path == "/api/auth/login":
            logins += 1
            assert "Authorization" not in request.headers
            assert request.content.decode().count('"password":"local-password"') == 1
            return httpx.Response(200, json={"token": f"token-{logins}"})
        if request.headers.get("Authorization") == "Bearer token-1" and len(seen) > 2:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"tasks": []})

    client = AfterglowClient(
        origin="http://127.0.0.1:8787", password="local-password", transport=httpx.MockTransport(respond),
    )
    assert await client.request("/api/tasks") == {"tasks": []}
    assert await client.request("/api/tasks") == {"tasks": []}
    assert logins == 2
    assert seen[-1].headers["Authorization"] == "Bearer token-2"


@pytest.mark.asyncio
async def test_supplied_gateway_token_expires_with_actionable_error():
    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="expired-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "expired"})),
    )
    with pytest.raises(AfterglowError, match="AFTERGLOW_TOKEN is invalid or expired") as rejected:
        await client.request("/api/tasks")
    assert rejected.value.status_code == 503


@pytest.mark.asyncio
async def test_oversized_response_is_rejected():
    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"a" * (MAX_RESPONSE_BYTES + 1))),
    )
    with pytest.raises(AfterglowError, match="too large"):
        await client.request("/api/tasks")


@pytest.mark.asyncio
async def test_afterglow_local_only_rejection_is_preserved():
    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="token",
        transport=httpx.MockTransport(lambda request: httpx.Response(403, json={"error": "Local browser required"})),
    )
    with pytest.raises(AfterglowError) as rejected:
        await client.request("/api/chatgpt/login", method="POST", body={"account_id": ""})
    assert rejected.value.status_code == 403
    assert rejected.value.detail == "Local browser required"


@pytest.mark.asyncio
async def test_markdown_report_stays_bounded_plain_text():
    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="token",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, text="# Report\n<script>alert(1)</script>", headers={"Content-Type": "text/markdown"},
        )),
    )
    assert await client.request("/api/report", params={"task": 7}, as_text=True) == "# Report\n<script>alert(1)</script>"


def test_gateway_requires_admin_and_write_header(monkeypatch):
    calls = []

    class FakeAfterglow:
        async def request(self, path, **kwargs):
            calls.append((path, kwargs))
            return {"ok": True}

    class FakeAuth:
        is_configured = True

        def is_admin(self, user):
            return user == "admin"

    app = FastAPI()
    app.state.auth_manager = FakeAuth()

    @app.middleware("http")
    async def test_identity(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(cyber_routes.router)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    monkeypatch.setattr(cyber_routes, "get_afterglow_client", lambda: FakeAfterglow())
    with TestClient(app) as browser:
        path = "/api/cybersecurity/tasks/12/control"
        assert browser.post(path, json={"action": "pause"}).status_code == 403
        assert browser.post(path, headers={"X-Test-User": "viewer", "X-Requested-With": "XMLHttpRequest"}, json={"action": "pause"}).status_code == 403
        assert browser.post(path, headers={"X-Test-User": "admin"}, json={"action": "pause"}).status_code == 403
        assert browser.post(path, headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest", "Origin": "https://other.example"}, json={"action": "pause"}).status_code == 403
        response = browser.post(path, headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest", "Origin": "http://testserver"}, json={"action": "pause"})
        assert response.status_code == 200
        assert calls == [("/api/tasks/12/control", {"method": "POST", "body": {"action": "pause"}, "params": None, "auth": True})]
        assert browser.post("/api/cybersecurity/tasks/-1/control", headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}, json={"action": "pause"}).status_code == 422
        assert browser.post(path, headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}, json={"action": "delete"}).status_code == 422
        assert browser.get("/api/cybersecurity/tasks/12/activity", headers={"X-Test-User": "admin"}).status_code == 200
        assert calls[-1] == ("/api/exploration/activity/history", {"method": "GET", "body": None, "params": {"task": 12, "session": "main", "before": 0, "limit": 100}, "auth": True})


def test_operations_use_typed_gateway_and_omit_asset_credentials(monkeypatch):
    calls = []

    class FakeAfterglow:
        async def request(self, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/assets" and kwargs["method"] == "GET":
                return {"assets": [{"id": 3, "domain": "example.test", "auth": [{"token": "secret"}]}]}
            if path == "/api/tasks/7/c2-handoffs/summary":
                return {"handoffs": [{"id": "2", "vector": "webshell_upload", "target_host": "lab.test",
                    "target_path": "/test", "status": "requested", "shell_url": "https://secret.test/token"}]}
            if path == "/api/report":
                return "# Report"
            return {"id": "7", "ok": True}

    class FakeAuth:
        is_configured = True

        def is_admin(self, user):
            return user == "admin"

    app = FastAPI()
    app.state.auth_manager = FakeAuth()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(cyber_routes.router)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    monkeypatch.setattr(cyber_routes, "get_afterglow_client", lambda: FakeAfterglow())
    headers = {"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}
    with TestClient(app) as browser:
        monkeypatch.setenv("AFTERGLOW_PUBLIC_URL", "http://untrusted.example")
        invalid_status = browser.get("/api/cybersecurity/status", headers=headers)
        assert invalid_status.status_code == 200
        assert invalid_status.json()["console_url"] == ""
        assert "AFTERGLOW_PUBLIC_URL" in invalid_status.json()["console_error"]
        monkeypatch.setenv("AFTERGLOW_PUBLIC_URL", "https://afterglow.example")
        assert browser.get("/api/cybersecurity/status", headers=headers).json()["console_url"] == "https://afterglow.example"
        created = browser.post("/api/cybersecurity/tasks", headers=headers, json={
            "name": " Lab ", "description": " Test target ", "goal": " Find issues ", "injected": "ignored",
        })
        assert created.status_code == 200
        assert calls[-1] == ("/api/tasks", {"method": "POST", "body": {
            "name": "Lab", "description": "Test target", "goal": "Find issues",
            "coverage_enabled": True, "seed_first_intent": False,
        }, "params": None, "auth": True})
        assert browser.post("/api/cybersecurity/tasks", headers=headers, json={
            "description": "  ", "goal": "find issues",
        }).status_code == 422
        assert browser.get("/api/cybersecurity/tasks/7/goals", headers=headers).status_code == 200
        assert calls[-1][0] == "/api/tasks/7/goals"
        assert browser.post("/api/cybersecurity/tasks/7/goals", headers=headers, json={
            "text": " Check access control ", "vulnclass": " authorization ", "ignored": "value",
        }).status_code == 200
        assert calls[-1][1]["body"] == {"text": "Check access control", "vulnclass": "authorization"}
        assert browser.patch("/api/cybersecurity/tasks/7/goals/2", headers=headers, json={
            "text": "Check authentication", "vulnclass": "",
        }).status_code == 200
        assert calls[-1][0] == "/api/tasks/7/goals/2"
        assert browser.post("/api/cybersecurity/tasks/7/goals", headers=headers, json={
            "text": "   ",
        }).status_code == 422
        assert browser.delete("/api/cybersecurity/tasks/7/goals/2", headers=headers).status_code == 200
        assert calls[-1][1]["method"] == "DELETE"
        assert browser.get("/api/cybersecurity/tasks/7/constraints", headers=headers).status_code == 200
        assert calls[-1][0] == "/api/tasks/7/constraints"
        assert browser.post("/api/cybersecurity/tasks/7/constraints", headers=headers, json={
            "kind": "deny", "text": " Do not contact production ", "ignored": "value",
        }).status_code == 200
        assert calls[-1][1]["body"] == {"kind": "deny", "text": "Do not contact production"}
        assert browser.patch("/api/cybersecurity/tasks/7/constraints/3", headers=headers, json={
            "kind": "allow", "text": "Use the lab only",
        }).status_code == 200
        assert calls[-1][0] == "/api/tasks/7/constraints/3"
        assert browser.post("/api/cybersecurity/tasks/7/constraints", headers=headers, json={
            "kind": "block", "text": "No production",
        }).status_code == 422
        assert browser.delete("/api/cybersecurity/tasks/7/constraints/3", headers=headers).status_code == 200
        assert calls[-1][1]["method"] == "DELETE"
        for name in ("outcome", "lessons", "waves", "artifacts", "business-map", "campaign", "hunt-classes", "attack-chains"):
            assert browser.get(f"/api/cybersecurity/tasks/7/{name}", headers=headers).status_code == 200
            assert calls[-1] == (f"/api/tasks/7/{name}", {
                "method": "GET", "body": None, "params": None, "auth": True,
            })
        handoffs = browser.get("/api/cybersecurity/tasks/7/c2-handoffs", headers=headers)
        assert handoffs.status_code == 200
        assert handoffs.json() == {"handoffs": [{"id": "2", "vector": "webshell_upload",
            "target_host": "lab.test", "target_path": "/test", "status": "requested"}]}
        assert calls[-1][0] == "/api/tasks/7/c2-handoffs/summary"
        assert browser.post("/api/cybersecurity/tasks/7/lessons/extract", headers={"X-Test-User": "admin"}).status_code == 403
        assert browser.post("/api/cybersecurity/tasks/7/lessons/extract", headers=headers).status_code == 200
        assert calls[-1] == ("/api/tasks/7/lessons/extract", {
            "method": "POST", "body": {}, "params": None, "auth": True,
        })
        assert browser.post("/api/cybersecurity/tasks/7/chat", headers=headers, json={"message": " hello "}).status_code == 200
        assert calls[-1] == ("/api/chat", {"method": "POST", "body": {"message": "hello"}, "params": {"task": 7}, "auth": True})
        assert browser.post("/api/cybersecurity/tasks/7/scope", headers=headers, json={
            "kind": "root_domain", "value": "  ",
        }).status_code == 422
        assert browser.post("/api/cybersecurity/assets", headers=headers, json={
            "kind": "service", "value": "file:///etc/passwd", "task_id": 7,
        }).status_code == 422
        assert browser.get("/api/cybersecurity/assets?task_id=7", headers=headers).json() == {
            "assets": [{"id": 3, "domain": "example.test"}],
        }
        assert browser.get("/api/cybersecurity/tasks/7/report", headers=headers).json() == {"markdown": "# Report"}
        assert calls[-1][1]["as_text"] is True
        assert browser.delete("/api/cybersecurity/tasks/7/rules/3", headers={"X-Test-User": "admin"}).status_code == 403
        assert browser.delete("/api/cybersecurity/tasks/7/rules/3", headers=headers).status_code == 200
        assert browser.post("/api/cybersecurity/tasks/7/rules", headers=headers, json={
            "action": "block", "kind": "root_domain", "pattern": "example.test",
        }).status_code == 422
        assert browser.post("/api/cybersecurity/models/active", headers={**headers, "Origin": "https://other.example"}, json={"id": 3}).status_code == 403
        assert browser.post("/api/cybersecurity/models/local/discover", headers=headers, json={
            "base_url": "http://untrusted.example:11434",
        }).status_code == 422
        assert browser.post("/api/cybersecurity/models/local/discover", headers=headers, json={
            "base_url": "http://ollama:11434",
        }).status_code == 200
        assert calls[-1] == ("/api/llm/models", {"method": "POST", "body": {
            "provider": "ollama", "base_url": "http://ollama:11434",
        }, "params": None, "auth": True})
        assert browser.post("/api/cybersecurity/models/local/profiles", headers=headers, json={
            "base_url": "http://ollama:11434", "name": " Lab ", "model": " security-model ", "api_key": "ignored",
        }).status_code == 200
        assert calls[-1][1]["body"] == {
            "name": "Lab", "format": "ollama", "model": "security-model", "base_url": "http://ollama:11434",
            "streaming": True, "context_window_k": 32, "max_tokens": 4096,
            "keep_alive": "30m", "llm_max_concurrent": 1,
        }


def test_local_model_and_chatgpt_management_boundaries(monkeypatch):
    calls = []
    login_url = "https://auth.openai.com/api/accounts/authorize?state=opaque"

    class FakeAfterglow:
        origin = "http://127.0.0.1:8787"

        async def request(self, path, **kwargs):
            calls.append((path, kwargs))
            if path == "/api/llm/profiles" and kwargs["method"] == "GET":
                return {"profiles": [
                    {"id": "1", "format": "ollama", "model": "qwen3:32b", "base_url": "http://127.0.0.1:11434"},
                    {"id": "2", "format": "ollama", "model": "other", "base_url": "http://untrusted.example"},
                ]}
            if path == "/api/llm/test":
                return {"ok": True, "gpu_offload_percent": 100}
            if path == "/api/chatgpt/status":
                return {"accounts": [{"id": "account-1", "connected": True, "plan_enabled": True}]}
            if path == "/api/chatgpt/login":
                return {"url": login_url, "expires_at": "2026-11-02T20:00:00Z"}
            if path == "/api/chatgpt/models":
                return {"models": [{"id": "test-model", "name": "Test model"}]}
            return {"ok": True}

    class FakeAuth:
        is_configured = True

        def is_admin(self, user):
            return user == "admin"

    app = FastAPI()
    app.state.auth_manager = FakeAuth()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(cyber_routes.router)
    monkeypatch.setattr(middleware, "auth_disabled", lambda: False)
    monkeypatch.setattr(cyber_routes, "get_afterglow_client", lambda: FakeAfterglow())
    headers = {"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}
    with TestClient(app, base_url="http://127.0.0.1:7000") as browser:
        local = browser.post("/api/cybersecurity/models/local/profiles/1/test", headers=headers)
        assert local.status_code == 200
        assert local.json()["gpu_offload_percent"] == 100
        assert calls[-1] == ("/api/llm/test", {"method": "POST", "body": {
            "profile_id": 1, "provider": "ollama", "model": "qwen3:32b",
            "base_url": "http://127.0.0.1:11434", "streaming": True,
            "context_window_k": 32,
        }, "params": None, "auth": True, "timeout": 120})
        assert browser.post("/api/cybersecurity/models/local/profiles/2/test", headers=headers).status_code == 404
        assert browser.post("/api/cybersecurity/models/local/profiles/3/test", headers=headers).status_code == 404

        connect = browser.post("/api/cybersecurity/models/chatgpt/connect", headers=headers, json={})
        assert connect.status_code == 200
        assert connect.json()["url"] == login_url
        assert connect.headers["Cache-Control"] == "no-store"
        assert browser.post("/api/cybersecurity/models/chatgpt/connect", headers={"X-Test-User": "admin"}, json={}).status_code == 403
        assert browser.get("/api/cybersecurity/models/chatgpt", headers=headers).headers["Cache-Control"] == "no-store"
        assert browser.get("/api/cybersecurity/models/chatgpt/catalog?account_id=account-1", headers=headers).json()["models"][0]["id"] == "test-model"
        assert browser.post("/api/cybersecurity/models/chatgpt/profiles", headers=headers, json={
            "account_id": "unknown", "model": "test-model", "name": "Bad",
        }).status_code == 400
        assert browser.post("/api/cybersecurity/models/chatgpt/profiles", headers=headers, json={
            "account_id": "account-1", "model": "unavailable-model", "name": "Bad",
        }).status_code == 400
        saved = browser.post("/api/cybersecurity/models/chatgpt/profiles", headers=headers, json={
            "account_id": " account-1 ", "model": " test-model ", "name": " Plan ", "api_key": "ignored",
        })
        assert saved.status_code == 200
        assert calls[-1][1]["body"] == {
            "name": "Plan", "format": "openai-responses", "auth_mode": "chatgpt_plan",
            "chatgpt_account_id": "account-1", "model": "test-model",
            "pool_exclude": True, "streaming": True,
        }
        assert browser.post("/api/cybersecurity/models/chatgpt/profiles/1/recheck", headers=headers).status_code == 200
        assert calls[-1][1]["timeout"] == 120
        assert browser.post("/api/cybersecurity/models/chatgpt/disconnect", headers=headers, json={"account_id": "account-1"}).status_code == 200
        login_url = "https://evil.example/api/accounts/authorize?state=opaque"
        assert browser.post("/api/cybersecurity/models/chatgpt/connect", headers=headers, json={}).status_code == 502
        login_url = "https://[bad"
        assert browser.post("/api/cybersecurity/models/chatgpt/connect", headers=headers, json={}).status_code == 502

    with TestClient(app, base_url="https://remote.example") as remote:
        assert remote.get("/api/cybersecurity/models/chatgpt", headers=headers).status_code == 200
        assert remote.post("/api/cybersecurity/models/chatgpt/connect", headers=headers, json={}).status_code == 403
