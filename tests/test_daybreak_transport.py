"""Daybreak is an explicit, request-local program on subscription Responses."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src import llm_core
from src.chatgpt_subscription import daybreak_access_program, daybreak_error
from src.request_models import ChatRequest

SUBSCRIPTION = "https://chatgpt.com/backend-api/codex"
MESSAGES = [{"role": "user", "content": "Explain how to review a security patch."}]


@pytest.fixture
def upstream(monkeypatch):
    state = SimpleNamespace(calls=[], error=None, error_event=False, error_body=None, error_status=403)

    def handler(request):
        payload = json.loads(request.content)
        state.calls.append((str(request.url), payload))
        if state.error_body is not None:
            return httpx.Response(state.error_status, json=state.error_body)
        if state.error:
            if not state.error_event:
                return httpx.Response(state.error_status, json={"error": state.error})
            events = [{"type": "response.failed", "response": {"error": state.error}}]
        elif "chatgpt.com" in str(request.url):
            events = [
                {"type": "response.output_text.delta", "delta": payload["access_programs"]["cyber"]},
                {"type": "response.completed", "response": {"model": payload["model"]}},
            ]
        else:
            events = [{"choices": [{"delta": {"content": "ordinary reply"}, "finish_reason": "stop"}]}]
        text = "\n\n".join("data: " + json.dumps(event) for event in events) + "\n\n"
        if "chatgpt.com" not in str(request.url):
            text += "data: [DONE]\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    async_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sync_client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: async_client)
    monkeypatch.setattr(llm_core.httpx, "stream", sync_client.stream)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *args: None)
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()
    yield state
    asyncio.run(async_client.aclose())
    sync_client.close()
    llm_core._response_cache.clear()
    llm_core._response_model_cache.clear()


@pytest.mark.parametrize(
    "model,enabled,expected",
    [
        ("gpt-6.1-sol", False, "standard"),
        ("gpt-6.1-sol", True, "daybreak_blue"),
        ("gpt-6-astra", True, "daybreak_blue"),
        ("gpt-6-sol", True, "daybreak_blue"),
        ("gpt-daybreak-blue-latest", True, "daybreak_blue"),
        ("gpt-5.6-cyber", True, "daybreak_red"),
        ("gpt-daybreak-red-latest", True, "daybreak_red"),
        ("gpt-daybreak-red-latest", False, "standard"),
    ],
)
@pytest.mark.asyncio
async def test_stream_sends_exact_program_and_preserves_selected_model(upstream, model, enabled, expected):
    chunks = [chunk async for chunk in llm_core.stream_llm(
        SUBSCRIPTION, model, MESSAGES, daybreak_enabled=enabled,
    )]
    url, payload = upstream.calls[0]
    assert url == SUBSCRIPTION + "/responses"
    assert payload["model"] == model
    assert payload["access_programs"] == {"cyber": expected}
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "temperature" not in payload
    assert any(expected in chunk for chunk in chunks)


@pytest.mark.parametrize("model", ["gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra", "gpt-4o", "future-model"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.asyncio
async def test_subscription_omits_temperature_for_every_model_and_program(upstream, model, enabled):
    result = await llm_core.llm_call_async(
        SUBSCRIPTION, model, MESSAGES, temperature=0.7, daybreak_enabled=enabled,
    )
    assert result == daybreak_access_program(model, enabled)
    assert len(upstream.calls) == 1
    url, payload = upstream.calls[0]
    assert url == SUBSCRIPTION + "/responses"
    assert payload["model"] == model
    assert "temperature" not in payload


@pytest.mark.asyncio
async def test_async_response_cache_is_partitioned_by_program(upstream):
    off = await llm_core.llm_call_async(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES)
    on = await llm_core.llm_call_async(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True)
    off_again = await llm_core.llm_call_async(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=False)
    assert (off, on, off_again) == ("standard", "daybreak_blue", "standard")
    assert len(upstream.calls) == 2


def test_sync_subscription_uses_responses_and_separate_program_cache(upstream):
    assert llm_core.llm_call(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True) == "daybreak_blue"
    assert llm_core.llm_call(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES) == "standard"
    assert llm_core.llm_call(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True) == "daybreak_blue"
    assert [url for url, _ in upstream.calls] == [SUBSCRIPTION + "/responses"] * 2
    assert all("temperature" not in payload for _, payload in upstream.calls)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_daybreak_state(upstream):
    on, off = await asyncio.gather(
        llm_core.llm_call_async(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True),
        llm_core.llm_call_async(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES, daybreak_enabled=False),
    )
    assert on == "daybreak_blue"
    assert off == "standard"


@pytest.mark.parametrize("url", ["https://api.openai.com/v1", "https://proxy.example/v1", "https://chatgpt.com.attacker.example/backend-api/codex"])
@pytest.mark.asyncio
async def test_enabling_on_another_provider_fails_before_http(upstream, url):
    with pytest.raises(HTTPException, match="ChatGPT Subscription"):
        await llm_core.llm_call_async(url, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True)
    with pytest.raises(HTTPException, match="ChatGPT Subscription"):
        llm_core.llm_call(url, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True)
    chunks = [chunk async for chunk in llm_core.stream_llm(url, "gpt-6.1-sol", MESSAGES, daybreak_enabled=True)]
    assert "Daybreak is available only" in chunks[0]
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_off_does_not_add_access_programs_to_other_providers(upstream):
    _ = [chunk async for chunk in llm_core.stream_llm(
        "https://api.openai.com/v1", "gpt-6.1-sol", MESSAGES, daybreak_enabled=False,
    )]
    assert len(upstream.calls) == 1
    assert "access_programs" not in upstream.calls[0][1]


@pytest.mark.parametrize("code", ["access_program_not_enabled", "invalid_access_program", "unsupported_access_program"])
@pytest.mark.parametrize("event", [False, True])
@pytest.mark.asyncio
async def test_program_error_is_specific_and_never_falls_back(upstream, code, event):
    upstream.error = {"code": code, "message": "Program is not available for this selection."}
    upstream.error_event = event
    candidates = [(SUBSCRIPTION, "gpt-6.1-sol", {}), (SUBSCRIPTION, "backup", {})]
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(
        candidates, MESSAGES, daybreak_enabled=True,
    )]
    assert len(upstream.calls) == 1
    assert "Daybreak" in chunks[0]
    assert "Reconnect" not in chunks[0]
    assert "fallback_forbidden" in chunks[0]
    assert not any('"type": "fallback"' in chunk for chunk in chunks)

    upstream.calls.clear()
    with pytest.raises(HTTPException, match="Daybreak"):
        await llm_core.llm_call_async_with_fallback(candidates, MESSAGES, daybreak_enabled=True)
    assert len(upstream.calls) == 1


def test_sync_program_error_does_not_reconnect_or_fallback(upstream):
    upstream.error = {"code": "access_program_not_enabled", "message": "Program disabled"}
    with pytest.raises(HTTPException, match="Daybreak access is not enabled"):
        llm_core.llm_call_with_fallback(
            [(SUBSCRIPTION, "gpt-6.1-sol", {}), (SUBSCRIPTION, "backup", {})],
            MESSAGES, daybreak_enabled=True,
        )
    assert len(upstream.calls) == 1


def test_ordinary_auth_failure_still_requests_reconnect():
    text = llm_core._format_chatgpt_subscription_error(401, '{"error":{"code":"token_expired"}}')
    assert "Reconnect" in text


@pytest.mark.parametrize("body", [
    {"error": {"code": "permission_denied", "message": "This workspace cannot use the selected model."}},
    {"error": {"code": "model_not_found", "message": "The selected model is not available for this account."}},
    {"detail": "Access to this model requires approval."},
])
def test_subscription_permission_denials_preserve_reason_without_reconnect(body):
    text = llm_core._format_chatgpt_subscription_error(403, json.dumps(body))
    error = body.get("error", body)
    assert error.get("message", error.get("detail")) in text
    if error.get("code"):
        assert error["code"] in text
    assert "Reconnect" not in text
    assert "credentials expired" not in text


def test_explicit_token_error_on_403_still_requests_reconnect():
    text = llm_core._format_chatgpt_subscription_error(403, '{"error":{"code":"invalid_token"}}')
    assert "Reconnect" in text


@pytest.mark.parametrize("shape", ["detail", "message", "error_string", "plain_string"])
def test_code_less_daybreak_model_denial_is_recognized(shape):
    detail = "Daybreak isn't available for this model. Turn off Daybreak or choose another model."
    body = {"detail": detail} if shape == "detail" else {"message": detail}
    if shape == "error_string":
        body = {"error": detail}
    elif shape == "plain_string":
        body = detail
    assert daybreak_error(body) == ("unsupported_access_program", detail)


@pytest.mark.asyncio
async def test_live_daybreak_detail_denial_is_terminal_on_all_subscription_paths(upstream):
    detail = "Daybreak isn't available for this model. Turn off Daybreak or choose another model."
    upstream.error_body = {"detail": detail}
    candidates = [(SUBSCRIPTION, "gpt-6.1-sol", {}), (SUBSCRIPTION, "backup", {})]
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(
        candidates, MESSAGES, daybreak_enabled=True,
    )]
    assert len(upstream.calls) == 1
    assert detail in chunks[0]
    assert "Reconnect" not in chunks[0]
    assert '"fallback_forbidden": true' in chunks[0]
    assert '"daybreak_supported": false' in chunks[0]
    assert '"model": "gpt-6.1-sol"' in chunks[0]
    assert "authentication_required" not in chunks[0]
    assert upstream.calls[0][1]["access_programs"] == {"cyber": "daybreak_blue"}

    for call in (llm_core.llm_call_async_with_fallback, llm_core.llm_call_with_fallback):
        upstream.calls.clear()
        with pytest.raises(HTTPException, match="Daybreak isn't available for this model"):
            if call is llm_core.llm_call_async_with_fallback:
                await call(candidates, MESSAGES, daybreak_enabled=True)
            else:
                call(candidates, MESSAGES, daybreak_enabled=True)
        assert len(upstream.calls) == 1
        assert upstream.calls[0][1]["access_programs"] == {"cyber": "daybreak_blue"}


@pytest.mark.parametrize("url,provider", [(SUBSCRIPTION, "chatgpt-subscription"), ("https://api.openai.com/v1", "openai")])
@pytest.mark.parametrize("status,code,expected", [(401, "invalid_token", True), (403, "invalid_token", True),
                                               (403, "permission_denied", False), (429, "rate_limit_exceeded", False)])
@pytest.mark.asyncio
async def test_only_genuine_auth_failures_offer_reconnect_for_actual_target(upstream, url, provider, status, code, expected):
    upstream.error = {"code": code, "message": "Provider rejected request"}
    upstream.error_status = status
    chunks = [chunk async for chunk in llm_core.stream_llm(url, "gpt-6-sol", MESSAGES)]
    error = json.loads(chunks[0].split("data: ", 1)[1])
    assert error.get("authentication_required", False) is expected
    if expected:
        assert error["provider"] == provider
        assert error["endpoint_url"] == upstream.calls[0][0]


@pytest.mark.asyncio
async def test_subscription_streamed_auth_error_preserves_reconnect_marker(upstream):
    upstream.error = {"code": "invalid_token", "message": "Token expired"}
    upstream.error_event = True
    chunks = [chunk async for chunk in llm_core.stream_llm(SUBSCRIPTION, "gpt-6-sol", MESSAGES)]
    error = json.loads(chunks[0].split("data: ", 1)[1])
    assert error["authentication_required"] is True
    assert error["provider"] == "chatgpt-subscription"
    assert error["endpoint_url"] == SUBSCRIPTION + "/responses"
    assert "Reconnect" in error["text"]


@pytest.mark.parametrize("mode", ["sync", "async", "stream"])
@pytest.mark.parametrize("effort", [None, "", "none", "high", "ultra"])
@pytest.mark.asyncio
async def test_subscription_reasoning_payload_is_explicit_or_provider_default(upstream, mode, effort):
    kwargs = {"reasoning_effort": effort, "daybreak_enabled": True}
    if mode == "sync":
        llm_core.llm_call(SUBSCRIPTION, "gpt-6-sol", MESSAGES, **kwargs)
    elif mode == "async":
        await llm_core.llm_call_async(SUBSCRIPTION, "gpt-6-sol", MESSAGES, **kwargs)
    else:
        _ = [chunk async for chunk in llm_core.stream_llm(SUBSCRIPTION, "gpt-6-sol", MESSAGES, **kwargs)]
    assert len(upstream.calls) == 1
    payload = upstream.calls[0][1]
    assert payload["model"] == "gpt-6-sol"
    assert payload["access_programs"] == {"cyber": "daybreak_blue"}
    if effort:
        assert payload["reasoning"] == {"effort": effort}
    else:
        assert "reasoning" not in payload


@pytest.mark.parametrize("mode", ["sync", "async"])
@pytest.mark.asyncio
async def test_subscription_cache_separates_explicit_reasoning_efforts(upstream, mode):
    for effort in (None, "high", "ultra", "high", ""):
        if mode == "sync":
            llm_core.llm_call(SUBSCRIPTION, "gpt-6-sol", MESSAGES, reasoning_effort=effort)
        else:
            await llm_core.llm_call_async(SUBSCRIPTION, "gpt-6-sol", MESSAGES, reasoning_effort=effort)
    assert [payload.get("reasoning") for _, payload in upstream.calls] == [None, {"effort": "high"}, {"effort": "ultra"}]


@pytest.mark.asyncio
async def test_reasoning_choice_does_not_leak_to_other_provider_or_fallback(upstream):
    candidates = [("https://api.openai.com/v1", "gpt-6-sol", {}), (SUBSCRIPTION, "gpt-6-sol", {})]
    with pytest.raises(HTTPException, match="only for the ChatGPT Subscription"):
        llm_core.llm_call_with_fallback(candidates, MESSAGES, reasoning_effort="high")
    with pytest.raises(HTTPException, match="only for the ChatGPT Subscription"):
        await llm_core.llm_call_async_with_fallback(candidates, MESSAGES, reasoning_effort="high")
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(candidates, MESSAGES, reasoning_effort="high")]
    assert "fallback_forbidden" in chunks[0]
    assert upstream.calls == []


@pytest.mark.parametrize("event", [False, True])
@pytest.mark.parametrize("structured_parameter", [False, True])
@pytest.mark.asyncio
async def test_upstream_rejected_effort_is_terminal_without_retry_or_downgrade(upstream, event, structured_parameter):
    upstream.error = ({"code": "invalid_request_error", "message": "Unsupported value: ultra", "param": "reasoning.effort"}
                      if structured_parameter else
                      {"code": "invalid_request_error", "message": "Unsupported value for reasoning.effort: ultra"})
    upstream.error_status = 400
    upstream.error_event = event
    candidates = [(SUBSCRIPTION, "primary", {}), (SUBSCRIPTION, "backup", {})]
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback(candidates, MESSAGES, reasoning_effort="ultra")]
    assert "fallback_forbidden" in chunks[0]
    assert len(upstream.calls) == 1
    assert upstream.calls[0][1]["reasoning"] == {"effort": "ultra"}
    upstream.calls.clear()
    with pytest.raises(HTTPException, match="Unsupported value"):
        llm_core.llm_call_with_fallback(candidates, MESSAGES, reasoning_effort="ultra")
    assert len(upstream.calls) == 1


@pytest.mark.asyncio
async def test_available_fallback_keeps_exact_requested_reasoning_effort(monkeypatch):
    seen = []
    async def fake_stream(url, model, messages, **kwargs):
        seen.append((model, kwargs["reasoning_effort"]))
        if model == "primary":
            yield 'event: error\ndata: {"status":503,"text":"Unavailable"}\n\n'
        else:
            yield 'data: {"delta":"done"}\n\n'
    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    _ = [chunk async for chunk in llm_core.stream_llm_with_fallback(
        [(SUBSCRIPTION, "primary", {}), (SUBSCRIPTION, "backup", {})], MESSAGES, reasoning_effort="ultra")]
    assert seen == [("primary", "ultra"), ("backup", "ultra")]


def test_pre_request_refresh_auth_failure_has_structured_reconnect_marker():
    from src.chatgpt_subscription import ChatGPTSubscriptionReauthRequired, ChatGPTSubscriptionRateLimited, to_http_exception
    error = to_http_exception(ChatGPTSubscriptionReauthRequired("Token expired"))
    assert error.status_code == 401
    assert error.detail["authentication_required"] is True
    assert error.detail["provider"] == "chatgpt-subscription"
    assert error.detail["endpoint_url"] == SUBSCRIPTION
    limited = to_http_exception(ChatGPTSubscriptionRateLimited("Quota reached"))
    assert limited.status_code == 429
    assert isinstance(limited.detail, str)


@pytest.mark.parametrize("with_descriptors", [True, False])
@pytest.mark.asyncio
async def test_auth_failure_after_same_url_fallback_identifies_actual_account(monkeypatch, with_descriptors):
    async def fake_stream(url, model, messages, headers=None, **kwargs):
        if headers["Authorization"] == "Bearer primary":
            yield 'event: error\ndata: {"status":503,"text":"Unavailable"}\n\n'
        else:
            yield 'event: error\ndata: ' + json.dumps({
                "status": 401, "text": "Credentials rejected", "authentication_required": True,
                "provider": "chatgpt-subscription", "endpoint_url": SUBSCRIPTION + "/responses",
            }) + '\n\n'
    monkeypatch.setattr(llm_core, "stream_llm", fake_stream)
    kwargs = {"candidate_route_descriptors": [{"endpoint_id": "account-a"}, {"endpoint_id": "account-b"}]} if with_descriptors else {}
    chunks = [chunk async for chunk in llm_core.stream_llm_with_fallback([
        (SUBSCRIPTION, "gpt-6-sol", {"Authorization": "Bearer primary"}),
        (SUBSCRIPTION, "gpt-6-sol", {"Authorization": "Bearer fallback"}),
    ], MESSAGES, **kwargs)]
    error = json.loads(chunks[0].split("data: ", 1)[1])
    assert error["candidate_index"] == 1
    assert error["endpoint_url"] == SUBSCRIPTION + "/responses"
    assert error.get("endpoint_id") == ("account-b" if with_descriptors else None)


def test_sync_subscription_string_error_remains_an_upstream_error(upstream):
    upstream.error = "Request failed upstream"
    upstream.error_event = True
    with pytest.raises(HTTPException, match="Request failed upstream"):
        llm_core.llm_call(SUBSCRIPTION, "gpt-6.1-sol", MESSAGES)


def test_chat_request_requires_actual_boolean_and_omission_uses_saved_state():
    assert ChatRequest(message="hello", session="s").daybreak_enabled is None
    assert ChatRequest(message="hello", session="s", daybreak_enabled=False).daybreak_enabled is False
    with pytest.raises(ValidationError):
        ChatRequest(message="hello", session="s", daybreak_enabled="false")


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_utility", [False, True])
async def test_compaction_preserves_program_only_for_the_selected_model(monkeypatch, separate_utility):
    import src.context_compactor as compactor

    calls = []
    monkeypatch.setattr(compactor, "get_context_length", lambda *args: 100)
    monkeypatch.setattr(compactor, "estimate_tokens", lambda *args: 99)
    monkeypatch.setattr(compactor, "resolve_endpoint", lambda *args, **kwargs: (
        ("https://utility.example/v1", "utility", {}) if separate_utility else (None, None, None)
    ))

    async def summary(*args, **kwargs):
        calls.append(kwargs)
        return "A concise summary."

    monkeypatch.setattr(compactor, "llm_call_async", summary)
    await compactor.maybe_compact(
        None, SUBSCRIPTION, "gpt-6.1-sol",
        [{"role": "user", "content": f"Message {number}"} for number in range(8)],
        persist=False, daybreak_enabled=True,
    )
    assert calls[0].get("daybreak_enabled", False) is not separate_utility
