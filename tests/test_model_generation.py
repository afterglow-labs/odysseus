import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routes.prefs_routes as prefs
from src import llm_core
from src.model_generation import apply_generation_options, capture_generation_options, generation_key, validate_generation_options


@pytest.mark.parametrize("value", [{"temperature": -1}, {"top_p": 1.1}, {"seed": 1.5}, {"thinking": "maybe"}, {"temperature": float("nan")}, {"messages": []}])
def test_invalid_controls_are_rejected(value):
    with pytest.raises(HTTPException):
        validate_generation_options(value)


def test_controls_save_per_user_and_model_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs, "PREFS_FILE", str(tmp_path / "prefs.json"))
    monkeypatch.setattr(prefs, "get_current_user", lambda request: "alice")
    app = FastAPI(); app.include_router(prefs.setup_prefs_routes())
    client = TestClient(app)
    session = SimpleNamespace(endpoint_url="http://127.0.0.1:8000/v1", model="qwen")
    key = generation_key(session.endpoint_url, session.model)
    options = {"temperature": 0, "thinking": "off", "top_k": 30, "max_tokens": 0}
    assert client.put("/api/prefs/model-generation", json={"model_key": key, "options": options}).status_code == 200
    assert capture_generation_options(session, "alice") == options
    assert capture_generation_options(session, "bob") == {}
    assert capture_generation_options(SimpleNamespace(endpoint_url=session.endpoint_url, model="other"), "alice") == {}
    before = (tmp_path / "prefs.json").read_text()
    assert client.put("/api/prefs/model-generation", json={"model_key": key, "options": {"min_p": -1}}).status_code == 422
    assert (tmp_path / "prefs.json").read_text() == before
    assert client.put("/api/prefs/model-generation", json={"model_key": key, "options": {}}).status_code == 200
    assert capture_generation_options(session, "alice") == {}


def test_local_controls_override_defaults_and_disable_qwen_thinking():
    payload = {"temperature": .2, "max_tokens": 2048, "top_p": .9}
    options = {"temperature": .75, "max_tokens": 0, "thinking": "off", "top_p": .8, "min_p": .05, "top_k": 40, "seed": 0}
    apply_generation_options(payload, options, provider="openai", local=True)
    assert payload["temperature"] == .75
    assert "max_tokens" not in payload
    assert payload["chat_template_kwargs"]["enable_thinking"] is False
    assert payload["reasoning_budget_tokens"] == 0
    assert payload["reasoning_effort"] == "none"
    assert payload["seed"] == 0
    assert payload["top_k"] == 40


def test_reasoning_none_disables_thinking_and_ollama_uses_native_options():
    payload = {}
    apply_generation_options(payload, {"reasoning_effort": "none", "top_k": 40, "temperature": 0, "max_tokens": 128}, provider="ollama")
    assert payload == {"think": False, "options": {"top_k": 40, "temperature": 0, "num_predict": 128}}
    payload = {}
    apply_generation_options(payload, {"reasoning_effort": "none"}, provider="openai", local=True)
    assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_response_cache_distinguishes_generation_options():
    args = ("http://localhost:8000/v1", "qwen", [{"role": "user", "content": "hi"}], .7, 128)
    assert llm_core._get_cache_key(*args, generation_options={"thinking": "off"}) != llm_core._get_cache_key(*args, generation_options={"thinking": "on"})


@pytest.mark.asyncio
async def test_async_and_stream_requests_send_controls_to_upstream(monkeypatch):
    payloads = []
    def respond(request):
        body = json.loads(request.content); payloads.append(body)
        if body.get("stream"):
            return httpx.Response(200, content='data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n', headers={"Content-Type": "text/event-stream"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        monkeypatch.setattr(llm_core, "_get_cached_response", lambda *args: None)
        monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
        options = {"thinking": "off", "temperature": .75, "top_k": 40, "min_p": .05, "max_tokens": 64}
        args = ("http://127.0.0.1:18781/v1", "qwen-generation-test", [{"role": "user", "content": "hi"}])
        assert await llm_core.llm_call_async(*args, generation_options=options) == "OK"
        chunks = [c async for c in llm_core.stream_llm(*args, generation_options=options)]
        assert any('OK' in c for c in chunks)
    assert len(payloads) == 2
    for body in payloads:
        assert body["temperature"] == .75
        assert body["top_k"] == 40
        assert body["min_p"] == .05
        assert body["max_tokens"] == 64
        assert body["chat_template_kwargs"]["enable_thinking"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "agent"])
async def test_chat_route_snapshots_saved_controls_before_context_changes(monkeypatch, mode):
    from tests.test_foreground_model_routing import _chat_stream_endpoint, _RouteRequest
    import routes.chat_routes as chat
    captured = {}
    endpoint = _chat_stream_endpoint(monkeypatch, mode, captured)
    async def stream(*args, **kwargs):
        captured["kwargs"] = kwargs
        yield 'data: {"delta":"OK"}\n\n'
        yield 'data: [DONE]\n\n'
    monkeypatch.setattr(chat, "stream_llm_with_fallback", stream)
    monkeypatch.setattr(chat, "stream_agent_loop", stream)
    saved = {"thinking": "off", "temperature": .8, "max_tokens": 256}
    monkeypatch.setattr(prefs, "_load_for_user", lambda owner: {"model-generation": {generation_key("https://selected.example/v1", "selected-model"): saved}})
    original = chat.build_chat_context
    async def context(*args, **kwargs):
        saved["temperature"] = .1
        return await original(*args, **kwargs)
    monkeypatch.setattr(chat, "build_chat_context", context)
    response = await endpoint(_RouteRequest(mode))
    async for _ in response.body_iterator:
        pass
    kwargs = captured["kwargs"]
    assert kwargs["temperature"] == .8
    assert kwargs["max_tokens"] == 256
    assert kwargs["generation_options"]["thinking"] == "off"
