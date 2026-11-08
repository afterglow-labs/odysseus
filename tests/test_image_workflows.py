"""Exercise GPU pinning, retry identity, owner boundaries, and image uploads."""
import io
import json
import signal
from types import SimpleNamespace
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from src import image_workflows as images
from routes.image_workflow_routes import setup_image_workflow_routes

ADA = "GPU-11111111-1111-1111-1111-111111111111"
BLACKWELL = "GPU-22222222-2222-2222-2222-222222222222"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    model_root = tmp_path / "models"
    for recipe in images.RECIPES.values():
        for name in recipe["components"].values():
            file = model_root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"x" * 2048)
    m = images.ImageJobManager(tmp_path / "jobs", roots=[model_root], defaults_file=tmp_path / "defaults.json", execution_lock=tmp_path / "lease")
    monkeypatch.setattr(images, "gpu_telemetry", lambda: {"gpus": [{"id": BLACKWELL, "name": "RTX 5090"}, {"id": ADA, "name": "RTX 4090"}]})
    monkeypatch.setattr(images, "_identity", lambda pid: {"pid": pid, "start": "test-process"})
    calls = []
    def launch(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(pid=123456)
    monkeypatch.setattr(images.subprocess, "Popen", launch)
    m.calls = calls
    return m


def submit(manager, request_id=None, **changes):
    incoming = manager.root.parent / ("input-" + uuid.uuid4().hex)
    incoming.mkdir()
    return manager.submit("owner", {"recipe": "snofs_krea", "prompt": "A paper boat", **changes}, incoming, [], None, request_id or str(uuid.uuid4()))


def test_all_recipes_default_to_4090_and_pin_the_worker_environment(manager, monkeypatch):
    assert all(w["defaults"]["gpu"] == ADA for w in manager.inventory()["workflows"])
    job = submit(manager)
    assert job["config"]["gpu"] == ADA
    assert manager.calls[0][1]["env"]["CUDA_VISIBLE_DEVICES"] == ADA
    manager.defaults_file.write_text(json.dumps({"gpu": ADA}))
    monkeypatch.setattr(images, "gpu_telemetry", lambda: {"gpus": [{"id": BLACKWELL, "name": "RTX 5090"}]})
    with pytest.raises(ValueError, match="unavailable"):
        submit(manager)
    assert len(manager.calls) == 1


def test_retry_reuses_job_and_does_not_start_a_second_worker(manager):
    request_id = str(uuid.uuid4())
    first = submit(manager, request_id)
    second = submit(manager, request_id)
    assert first["id"] == second["id"]
    assert len(manager.calls) == 1
    with pytest.raises(FileNotFoundError):
        manager.state(first["id"], "another-owner")
    assert manager.jobs("another-owner") == []


def test_dead_pid_reuse_is_not_signalled_and_does_not_claim_completion(manager, monkeypatch):
    job = submit(manager)
    monkeypatch.setattr(images, "_identity", lambda pid: {"pid": pid, "start": "different-process"})
    monkeypatch.setattr(images.os, "kill", lambda *args: pytest.fail("Reused PID must never be signalled"))
    assert manager.stop(job["id"], "owner")["status"] == "failed"


def test_worker_spawn_failure_is_terminal(manager, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("Could not start worker")
    monkeypatch.setattr(images.subprocess, "Popen", fail)
    assert submit(manager)["status"] == "failed"


@pytest.fixture
def api(manager, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda name: name in {"owner", "another-owner"})
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-user", "owner")
        return await call_next(request)
    app.include_router(setup_image_workflow_routes(manager))
    return TestClient(app), manager


def png():
    stream = io.BytesIO()
    Image.new("RGB", (32, 32), "blue").save(stream, format="PNG")
    return stream.getvalue()


def test_api_requires_admin_and_validates_inpaint_mask_before_launch(api):
    client, manager = api
    assert client.get("/api/image-workflows/inventory", headers={"x-user": "reader"}).status_code == 403
    cfg = {"recipe": "lustify_inpaint", "prompt": "Replace the vase"}
    headers = {"X-Odysseus-Submission-Id": str(uuid.uuid4())}
    response = client.post("/api/image-workflows/jobs", data={"config": json.dumps(cfg)}, files={"images": ("source.png", png(), "image/png")}, headers=headers)
    assert response.status_code == 400 and "mask" in response.text
    assert not manager.calls
    response = client.post("/api/image-workflows/jobs", data={"config": json.dumps(cfg)}, files=[("images", ("source.png", png(), "image/png")), ("mask", ("mask.png", png(), "image/png"))], headers=headers)
    assert response.status_code == 200, response.text
    job_id = response.json()["id"]
    assert client.get(f"/api/image-workflows/jobs/{job_id}", headers={"x-user": "another-owner"}).status_code == 404
    assert client.post(f"/api/image-workflows/jobs/{job_id}/stop", headers={"x-user": "another-owner"}).status_code == 404
    assert not list(manager.root.glob(".upload-*"))


def test_api_rejects_nonimage_payload_and_arbitrary_model_paths(api):
    client, manager = api
    headers = {"X-Odysseus-Submission-Id": str(uuid.uuid4())}
    cfg = {"recipe": "qwen_rapid", "prompt": "A blue paper boat"}
    response = client.post("/api/image-workflows/jobs", data={"config": json.dumps(cfg)}, files={"images": ("fake.png", b"not a PNG", "image/png")}, headers=headers)
    assert response.status_code == 400 and not manager.calls
    cfg.update(mode="generate", checkpoint="/etc/passwd")
    response = client.post("/api/image-workflows/jobs", data={"config": json.dumps(cfg)}, headers=headers)
    assert response.status_code == 400 and not manager.calls
    assert not list(manager.root.glob(".upload-*"))
