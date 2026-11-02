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
async def test_oversized_response_is_rejected():
    client = AfterglowClient(
        origin="http://127.0.0.1:8787", token="token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"a" * (MAX_RESPONSE_BYTES + 1))),
    )
    with pytest.raises(AfterglowError, match="too large"):
        await client.request("/api/tasks")


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
        response = browser.post(path, headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}, json={"action": "pause"})
        assert response.status_code == 200
        assert calls == [("/api/tasks/12/control", {"method": "POST", "body": {"action": "pause"}, "params": None, "auth": True})]
        assert browser.post("/api/cybersecurity/tasks/-1/control", headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}, json={"action": "pause"}).status_code == 422
        assert browser.post(path, headers={"X-Test-User": "admin", "X-Requested-With": "XMLHttpRequest"}, json={"action": "delete"}).status_code == 422
