"""Actual async provider payloads retain H3 image labels and image bytes."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from src import llm_core
from src.chatgpt_subscription import build_responses_input


IMAGE_ONE = "data:image/png;base64,AAAA"
IMAGE_TWO = "data:image/jpeg;base64,BBBB"


def messages():
    return [
        {"role": "system", "content": "Rewrite the draft using the supplied image roles."},
        {"role": "user", "content": [
            {"type": "text", "text": "<Picture 1> is the first frame; preserve this identity."},
            {"type": "image_url", "image_url": {"url": IMAGE_ONE, "detail": "high"}},
            {"type": "text", "text": "<Picture 2> is the last frame; reach this pose."},
            {"type": "image_url", "image_url": {"url": IMAGE_TWO}},
        ]},
    ]


@pytest.mark.parametrize(("provider", "url", "model"), [
    ("openai", "http://127.0.0.1:8001/v1/chat/completions", "Qwen3.8-27B"),
    ("ollama", "http://127.0.0.1:11434/api/chat", "qwen3-vl"),
    ("anthropic", "https://api.anthropic.com/v1/messages", "claude-sonnet-4"),
    ("subscription", "https://chatgpt.com/backend-api/codex/responses", "gpt-5"),
])
def test_strict_async_image_payloads(monkeypatch, provider, url, model):
    captured = []
    monkeypatch.setattr(llm_core, "_response_cache", {})
    monkeypatch.setattr(llm_core, "_response_model_cache", {})
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _: False)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda _: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_local_model_gate_enabled", lambda: False)
    monkeypatch.setattr(llm_core, "get_context_length", lambda *a, **k: 4096)

    class Response:
        is_success = True
        status_code = 200
        def json(self):
            if provider == "anthropic":
                return {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Final prompt."}]}
            if provider == "ollama":
                return {"done": True, "done_reason": "stop", "message": {"content": "Final prompt."}}
            return {"choices": [{"finish_reason": "stop", "message": {"content": "Final prompt."}}]}
        async def aiter_lines(self):
            yield "data: " + json.dumps({"type": "response.output_text.delta", "delta": "Final prompt."})
            yield "data: " + json.dumps({"type": "response.completed", "response": {"status": "completed"}})

    async def post(client, target, headers, **kwargs):
        captured.append(kwargs["json"])
        return Response()

    class Client:
        @asynccontextmanager
        async def stream(self, method, target, **kwargs):
            captured.append(kwargs["json"])
            yield Response()

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", post)
    monkeypatch.setattr(llm_core, "_get_http_client", Client)
    source = messages()
    before = deepcopy(source)
    result = asyncio.run(llm_core.llm_call_async(
        url, model, source, strict_final_only=True, max_retries=1,
        generation_options={"thinking": "off", "reasoning_effort": "none"},
    ))
    assert result == "Final prompt."
    assert source == before
    assert len(captured) == 1
    payload = captured[0]
    if provider == "subscription":
        content = payload["input"][0]["content"]
        assert [block["type"] for block in content] == ["input_text", "input_image", "input_text", "input_image"]
        assert [content[1]["image_url"], content[3]["image_url"]] == [IMAGE_ONE, IMAGE_TWO]
        assert content[1]["detail"] == "high"
        assert content[3]["detail"] == "auto"
    elif provider == "ollama":
        user = payload["messages"][-1]
        assert user["images"] == ["AAAA", "BBBB"]
        assert user["content"].index("<Picture 1>") < user["content"].index("<Picture 2>")
        assert payload["think"] is False
    else:
        content = payload["messages"][-1]["content"]
        assert "<Picture 1>" in content[0]["text"]
        assert "<Picture 2>" in content[2]["text"]
        if provider == "anthropic":
            assert content[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}
            assert content[3]["source"]["data"] == "BBBB"
        else:
            assert content == before[-1]["content"]
            assert payload["chat_template_kwargs"]["enable_thinking"] is False


def test_subscription_text_and_tool_conversion_unchanged():
    assert build_responses_input([
        {"role": "user", "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
        {"role": "assistant", "content": "answer"},
        {"role": "tool", "content": "tool result"},
    ]) == [
        {"role": "user", "content": [{"type": "input_text", "text": "one\ntwo"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "answer"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "tool result"}]},
    ]


@pytest.mark.parametrize("role", ["assistant", "system", "tool"])
def test_subscription_rejects_unsupported_image_roles(role):
    with pytest.raises(HTTPException, match="only in user messages"):
        build_responses_input([{"role": role, "content": [
            {"type": "image_url", "image_url": {"url": IMAGE_ONE}},
        ]}])


@pytest.mark.parametrize("image", [
    {"url": "data:image/svg+xml;base64,AAAA"},
    {"url": "data:image/png;base64,"},
    {"url": "file:///private/image.png"},
    {"url": IMAGE_ONE, "detail": "invalid"},
    {},
])
def test_subscription_rejects_invalid_image_inputs_instead_of_dropping_them(image):
    with pytest.raises(HTTPException):
        build_responses_input([{"role": "user", "content": [
            {"type": "image_url", "image_url": image},
        ]}])
