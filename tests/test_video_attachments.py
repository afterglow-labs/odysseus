import base64
import json
from types import SimpleNamespace

import pytest

from src import chat_handler, chat_helpers, document_processor as dp
from src.attachment_refs import persistable_message_content, search_index_text
from src.video_input import is_video_file


@pytest.mark.parametrize("name,mime,expected", [
    ("clip.MP4", "application/octet-stream", True),
    ("extensionless", "video/quicktime", True),
    ("clip.webm", "video/webm", True),
    ("voice.webm", "audio/webm", False),
    ("voice.mp4", "audio/mp4", False),
    ("image.png", "image/png", False),
])
def test_video_detection_preserves_audio_recordings(name, mime, expected):
    assert is_video_file(name, mime) is expected


@pytest.mark.parametrize("caps,expected", [
    ({"vision": True, "video": True}, True),
    ({"vision": True, "video": False}, False),
    ({"vision": True}, None),
])
def test_video_capability_is_separate_from_image_support(monkeypatch, caps, expected):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(is_success=True, json=lambda: {"modalities": caps})
    monkeypatch.setattr(chat_helpers.httpx, "get", get)
    assert chat_helpers.llamacpp_supports_video("http://localhost:8001/v1", {"Authorization": "Bearer test"}) is expected
    assert calls == [("http://localhost:8001/props", {"timeout": 1.0, "headers": {"Authorization": "Bearer test"}})]


def _uploads(tmp_path, *, authorized=True, inside=True):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video-test-fixture")
    info = {"id": "clip", "name": "clip.mp4", "mime": "video/mp4", "path": str(path)}
    owners = []
    def resolve(fid, owner=None):
        owners.append(owner)
        return info if authorized else None
    uploads = SimpleNamespace(resolve_upload=resolve, _inside_upload_dir=lambda path: inside,
                              is_image_file=lambda *a: False, is_audio_file=lambda *a: False,
                              is_document_file=lambda *a: False)
    return uploads, info, owners


@pytest.mark.asyncio
async def test_preprocess_sends_video_bytes_to_native_model(monkeypatch, tmp_path):
    uploads, info, owners = _uploads(tmp_path)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    monkeypatch.setattr(chat_handler, "model_supports_vision", lambda *a, **kw: True)
    monkeypatch.setattr(chat_handler, "llamacpp_supports_video", lambda *a, **kw: True)
    monkeypatch.setattr(chat_handler, "analyze_video_with_vl_result", lambda *a, **kw: pytest.fail("unnecessary fallback"))
    handler = chat_handler.ChatHandler(None, None, None, None, None, uploads)
    session = SimpleNamespace(model="qwen-local", endpoint_url="http://localhost:8001/v1", owner="alice", id="test")
    enhanced, content, _, _, metadata = await handler.preprocess_message("What happens?", ["clip"], session)
    part = next(p for p in content if p["type"] == "input_video")
    assert base64.b64decode(part["input_video"]["data"]) == b"video-test-fixture"
    assert owners == ["alice"]
    assert "Video attached: clip.mp4" in enhanced
    assert metadata[0]["vision_model"] == "qwen-local"
    assert "non-text file" not in str(content)


@pytest.mark.parametrize("authorized,inside", [(False, True), (True, False)])
def test_video_read_requires_authorized_upload_and_contained_path(monkeypatch, tmp_path, authorized, inside):
    uploads, _, owners = _uploads(tmp_path, authorized=authorized, inside=inside)
    monkeypatch.setattr(dp, "video_content_part", lambda *a: pytest.fail("must not read these bytes"))
    assert dp.build_user_content("hello", ["clip"], str(tmp_path), uploads, owner="alice", native_video=True) == "hello"
    assert owners == ["alice"]


@pytest.mark.asyncio
async def test_image_only_chat_uses_configured_video_model(monkeypatch, tmp_path):
    uploads, _, _ = _uploads(tmp_path)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: default)
    monkeypatch.setattr(chat_handler, "model_supports_vision", lambda *a, **kw: True)
    monkeypatch.setattr(chat_handler, "llamacpp_supports_video", lambda *a, **kw: False)
    seen = []
    def analyze(path, owner=None, prompt=""):
        seen.append((owner, prompt))
        return {"text": "A person walks through a doorway.", "model": "video-model"}
    monkeypatch.setattr(chat_handler, "analyze_video_with_vl_result", analyze)
    handler = chat_handler.ChatHandler(None, None, None, None, None, uploads)
    session = SimpleNamespace(model="image-only", endpoint_url="http://localhost:8001/v1", owner="alice", id="test")
    _, content, _, _, metadata = await handler.preprocess_message("What happens?", ["clip"], session)
    assert isinstance(content, str) and "A person walks through a doorway." in content
    assert seen == [("alice", "What happens?")]
    assert metadata[0]["vision_model"] == "video-model"


@pytest.mark.asyncio
async def test_disabled_vision_does_not_decode_or_send_video(monkeypatch, tmp_path):
    uploads, _, _ = _uploads(tmp_path)
    monkeypatch.setattr("src.settings.get_setting", lambda key, default=None: False if key == "vision_enabled" else default)
    monkeypatch.setattr(chat_handler, "llamacpp_supports_video", lambda *a, **kw: pytest.fail("must not probe"))
    monkeypatch.setattr(dp, "video_content_part", lambda *a: pytest.fail("must not decode"))
    handler = chat_handler.ChatHandler(None, None, None, None, None, uploads)
    session = SimpleNamespace(model="qwen", endpoint_url="http://localhost:8001/v1", owner="alice", id="test")
    _, content, _, _, _ = await handler.preprocess_message("What happens?", ["clip"], session)
    assert isinstance(content, str) and "Vision is disabled" in content


def test_video_fallback_preserves_owner_and_saved_generation_controls(monkeypatch, tmp_path):
    import src.endpoint_resolver as endpoints
    import src.model_generation as generation
    _, info, _ = _uploads(tmp_path)
    seen = {}
    monkeypatch.setattr(dp, "_load_vl_settings", lambda: {"vision_model": "configured"})
    def resolve(configured, owner=None):
        seen["owner"] = owner
        return ("http://localhost:8001/v1/chat/completions", "qwen", {"X-Test": "auth"})
    monkeypatch.setattr(dp, "_resolve_vl_model", resolve)
    monkeypatch.setattr(endpoints, "resolve_vision_fallback_candidates", lambda owner=None: [])
    monkeypatch.setattr(chat_helpers, "llamacpp_supports_video", lambda *a, **kw: True)
    def capture(session, owner):
        assert owner == "alice" and session.model == "qwen"
        return {"thinking": "off"}
    monkeypatch.setattr(generation, "capture_generation_options", capture)
    def llm(url, model, messages, **kwargs):
        seen["call"] = kwargs
        assert messages[0]["content"][0]["text"] == "Describe the movement."
        assert messages[0]["content"][1]["type"] == "input_video"
        return "A ball moves left."
    monkeypatch.setattr(dp, "llm_call", llm)
    assert dp.analyze_video_with_vl_result(info["path"], owner="alice", prompt="Describe the movement.")["text"] == "A ball moves left."
    assert seen["owner"] == "alice"
    assert seen["call"]["generation_options"] == {"thinking": "off"}
    assert seen["call"]["headers"] == {"X-Test": "auth"}


def test_video_history_and_search_keep_references_without_raw_base64():
    from core.session_manager import _parse_msg_content
    content = [{"type": "text", "text": "Describe this clip."},
               {"type": "input_video", "input_video": {"data": "X" * 4096}}]
    assert _parse_msg_content(json.dumps(content)) == content
    stored = persistable_message_content(content, {"attachments": [{"id": "clip", "name": "clip.mp4", "mime": "video/mp4"}]})
    assert "Attachment: clip.mp4" in stored and "inline media payload omitted" in stored
    assert "X" * 100 not in stored
    assert "X" * 100 not in search_index_text(json.dumps(content))
