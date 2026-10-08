import asyncio
import base64
import io
import json
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import ModelEndpoint
from routes import h3_video_routes as routes
from src import endpoint_resolver
from src import h3_prompt_enhancement as enhancement


LOCAL_MODEL = "/cache/models--Qwen3.8-27B/snapshots/rev/Qwen3.8-27B-Uncensored-Q4_K_M.gguf"
REWRITE = "[Shot 1] A paper boat drifts across a still pond at dawn. The camera gently tracks its ripples. Soft water sounds accompany the continuous shot."


@pytest.fixture
def endpoints(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'endpoints.db'}", connect_args={"check_same_thread": False})
    ModelEndpoint.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(enhancement, "SessionLocal", sessions)
    monkeypatch.setattr(endpoint_resolver, "SessionLocal", sessions)
    resolved = []

    def runtime(endpoint, owner):
        resolved.append((endpoint.id, owner))
        return endpoint.base_url, "test-only-secret"

    monkeypatch.setattr(endpoint_resolver, "resolve_endpoint_runtime", runtime)

    def add(identity, *, owner="corey", models=None, base="http://localhost:8001/v1", **fields):
        values = dict(id=identity, owner=owner, name=identity, base_url=base, is_enabled=True,
                      endpoint_kind="local", model_type="llm", cached_models=json.dumps(models or [LOCAL_MODEL]))
        values.update(fields)
        with sessions() as db:
            db.add(ModelEndpoint(**values))
            db.commit()

    yield SimpleNamespace(add=add, sessions=sessions, resolved=resolved)
    engine.dispose()


@pytest.fixture
def upstream(monkeypatch):
    seen = []
    state = {"models": [LOCAL_MODEL], "status": 200}

    def handler(request):
        seen.append(request)
        assert request.method == "GET", "Availability must not run inference"
        assert request.headers["Authorization"] == "Bearer test-only-secret"
        result = {"data": [{"id": model} for model in state["models"]]}
        if "capabilities" in state:
            result["models"] = [{"model": model, "capabilities": state["capabilities"]} for model in state["models"]]
        return httpx.Response(state["status"], json=result)

    monkeypatch.setattr(enhancement, "_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False))
    return SimpleNamespace(seen=seen, state=state)


@pytest.fixture
def inference(monkeypatch):
    seen = []
    state = {"text": REWRITE, "error": None}

    async def call(url, model, messages, **kwargs):
        seen.append({"url": url, "model": model, "messages": messages, **kwargs})
        if state["error"]:
            raise state["error"]
        return state["text"], model

    monkeypatch.setattr(enhancement, "llm_call_async", call)
    return SimpleNamespace(seen=seen, state=state)


@pytest.fixture
def api(monkeypatch, endpoints, upstream, inference, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {"corey", "other"})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-user", "corey")
        return await call_next(request)

    def no_jobs(*args, **kwargs):
        pytest.fail("Manual prompt enhancement must not create or alter a generation job")

    app.include_router(routes.setup_h3_video_routes(SimpleNamespace(
        launch=no_jobs, stage=no_jobs, jobs=no_jobs, root=tmp_path / "jobs", project=tmp_path)))
    with TestClient(app) as client:
        yield client


def payload(**changes):
    return {"prompt": "A paper boat on a pond", "endpoint_id": "local", "model": LOCAL_MODEL, **changes}


def test_picker_owner_shared_disabled_hidden_and_chat_filters(api, endpoints, upstream, inference):
    endpoints.add("local", owner=None)
    endpoints.add("other-owner", owner="other")
    endpoints.add("disabled", is_enabled=False)
    endpoints.add("image", model_type="image")
    endpoints.add("hidden", hidden_models=json.dumps([LOCAL_MODEL]))
    endpoints.add("embeddings", models=["text-embedding-3-large"])
    endpoints.add("cloud", base="https://api.openai.com/v1", endpoint_kind="api",
                  models=["gpt-5", "gpt-5-mini"], pinned_models='["gpt-5-mini"]')
    endpoints.add("unselected", base="https://api.openai.com/v1", endpoint_kind="api", pinned_models="[]")
    response = api.get("/api/video/h3/prompt-enhancer")
    assert response.status_code == 200
    result = response.json()
    assert {(model["endpoint_id"], model["model"]) for model in result["models"]} == {
        ("local", LOCAL_MODEL), ("cloud", "gpt-5-mini")}
    assert result["default"] == {"endpoint_id": "local", "model": LOCAL_MODEL}
    assert result["available"] is True
    assert "test-only-secret" not in response.text
    assert len(result["preference_scope"]) == 24
    other_scope = api.get("/api/video/h3/prompt-enhancer", headers={"x-user": "other"}).json()["preference_scope"]
    assert other_scope != result["preference_scope"]
    assert not inference.seen
    assert all(request.url.host == "localhost" for request in upstream.seen)


def test_unavailable_local_does_not_choose_cloud_default(api, endpoints, upstream):
    endpoints.add("local", owner=None)
    endpoints.add("subscription", base="https://chatgpt.com/backend-api/codex", endpoint_kind="api", models=["gpt-6.1-sol"])
    upstream.state["status"] = 401
    result = api.get("/api/video/h3/prompt-enhancer").json()
    assert result["available"] is True and len(result["models"]) == 2
    assert result["default"] is None
    assert [identity for identity, owner in endpoints.resolved] == ["local"]


def test_empty_picker_is_actionable_and_does_not_resolve_defaults(api, endpoints, upstream):
    result = api.get("/api/video/h3/prompt-enhancer").json()
    assert result["available"] is False and result["models"] == [] and result["default"] is None
    assert "Model Endpoints" in result["reason"]
    assert not endpoints.resolved and not upstream.seen


@pytest.mark.parametrize("user", ["reader", ""])
def test_admin_gate_before_discovery_or_inference(api, endpoints, upstream, inference, user):
    endpoints.add("local")
    assert api.get("/api/video/h3/prompt-enhancer", headers={"x-user": user}).status_code == 403
    assert api.post("/api/video/h3/enhance-prompt", headers={"x-user": user}, json=payload()).status_code == 403
    assert not endpoints.resolved and not upstream.seen and not inference.seen


def test_selected_local_exact_route_auth_strict_final_and_no_jobs(api, endpoints, upstream, inference):
    endpoints.add("local", owner=None)
    result = api.post("/api/video/h3/enhance-prompt", json=payload()).json()
    assert result == {"prompt": REWRITE, "model": LOCAL_MODEL}
    assert len(inference.seen) == 1
    call = inference.seen[0]
    assert call["url"] == "http://localhost:8001/v1/chat/completions"
    assert call["model"] == LOCAL_MODEL
    assert call["headers"]["Authorization"] == "Bearer test-only-secret"
    assert call["strict_final_only"] is True and call["max_retries"] == 1
    assert call["generation_options"] == {"thinking": "off", "reasoning_effort": "none"}
    context = json.loads(call["messages"][1]["content"])
    assert context["duration_seconds"] == pytest.approx(124 / 24, abs=0.001)
    assert "endpoint_id" not in context and "model" not in context
    assert "TEXT AND COUNTS ONLY" in call["messages"][0]["content"]


def test_explicit_subscription_selection_uses_generic_transport_only(api, endpoints, upstream, inference):
    endpoints.add("local")
    endpoints.add("subscription", base="https://chatgpt.com/backend-api/codex", endpoint_kind="api", models=["gpt-6.1-sol"])
    response = api.post("/api/video/h3/enhance-prompt", json=payload(endpoint_id="subscription", model="gpt-6.1-sol"))
    assert response.status_code == 200
    call = inference.seen[0]
    assert call["url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert call["model"] == "gpt-6.1-sol" and "reasoning_effort" not in call
    assert not upstream.seen


@pytest.mark.parametrize("changes", [
    {"endpoint_id": "other"}, {"model": "unregistered"}, {"endpoint_id": []}, {"model": {}},
    {"endpoint_id": ""}, {"model": ""}, {"url": "https://attacker.invalid"},
    {"prompt": ""}, {"prompt": "x" * 16001}, {"mode": []}, {"frames": 125},
    {"frames": True}, {"width": 257}, {"width": 1920, "height": 1088},
    {"reference_counts": []}, {"reference_counts": {"reference_images": True}},
    {"reference_counts": {"other": 1}}, {"reference_counts": {"reference_images": 1}},
    {"mode": "fl2va", "reference_counts": {"reference_videos": 1}},
    {"mode": "ref2va", "reference_counts": {"first_frame": 1}},
    {"mode": "ref2va", "reference_counts": {"reference_images": 10}},
    {"mode": "ref2va", "reference_counts": {"reference_images": 9, "reference_videos": 3, "reference_audio": 1}},
])
def test_invalid_input_or_unowned_selection_never_runs_inference(api, endpoints, upstream, inference, changes):
    endpoints.add("local")
    endpoints.add("other", owner="other")
    response = api.post("/api/video/h3/enhance-prompt", json=payload(**changes))
    assert response.status_code == 400
    assert not upstream.seen and not inference.seen


def test_local_model_stopped_or_changed_never_falls_back(api, endpoints, upstream, inference):
    endpoints.add("local")
    endpoints.add("cloud", base="https://api.openai.com/v1", endpoint_kind="api", models=["gpt-5-mini"])
    upstream.state["models"] = ["another-loaded-model"]
    response = api.post("/api/video/h3/enhance-prompt", json=payload())
    assert response.status_code == 503
    assert "endpoint is reachable" in response.json()["detail"]
    assert "not reachable" not in response.json()["detail"]
    assert not inference.seen
    assert [identity for identity, owner in endpoints.resolved] == ["local"]


@pytest.mark.parametrize("root", ["/mnt/e/AI/huggingface/hub", "C:\\Users\\Corey\\.cache\\huggingface\\hub"])
def test_repository_selection_resolves_exact_advertised_gguf_path(api, endpoints, upstream, inference, root):
    repo = "Example/Qwen3.8-27B-GGUF"
    actual = f"{root}/models--Example--Qwen3.8-27B-GGUF/snapshots/rev/model-Q4_K_M.gguf"
    if root.startswith("C:"):
        actual = actual.replace("/", "\\")
    endpoints.add("local", models=[repo])
    upstream.state["models"] = [actual]

    status = api.get("/api/video/h3/prompt-enhancer").json()
    assert status["default"] == {"endpoint_id": "local", "model": repo}
    assert not inference.seen
    response = api.post("/api/video/h3/enhance-prompt", json=payload(model=repo))
    assert response.status_code == 200
    assert response.json() == {"prompt": REWRITE, "model": repo}
    assert inference.seen[0]["model"] == actual
    assert inference.seen[0]["url"] == "http://localhost:8001/v1/chat/completions"


@pytest.mark.parametrize("advertised", [
    ["/cache/models--Other--Qwen3.8-27B-GGUF/snapshots/rev/model-Q4_K_M.gguf"],
    ["/cache/models--Example--Qwen3.8-27B-GGUF-extra/snapshots/rev/model-Q4_K_M.gguf"],
    ["/cache/models--Example--Qwen3.8-27B-GGUF/snapshots/rev/model-Q4_K_M.gguf",
     "/cache/models--Example--Qwen3.8-27B-GGUF/snapshots/rev/model-Q8_0.gguf"],
])
def test_repository_alias_never_guesses_another_or_ambiguous_model(api, endpoints, upstream, inference, advertised):
    repo = "Example/Qwen3.8-27B-GGUF"
    endpoints.add("local", models=[repo])
    upstream.state["models"] = advertised
    response = api.post("/api/video/h3/enhance-prompt", json=payload(model=repo))
    assert response.status_code == 503
    assert "endpoint is reachable" in response.json()["detail"]
    assert not inference.seen


def test_explicit_gguf_selection_does_not_switch_quant_within_same_repository(api, endpoints, upstream, inference):
    selected = "/cache/models--Example--Qwen3.8-27B-GGUF/snapshots/rev/model-Q4_K_M.gguf"
    endpoints.add("local", models=[selected])
    upstream.state["models"] = [selected.replace("Q4_K_M", "Q8_0")]
    response = api.post("/api/video/h3/enhance-prompt", json=payload(model=selected))
    assert response.status_code == 503 and not inference.seen


def test_repository_alias_checks_loaded_models_image_capability(api, endpoints, upstream, inference):
    repo = "Example/Qwen3.8-27B-GGUF"
    endpoints.add("local", models=[repo])
    upstream.state["models"] = ["/cache/models--Example--Qwen3.8-27B-GGUF/snapshots/rev/model-Q4_K_M.gguf"]
    upstream.state["capabilities"] = ["completion"]
    response = image_request(api, payload(model=repo, mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", image_bytes(), "image/png"))])
    assert response.status_code == 400 and "vision-capable" in response.json()["detail"]
    assert not inference.seen

    upstream.state["capabilities"] = ["completion", "vision"]
    response = image_request(api, payload(model=repo, mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", image_bytes(), "image/png"))])
    assert response.status_code == 200
    assert inference.seen[0]["model"] == upstream.state["models"][0]
    assert any(part.get("type") == "image_url" for part in inference.seen[0]["messages"][1]["content"])


def test_local_probe_failure_remains_connectivity_error(api, endpoints, upstream, inference):
    endpoints.add("local")
    upstream.state["status"] = 503
    response = api.post("/api/video/h3/enhance-prompt", json=payload())
    assert response.status_code == 503
    assert "not reachable" in response.json()["detail"]
    assert not inference.seen


def test_reference_context_last_frame_only_and_exact_dialogue(api, endpoints, inference):
    endpoints.add("local")
    original = 'The subject in <Picture 1> says "Wait here." End on the uploaded last frame.'
    inference.state["text"] = original + " The camera holds a steady medium shot while the water ripples softly."
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        prompt=original, mode="fl2va", reference_counts={"last_frame": 1}))
    assert response.status_code == 200
    context = json.loads(inference.seen[0]["messages"][1]["content"])
    assert context["reference_counts"]["last_frame"] == 1
    assert context["reference_counts"]["first_frame"] == 0
    assert "only\nlast-frame image is Picture 1" in inference.seen[0]["messages"][0]["content"]


@pytest.mark.parametrize("text", [None, "", "<think>Reason first.</think>A pond.", "Analysis:\nA pond.",
                                      "Let me think about this prompt.", '{"prompt":"A pond"}', '["A pond"]',
                                      "```A pond```", "x" * 16001, "The subject in <Picture 9> waves."])
def test_reasoning_wrappers_and_invented_references_preserve_draft(api, endpoints, inference, text):
    endpoints.add("local")
    inference.state["text"] = text
    response = api.post("/api/video/h3/enhance-prompt", json=payload())
    assert response.status_code == 502 and "preserved" in response.json()["detail"]


@pytest.mark.parametrize("rewrite", ["A subject says \"Wait here.\"", "The subject in <Picture 1> says \"Go away.\""])
def test_reference_markers_and_dialogue_cannot_be_lost(api, endpoints, inference, rewrite):
    endpoints.add("local")
    inference.state["text"] = rewrite
    response = api.post("/api/video/h3/enhance-prompt", json=payload(
        prompt='The subject in <Picture 1> says "Wait here."', mode="ref2va", reference_counts={"reference_images": 1}))
    assert response.status_code == 502


@pytest.mark.parametrize("error, expected", [(HTTPException(401, "test-only-secret"), 502),
                                              (HTTPException(502, "raw provider reasoning"), 502),
                                              (HTTPException(504, "raw URL"), 504),
                                              (httpx.ConnectError("test-only-secret"), 503)])
def test_upstream_errors_are_sanitized_and_never_fallback(api, endpoints, inference, error, expected):
    endpoints.add("local")
    endpoints.add("cloud", base="https://api.openai.com/v1", endpoint_kind="api", models=["gpt-5-mini"])
    inference.state["error"] = error
    response = api.post("/api/video/h3/enhance-prompt", json=payload())
    assert response.status_code == expected
    assert "test-only-secret" not in response.text and "raw" not in response.text
    assert len(inference.seen) == 1


def test_body_bound_and_invalid_json_before_model_access(api, inference):
    assert api.post("/api/video/h3/enhance-prompt", content=b"x" * (96 * 1024 + 1)).status_code == 413
    assert api.post("/api/video/h3/enhance-prompt", content="{invalid").status_code == 400
    assert not inference.seen


def test_browser_disconnect_cancels_in_flight_model_call(monkeypatch):
    async def scenario():
        started, cancelled, disconnect = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def rewrite(owner, config, images=None):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        monkeypatch.setattr(routes, "enhance_prompt", rewrite)
        task = asyncio.create_task(routes._enhance_until_disconnected(SimpleNamespace(receive=receive), "corey", payload()))
        await asyncio.wait_for(started.wait(), 1)
        disconnect.set()
        with pytest.raises(HTTPException) as raised:
            await asyncio.wait_for(task, 1)
        assert raised.value.status_code == 499
        assert cancelled.is_set()

    asyncio.run(scenario())


def image_bytes(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(output, format=format)
    return output.getvalue()


def image_request(client, config, files, **kwargs):
    return client.post("/api/video/h3/enhance-prompt", data={"config": json.dumps(config)}, files=files, **kwargs)


@pytest.mark.parametrize("fields, markers", [
    (["first_frame"], ["<Picture 1>"]),
    (["last_frame"], ["<Picture 1>"]),
    (["first_frame", "last_frame"], ["<Picture 1>", "<Picture 2>"]),
])
def test_actual_keyframes_get_ordered_markers_roles_and_original_pixels(api, endpoints, upstream, inference, fields, markers):
    endpoints.add("local")
    upstream.state["capabilities"] = ["completion", "multimodal"]
    files = [(field, (field + ".png", image_bytes(color), "image/png"))
             for field, color in zip(fields, ("red", "blue"))]
    response = image_request(api, payload(mode="fl2va", reference_counts={field: 1 for field in fields}), files)
    assert response.status_code == 200, response.text
    call = inference.seen[0]
    content = call["messages"][1]["content"]
    context = json.loads(content[0]["text"])
    assert context["observed_images"] == [{"image_index": index + 1, "marker": marker, "role": field}
                                           for index, (field, marker) in enumerate(zip(fields, markers))]
    assert "TEXT AND COUNTS ONLY" not in call["messages"][0]["content"]
    assert call["messages"][0]["content"].startswith("You enhance the user's draft for a MiniMax H3 VIDEO GENERATION render")
    assert "Respect requested transformations" in call["messages"][0]["content"]
    for index, (field, marker) in enumerate(zip(fields, markers)):
        assert f"Attached image {index + 1}: {marker}" in content[1 + index * 2]["text"]
        assert ("FIRST FRAME" if field == "first_frame" else "LAST FRAME") in content[1 + index * 2]["text"]
        uri = content[2 + index * 2]["image_url"]["url"]
        assert uri.startswith("data:image/png;base64,")
        assert base64.b64decode(uri.split(",", 1)[1]) == files[index][1][1]
    assert call["strict_final_only"] is True and call["generation_options"]["thinking"] == "off"


def test_reference_images_preserve_upload_order_and_video_audio_remain_unobserved(api, endpoints, inference):
    endpoints.add("local")
    files = [("reference_images", ("one.jpeg", image_bytes("red", "JPEG"), "image/jpeg")),
             ("reference_images", ("two.webp", image_bytes("blue", "WEBP"), "image/webp"))]
    response = image_request(api, payload(mode="ref2va", reference_counts={"reference_images": 2,
                             "reference_videos": 1, "reference_audio": 1}), files)
    assert response.status_code == 200
    content = inference.seen[0]["messages"][1]["content"]
    context = json.loads(content[0]["text"])
    assert context["reference_counts"]["reference_videos"] == context["reference_counts"]["reference_audio"] == 1
    assert [image["marker"] for image in context["observed_images"]] == ["<Picture 1>", "<Picture 2>"]
    assert all(image["role"] == "reference_images" for image in context["observed_images"])
    assert "REFERENCE IMAGE" in content[1]["text"] and "not a mandatory first or last frame" in content[3]["text"]
    assert content[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert content[4]["image_url"]["url"].startswith("data:image/webp;base64,")
    assert "Video and audio contents are NOT supplied" in inference.seen[0]["messages"][0]["content"]


def test_legacy_json_reference_counts_do_not_claim_images_were_seen(api, endpoints, inference):
    endpoints.add("local")
    response = api.post("/api/video/h3/enhance-prompt", json=payload(mode="ref2va", reference_counts={"reference_images": 1}))
    assert response.status_code == 200
    call = inference.seen[0]
    assert isinstance(call["messages"][1]["content"], str)
    assert json.loads(call["messages"][1]["content"])["observed_images"] == []
    assert "TEXT AND COUNTS ONLY" in call["messages"][0]["content"]


@pytest.mark.parametrize("config, fields", [
    (payload(), ["first_frame"]),
    (payload(mode="fl2va", reference_counts={"first_frame": 1}), ["last_frame"]),
    (payload(mode="fl2va", reference_counts={"first_frame": 1}), ["first_frame", "first_frame"]),
    (payload(mode="fl2va", reference_counts={"first_frame": 1, "last_frame": 1}), ["first_frame"]),
    (payload(mode="ref2va", reference_counts={"reference_images": 1}), ["first_frame"]),
    (payload(mode="ref2va", reference_counts={"reference_images": 2}), ["reference_images"]),
    (payload(mode="ref2va", reference_counts={"reference_images": 1}), ["reference_images", "reference_videos"]),
])
def test_multipart_mode_counts_and_fields_cannot_silently_drop_images(api, endpoints, inference, upstream, config, fields):
    endpoints.add("local")
    response = image_request(api, config, [(field, ("test.png", image_bytes(), "image/png")) for field in fields])
    assert response.status_code == 400
    assert not inference.seen and not upstream.seen


@pytest.mark.parametrize("filename, data", [("empty.png", b""), ("fake.png", b"not an image"),
                                            ("wrong.jpeg", image_bytes()), ("image.bmp", image_bytes(format="BMP")),
                                            ("broken.jpg", image_bytes(format="JPEG")[:100])])
def test_images_must_have_valid_format_and_complete_pixels(api, endpoints, inference, filename, data):
    endpoints.add("local")
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", (filename, data, "application/octet-stream"))])
    assert response.status_code == 400 and not inference.seen


def test_oversized_pixels_and_animation_are_rejected_before_inference(api, endpoints, inference, monkeypatch):
    endpoints.add("local")
    monkeypatch.setattr(enhancement, "MAX_IMAGE_PIXELS", 100)
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("large.png", image_bytes(), "image/png"))])
    assert response.status_code == 400
    monkeypatch.setattr(enhancement, "MAX_IMAGE_PIXELS", 64 * 1024 * 1024)
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(output, format="PNG", save_all=True,
                                             append_images=[Image.new("RGB", (16, 12), "blue")], duration=100)
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("animated.png", output.getvalue(), "image/png"))])
    assert response.status_code == 400 and not inference.seen


def test_existing_configured_upload_limit_applies_to_combined_images(api, endpoints, inference, monkeypatch):
    endpoints.add("local")
    data = image_bytes()
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", str(len(data) + 1))
    response = image_request(api, payload(mode="fl2va", reference_counts={"first_frame": 1, "last_frame": 1}),
                             [(field, ("image.png", data, "image/png")) for field in ("first_frame", "last_frame")])
    assert response.status_code == 413 and not inference.seen
    assert isinstance(response.json()["detail"], str)
    assert response.json()["detail"] == f"Prompt images exceed {len(data) + 1} bytes per request"
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", data, "image/png"))])
    assert response.status_code == 200


def test_multipart_body_cap_holds_even_with_false_content_length(api, endpoints, inference, monkeypatch):
    endpoints.add("local")
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", "16")
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("oversized.png", b"x" * 100000, "image/png"))],
                             headers={"Content-Length": "0"})
    assert response.status_code == 413 and not inference.seen
    assert isinstance(response.json()["detail"], str)
    assert response.json()["detail"] == "Prompt images exceed 16 bytes per request"


def test_image_spools_are_closed_without_persisting_or_creating_jobs(api, endpoints, monkeypatch):
    endpoints.add("local")
    prepare = routes.prepare_prompt_images
    files_seen = []

    async def inspect(config, uploads, limit):
        files_seen.extend(upload.file for files in uploads.values() for upload in files)
        return await prepare(config, uploads, limit)

    monkeypatch.setattr(routes, "prepare_prompt_images", inspect)
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", image_bytes(), "image/png"))])
    assert response.status_code == 200
    assert files_seen and all(file.closed for file in files_seen)


def test_model_explicitly_without_vision_is_not_sent_images_or_replaced(api, endpoints, upstream, inference):
    endpoints.add("local")
    endpoints.add("cloud", base="https://api.openai.com/v1", endpoint_kind="api", models=["gpt-5-mini"])
    upstream.state["capabilities"] = ["completion"]
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", image_bytes(), "image/png"))])
    assert response.status_code == 400 and "vision-capable" in response.json()["detail"]
    assert not inference.seen and [identity for identity, owner in endpoints.resolved] == ["local"]


def test_provider_image_rejection_is_visible_without_text_only_retry(api, endpoints, inference):
    endpoints.add("local")
    inference.state["error"] = HTTPException(400, "image not supported raw secret")
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("image.png", image_bytes(), "image/png"))])
    assert response.status_code == 502 and "rejected the image request" in response.json()["detail"]
    assert "raw secret" not in response.text and len(inference.seen) == 1


def test_multipart_auth_is_checked_before_reading_image_content(api, endpoints, inference):
    endpoints.add("local")
    response = image_request(api, payload(mode="fl2va", reference_counts={"last_frame": 1}),
                             [("last_frame", ("fake.png", b"not an image", "image/png"))], headers={"x-user": "reader"})
    assert response.status_code == 403 and not inference.seen
