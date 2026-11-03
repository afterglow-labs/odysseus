"""Small, fixed-origin client for the separate Afterglow server.

The browser never receives the Afterglow credential. Callers supply only
allowlisted API paths assembled by ``routes.cybersecurity_routes``.
"""

import asyncio
import json
import os
from functools import lru_cache
from urllib.parse import urlsplit

import httpx


MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AfterglowError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def validate_origin(value: str, *, allow_http: bool = False) -> str:
    """Accept HTTPS or loopback HTTP; private HTTP needs an explicit opt-in."""
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AFTERGLOW_URL has an invalid port") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or port == 0
        or parsed.scheme not in ("http", "https")
        or (parsed.scheme == "http" and not allow_http and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ValueError("AFTERGLOW_URL must be an HTTPS origin or a loopback HTTP origin")
    return value.rstrip("/")


class AfterglowClient:
    def __init__(self, *, origin: str, token: str = "", password: str = "", allow_http: bool = False, transport=None):
        self.origin = validate_origin(origin, allow_http=allow_http)
        if "\r" in token or "\n" in token:
            raise ValueError("AFTERGLOW_TOKEN contains a line break")
        self._supplied_token = token
        self._token = token
        self._password = password
        self._login_lock = asyncio.Lock()
        self._transport = transport

    async def _fetch(self, path: str, *, method: str = "GET", body=None, params=None, auth=True, as_text=False, timeout=20):
        if not path.startswith("/api/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("Invalid Afterglow API path")
        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=False, trust_env=False, transport=self._transport,
            ) as client:
                async with client.stream(
                    method, self.origin + path, params=params, headers=headers, json=body
                ) as response:
                    if response.is_redirect:
                        raise AfterglowError(502, "Afterglow returned an unexpected redirect")
                    chunks = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise AfterglowError(502, "Afterglow response is too large")
                        chunks.append(chunk)
                    payload = b"".join(chunks)
                    if as_text and response.status_code < 400:
                        try:
                            return payload.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise AfterglowError(502, "Afterglow returned invalid text") from exc
                    try:
                        data = json.loads(payload)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise AfterglowError(502, "Afterglow returned invalid JSON") from exc
                    if response.status_code >= 400:
                        detail = data.get("error") if isinstance(data, dict) else None
                        if not isinstance(detail, str) or response.status_code >= 500:
                            detail = "Afterglow request failed"
                        detail = detail[:300]
                        code = response.status_code if response.status_code in (400, 403, 404, 409) else 502
                        if response.status_code == 401:
                            code = 503
                            detail = (
                                "AFTERGLOW_TOKEN is invalid or expired" if self._supplied_token
                                else "Afterglow authentication failed"
                            )
                        raise AfterglowError(code, detail)
                    return data
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise AfterglowError(503, "Afterglow is unavailable") from exc

    async def _authenticate(self):
        if self._token:
            return
        if not self._password:
            raise AfterglowError(503, "Set AFTERGLOW_PASSWORD or AFTERGLOW_TOKEN on the Odysseus server")
        async with self._login_lock:
            if self._token:
                return
            data = await self._fetch(
                "/api/auth/login", method="POST",
                body={"username": "ARTEX", "password": self._password}, auth=False,
            )
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise AfterglowError(502, "Afterglow login returned no token")
            self._token = token

    async def request(self, path: str, *, method: str = "GET", body=None, params=None, auth=True, as_text=False, timeout=20):
        if auth:
            await self._authenticate()
        try:
            return await self._fetch(path, method=method, body=body, params=params, auth=auth, as_text=as_text, timeout=timeout)
        except AfterglowError as exc:
            if exc.detail != "Afterglow authentication failed" or self._supplied_token or not self._password:
                raise
            # A password-managed JWT may have expired. Refresh once, then stop.
            self._token = ""
            await self._authenticate()
            return await self._fetch(path, method=method, body=body, params=params, auth=auth, as_text=as_text, timeout=timeout)


@lru_cache(maxsize=1)
def get_afterglow_client() -> AfterglowClient:
    allow_http = os.getenv("AFTERGLOW_ALLOW_HTTP", "false").lower()
    if allow_http not in ("true", "false"):
        raise ValueError("AFTERGLOW_ALLOW_HTTP must be true or false")
    return AfterglowClient(
        origin=os.getenv("AFTERGLOW_URL", "http://127.0.0.1:8787"),
        token=os.getenv("AFTERGLOW_TOKEN", ""),
        password=os.getenv("AFTERGLOW_PASSWORD", ""),
        allow_http=allow_http == "true",
    )
