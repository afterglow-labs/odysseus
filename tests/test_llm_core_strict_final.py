"""Opt-in completed-answer extraction for manual prompt enhancement."""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest
from fastapi import HTTPException

from src import llm_core


@pytest.fixture(autouse=True)
def isolated_calls(monkeypatch):
    monkeypatch.setattr(llm_core, "_response_cache", {})
    monkeypatch.setattr(llm_core, "_response_model_cache", {})
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda _: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: object())
    monkeypatch.setattr(llm_core, "get_context_length", lambda *a, **k: 4096)


def completion(content="Final prompt.", *, finish="stop", **message):
    return {"model": "selected-model", "choices": [{"finish_reason": finish,
            "message": {"content": content, **message}}]}


def mock_post(monkeypatch, data):
    class Response:
        is_success = True
        def json(self):
            return data
    async def post(*args, **kwargs):
        return Response()
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)


def call(*, strict=True, url="https://model.example/v1/chat/completions", **kwargs):
    return asyncio.run(llm_core.llm_call_async(
        url, "selected-model", [{"role": "user", "content": "Rewrite my draft"}],
        strict_final_only=strict, max_retries=1, **kwargs,
    ))


@pytest.mark.parametrize("data", [
    completion(None, reasoning_content="Private reasoning"),
    completion("", reasoning_content="Private reasoning"),
    completion("Incomplete answer", finish="length"),
    completion("Incomplete answer", finish=None),
    completion("Tool answer", finish="tool_calls"),
    completion("Tool answer", tool_calls=[{"function": {"name": "tool"}}]),
    completion("Tool answer", function_call={"name": "tool"}),
    completion([{"type": "thinking", "thinking": "Private reasoning"}]),
])
def test_strict_rejects_unfinished_or_non_answer_content(monkeypatch, data):
    mock_post(monkeypatch, data)
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == 502
    assert error.value.fallback_eligible is False
    assert not llm_core._response_cache


def test_strict_mistral_keeps_only_final_text_and_metadata(monkeypatch):
    mock_post(monkeypatch, completion([
        {"type": "thinking", "thinking": [{"type": "text", "text": "Private reasoning"}]},
        {"type": "text", "text": "Final "},
        {"type": "text", "text": "prompt."},
    ]))
    assert call(return_model_metadata=True) == ("Final prompt.", "selected-model")


def test_strict_cache_is_separate_from_legacy_reasoning_fallback(monkeypatch):
    mock_post(monkeypatch, completion(None, reasoning_content="Legacy reasoning"))
    assert call(strict=False) == "Legacy reasoning"
    with pytest.raises(HTTPException):
        call()
    mock_post(monkeypatch, completion("Final prompt."))
    assert call() == "Final prompt."
    assert call(strict=False) == "Legacy reasoning"
    async def unexpected_post(*args, **kwargs):
        raise AssertionError("Strict answer should be cached separately")
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", unexpected_post)
    assert call() == "Final prompt."


@pytest.mark.parametrize(("stop_reason", "expected"), [
    ("end_turn", "Final prompt."), ("stop_sequence", "Final prompt."),
    ("max_tokens", None), ("tool_use", None), (None, None),
])
def test_strict_anthropic_final_text(monkeypatch, stop_reason, expected):
    mock_post(monkeypatch, {"stop_reason": stop_reason, "content": [
        {"type": "thinking", "thinking": "Private reasoning"},
        {"type": "text", "text": "Final prompt."},
    ]})
    if expected:
        assert call(url="https://api.anthropic.com/v1/messages") == expected
    else:
        with pytest.raises(HTTPException):
            call(url="https://api.anthropic.com/v1/messages")


@pytest.mark.parametrize(("done", "reason", "content", "expected"), [
    (True, "stop", "Final prompt.", "Final prompt."),
    (False, "stop", "Partial", None), (True, "length", "Partial", None),
    (True, "stop", "", None),
])
def test_strict_native_ollama_completion(monkeypatch, done, reason, content, expected):
    mock_post(monkeypatch, {"done": done, "done_reason": reason,
              "message": {"content": content, "thinking": "Private reasoning"}})
    if expected:
        assert call(url="https://ollama.example:11434/api/chat") == expected
    else:
        with pytest.raises(HTTPException):
            call(url="https://ollama.example:11434/api/chat")


def subscription(monkeypatch, events, *, strict=True):
    class Response:
        status_code = 200
        async def aiter_lines(self):
            for event in events:
                yield "data: " + json.dumps(event)
    class Client:
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield Response()
    monkeypatch.setattr(llm_core, "_get_http_client", Client)
    return call(strict=strict, url="https://chatgpt.com/backend-api/codex/responses")


def test_strict_subscription_requires_completed_final_answer(monkeypatch):
    assert subscription(monkeypatch, [
        {"type": "response.reasoning_summary_text.delta", "delta": "Private reasoning"},
        {"type": "response.output_text.delta", "delta": "Final prompt."},
        {"type": "response.completed", "response": {"status": "completed"}},
    ]) == "Final prompt."


@pytest.mark.parametrize("ending", [
    [],
    [{"type": "response.incomplete", "response": {"status": "incomplete"}}],
    [{"type": "response.completed", "response": {"status": "incomplete"}}],
    [{"type": "response.completed", "response": {"status": "completed",
       "incomplete_details": {"reason": "max_output_tokens"}}}],
    [{"type": "response.output_item.added", "item": {"type": "function_call"}}],
    [{"type": "response.completed", "response": {"status": "completed",
       "output": [{"type": "function_call"}]}}],
])
def test_strict_subscription_rejects_partial_or_tool_output(monkeypatch, ending):
    with pytest.raises(HTTPException) as error:
        subscription(monkeypatch, [{"type": "response.output_text.delta", "delta": "Partial"}, *ending])
    assert error.value.status_code == 502
    assert not llm_core._response_cache


def test_strict_subscription_rejects_reasoning_only(monkeypatch):
    with pytest.raises(HTTPException):
        subscription(monkeypatch, [
            {"type": "response.reasoning_summary_text.delta", "delta": "Private reasoning"},
            {"type": "response.completed", "response": {"status": "completed"}},
        ])


def test_subscription_legacy_eof_behavior_is_unchanged(monkeypatch):
    assert subscription(monkeypatch, [
        {"type": "response.output_text.delta", "delta": "Legacy partial"},
    ], strict=False) == "Legacy partial"


def test_strict_call_propagates_cancellation(monkeypatch):
    async def cancelled_post(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", cancelled_post)
    with pytest.raises(asyncio.CancelledError):
        call()
    assert not llm_core._response_cache
