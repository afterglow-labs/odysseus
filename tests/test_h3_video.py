import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import struct
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from src import h3_video as h3
from routes.h3_video_routes import setup_h3_video_routes


def weights(path, lora=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = json.dumps({("diffusion_model.test.lora_A.weight" if lora else "model.weight"):
                         {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}).encode()
    path.write_bytes(struct.pack("<Q", len(header)) + header + b"0000")
    return path


@pytest.fixture
def inventory(tmp_path):
    root = tmp_path / "models--test--MiniMax-H3" / "snapshots" / "revision"
    for name, lora in (("MiniMax_H3_FL2VA_int8.safetensors", False),
                       ("MiniMax_H3_Ref2VA_nvfp4.safetensors", False),
                       ("qwen3vl_32b_minimax_h3_nvfp4.safetensors", False),
                       ("minimax_h3_video_vae.safetensors", False),
                       ("minimax_h3_audio_vae.safetensors", False),
                       ("minimax_h3_head_swap.safetensors", True)):
        weights(root / name, lora)
    components = h3.discover_components([tmp_path])
    gpus = [{"id": "GPU-blackwell", "name": "RTX 5090", "nvfp4": True},
            {"id": "GPU-ada", "name": "RTX 4090", "nvfp4": False}]
    return {"components": components, "gpus": gpus, "runtime_ready": True,
            "runtime_error": "", "defaults": dict(h3.DEFAULTS)}


def config(inventory, mode="t2va"):
    result = {**h3.DEFAULTS, "gpu": "GPU-blackwell", "prompt": "A paper boat on a calm pond", "mode": mode}
    for role in ("model", "encoder", "video_vae", "audio_vae"):
        result[role] = next(item["id"] for item in inventory["components"] if item["role"] == role
                            and (role != "model" or item["variant"] == ("ref2va" if mode == "ref2va" else "fl2va")))
    return result


def test_inventory_discovers_nested_hub_and_linked_vae_without_gguf_or_remote_roots(tmp_path):
    project = tmp_path / "project"
    hub = project / "cache/huggingface/hub"
    weights(hub / "hub/models--test--MiniMax-H3/snapshots/r/vae/minimax_h3_audio_vae.safetensors")
    external = weights(tmp_path / "windows/minimax_h3_video_vae.safetensors")
    runtime = project / "runtimes/minimax-h3/models"
    runtime.mkdir(parents=True)
    (runtime / external.name).symlink_to(external)
    weights(hub / "models--test--MiniMax-H3/snapshots/r/MiniMax_H3_FL2VA.safetensors")
    weights(hub / "models--test--BFS/snapshots/r/h3/minimax_h3_head_swap.safetensors", True)
    (hub / "wrong_h3.gguf").write_bytes(b"GGUF")
    local = tmp_path / "extra"
    weights(local / "minimax_h3_LoRA.safetensors", True)
    remote = tmp_path / "remote"
    weights(remote / "minimax_h3_unused.safetensors", True)
    state = tmp_path / "cookbook.json"
    state.write_text(json.dumps({"env": {"servers": [
        {"host": "", "modelDirs": [str(local)]}, {"host": "user@remote", "modelDirs": [str(remote)]}]}}))
    roots = h3.cache_roots(project, state)
    assert local in roots and remote not in roots
    components = h3.discover_components(roots)
    assert {c["role"] for c in components} == {"model", "video_vae", "audio_vae", "lora"}
    assert len(components) == 5
    assert all(len(c["id"]) == 32 for c in components)


@pytest.mark.parametrize("change", [
    {"model": "/etc/passwd"}, {"encoder": {}}, {"mode": []}, {"sampler": {}},
    {"scheduler": []}, {"reference_size": []}, {"gpu": 0}, {"width": 961},
    {"width": 1920, "height": 1088}, {"frames": 125}, {"seed": True},
    {"lora_scale": float("nan")}, {"prompt": "x" * 16001}, {"worker_path": "/tmp/malicious.py"},
])
def test_invalid_config_fails_before_worker(inventory, change):
    with pytest.raises(ValueError):
        h3.validate_config({**config(inventory), **change}, inventory["components"], inventory["gpus"], {})


def test_component_roles_mode_nvfp4_and_reference_rules(inventory):
    base = config(inventory)
    with pytest.raises(ValueError, match="encoder"):
        h3.validate_config({**base, "encoder": base["audio_vae"]}, inventory["components"], inventory["gpus"], {})
    with pytest.raises(ValueError, match="Blackwell"):
        h3.validate_config({**base, "gpu": "GPU-ada"}, inventory["components"], inventory["gpus"], {})
    with pytest.raises(ValueError, match="REF2VA"):
        h3.validate_config({**base, "mode": "ref2va"}, inventory["components"], inventory["gpus"], {"reference_images": [1]})
    with pytest.raises(ValueError, match="image or video"):
        h3.validate_config(config(inventory, "ref2va"), inventory["components"], inventory["gpus"], {"reference_audio": [1]})
    last = h3.validate_config(config(inventory, "fl2va"), inventory["components"], inventory["gpus"], {"last_frame": [1]})
    assert Path(last["model"]).is_absolute()
    with pytest.raises(ValueError, match="Text mode"):
        h3.validate_config(base, inventory["components"], inventory["gpus"], {"first_frame": [1]})


def test_vae_gpu_selection_does_not_change_main_gpu_precision_requirement(inventory):
    base = config(inventory)
    selected = h3.validate_config({**base, "vae_gpu": "GPU-ada"}, inventory["components"], inventory["gpus"], {})
    assert selected["gpu"] == "GPU-blackwell" and selected["vae_gpu"] == "GPU-ada"
    same = h3.validate_config(base, inventory["components"], inventory["gpus"], {})
    assert same["vae_gpu"] == ""
    for bad in (None, [], {}, 0, "GPU-unavailable"):
        with pytest.raises(ValueError, match="VAEs"):
            h3.validate_config({**base, "vae_gpu": bad}, inventory["components"], inventory["gpus"], {})
    with pytest.raises(ValueError, match="Blackwell"):
        h3.validate_config({**base, "gpu": "GPU-ada", "vae_gpu": "GPU-blackwell"},
                          inventory["components"], inventory["gpus"], {})


@pytest.fixture
def api(tmp_path, monkeypatch, inventory):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = h3.H3JobManager(tmp_path / "jobs", gallery_directory=tmp_path / "gallery")
    monkeypatch.setattr(manager, "inventory", lambda: inventory)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {"corey", "other"})

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("x-user", "corey")
        return await call_next(request)

    app.include_router(setup_h3_video_routes(manager))
    return TestClient(app), manager, inventory


def test_routes_require_admin_and_owner_before_inventory_or_process_work(api):
    client, manager, inventory = api
    for method, path in (("get", "/inventory"), ("get", "/jobs"), ("post", "/jobs"),
                         ("delete", "/jobs/" + "a" * 32), ("get", "/jobs/" + "a" * 32 + "/video")):
        assert getattr(client, method)("/api/video/h3" + path, headers={"x-user": "reader"}).status_code == 403
    assert not manager.root.exists()


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(buffer, format="PNG")
    return buffer.getvalue()


def test_upload_validation_size_path_safety_and_last_frame_only(api, monkeypatch):
    client, manager, inv = api
    captured = {}

    def launch(directory, owner, cfg, uploads):
        captured.update(owner=owner, config=cfg, uploads=uploads, directory=directory)
        assert Path(uploads["last_frame"]).parent == directory
        assert ".." not in Path(uploads["last_frame"]).name
        return {"id": directory.name, "status": "queued"}

    monkeypatch.setattr(manager, "launch", launch)
    body = {"config": json.dumps(config(inv, "fl2va"))}
    response = client.post("/api/video/h3/jobs", data=body,
                           files={"last_frame": ("../../outside.png", png(), "image/png")})
    assert response.status_code == 201, response.text
    assert captured["owner"] == "corey"
    assert not (manager.root.parent / "outside.png").exists()
    response = client.post("/api/video/h3/jobs", data=body,
                           files={"last_frame": ("fake.png", b"not an image", "image/png")})
    assert response.status_code == 400
    response = client.post("/api/video/h3/jobs", data=body,
                           files={"last_frame": ("script.py", png(), "image/png")})
    assert response.status_code == 400
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", "16")
    response = client.post("/api/video/h3/jobs", data=body,
                           files={"last_frame": ("real.png", png(), "image/png")})
    assert response.status_code == 413
    assert not list(manager.root.glob("*/manifest.json"))


def poll(fn, predicate, seconds=10):
    deadline = time.monotonic() + seconds
    value = None
    while time.monotonic() < deadline:
        value = fn()
        if predicate(value):
            return value
        time.sleep(.05)
    raise AssertionError(f"Timed out: {value}")


def test_durable_success_gallery_ownership_and_video_response(api, tmp_path, monkeypatch):
    client, manager, inv = api
    database = tmp_path / "results.db"
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(database))
    worker = tmp_path / "stub_worker.py"
    worker.write_text('''import json,sys,time
from pathlib import Path
job=json.loads(Path(sys.argv[2]).read_text())
time.sleep(.2)
Path(job['output_path']).write_bytes(b'fixture-MP4')
Path(job['status_path']).write_text(json.dumps({'phase':'completed','step':20,'total_steps':20}))
''')
    manager.worker = worker
    original_prompt = '  A paper boat on a pond.\nA sign reads "こんにちは".  '
    draft = {**config(inv), "prompt": original_prompt}
    response = client.post("/api/video/h3/jobs", data={"config": json.dumps(draft)})
    assert response.status_code == 201, response.text
    job_id = response.json()["id"]
    assert response.json()["prompt"] == original_prompt
    draft["prompt"] = "A later edit must not change the launched job"
    # A fresh manager instance sees the same detached job after a web restart.
    restarted = h3.H3JobManager(manager.root, gallery_directory=manager.gallery_directory)
    finished = poll(lambda: restarted.view(job_id, "corey"), lambda job: job["status"] not in h3.ACTIVE)
    assert finished["status"] == "completed", finished
    assert finished["prompt"] == original_prompt
    assert client.get("/api/video/h3/jobs").json()["jobs"][0]["prompt"] == original_prompt
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT owner,filename,prompt,caption FROM gallery_images").fetchall()
    assert len(rows) == 1 and rows[0][0] == "corey" and rows[0][2] == original_prompt
    assert json.loads(rows[0][3])["seed"] == 42
    assert (manager.gallery_directory / rows[0][1]).read_bytes() == b"fixture-MP4"
    assert client.get(f"/api/video/h3/jobs/{job_id}/video").content == b"fixture-MP4"
    assert client.get(f"/api/video/h3/jobs/{job_id}/video", headers={"x-user": "other"}).status_code == 404
    assert client.delete(f"/api/video/h3/jobs/{job_id}", headers={"x-user": "other"}).status_code == 404
    other_jobs = client.get("/api/video/h3/jobs", headers={"x-user": "other"}).json()
    assert other_jobs["jobs"] == []
    assert other_jobs["queue"]["total_queued"] == other_jobs["queue"]["total_running"] == 0


def test_restart_cancel_kills_owned_detached_children_preserves_neighbor(api, tmp_path):
    client, manager, inv = api
    worker = tmp_path / "stubborn_worker.py"
    worker.write_text('''import json,os,signal,subprocess,sys,time
from pathlib import Path
if sys.argv[1]=='--job':
    root=Path(sys.argv[2]).parent;role='worker'
else:
    root=Path(sys.argv[2]);role=sys.argv[1];os.setsid()
for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,signal.SIG_IGN)
(root/(role+'.pid')).write_text(str(os.getpid()))
if role!='grandchild':subprocess.Popen([sys.executable,__file__,'child' if role=='worker' else 'grandchild',str(root)])
while True:time.sleep(.1)
''')
    manager.worker = worker
    neighbor = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
    job_id, queued_id, pids = None, None, []
    try:
        response = client.post("/api/video/h3/jobs", data={"config": json.dumps(config(inv))})
        assert response.status_code == 201, response.text
        job_id = response.json()["id"]
        directory = manager.directory(job_id)
        poll(lambda: list(directory.glob("*.pid")), lambda paths: len(paths) == 3)
        pids = [int(path.read_text()) for path in directory.glob("*.pid")]
        second = client.post("/api/video/h3/jobs", data={"config": json.dumps(config(inv))})
        assert second.status_code == 201
        queued_id = second.json()["id"]
        assert second.json()["status"] == "queued" and second.json()["queue_position"] == 1
        assert manager.cancel(queued_id, "corey")["status"] == "stopped"
        assert not list(manager.directory(queued_id).glob("*.pid"))
        restarted = h3.H3JobManager(manager.root)
        assert restarted.view(job_id, "corey")["status"] == "running"
        stopped = restarted.cancel(job_id, "corey")
        assert stopped["status"] == "stopped"
        assert not (set(pids) & set(h3._snapshot()))
        assert neighbor.poll() is None
        assert restarted.cancel(job_id, "corey")["status"] == "stopped"
    finally:
        if job_id:
            manager.cancel(job_id, "corey")
        if queued_id:
            manager.cancel(queued_id, "corey")
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        neighbor.terminate()
        neighbor.wait(timeout=5)


def test_lost_supervisor_does_not_claim_success_and_pid_reuse_not_killed(tmp_path, monkeypatch):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory = manager.stage()
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": "running",
                                         "created_at": time.time()-30, "supervisor": {"pid": 54321, "start": "old"}})
    h3._write(directory / "processes.json", {"54321": "old"})
    monkeypatch.setattr(h3, "_snapshot", lambda: {54321: (1, "new")})
    monkeypatch.setattr(h3.os, "kill", lambda *args: pytest.fail("Reused PID must not be signalled"))
    assert manager.view(directory.name, "corey")["status"] == "failed"
    assert h3.stop_workers(directory)


def test_stop_failure_remains_active_and_api_reports_unverified(api, monkeypatch):
    client, manager, _ = api
    directory = manager.stage()
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": "running",
                                         "created_at": time.time(), "supervisor": h3._identity(os.getpid())})
    monkeypatch.setattr(h3, "stop_workers", lambda *args, **kwargs: False)
    result = client.delete("/api/video/h3/jobs/" + directory.name)
    assert result.status_code == 409 and "not been confirmed" in result.json()["detail"]
    assert h3._read(directory / "state.json")["status"] == "running"


def test_stream_size_cap_cannot_be_bypassed_with_small_content_length(api, monkeypatch):
    client, manager, inv = api
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", "16")
    response = client.post("/api/video/h3/jobs", headers={"Content-Length": "0"},
                           data={"config": json.dumps(config(inv, "fl2va"))},
                           files={"first_frame": ("oversized.png", b"x" * 70000, "image/png")})
    assert response.status_code == 413
    assert not manager.root.exists()


def test_gpu_ids_are_uuids_not_driver_indices(monkeypatch):
    def run(args, **kwargs):
        assert "--query-gpu=uuid,name,compute_cap" in args
        return SimpleNamespace(returncode=0, stdout="GPU-5090, NVIDIA GeForce RTX 5090, 12.0\nGPU-4090, NVIDIA GeForce RTX 4090, 8.9\n")
    monkeypatch.setattr(h3.subprocess, "run", run)
    gpus = h3.gpu_inventory()
    assert gpus == [{"id": "GPU-5090", "name": "NVIDIA GeForce RTX 5090", "nvfp4": True},
                    {"id": "GPU-4090", "name": "NVIDIA GeForce RTX 4090", "nvfp4": False}]


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed", "stopped"])
def test_prompt_snapshot_survives_restart_for_every_job_status(tmp_path, monkeypatch, status):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory = manager.stage()
    prompt = 'head_swap: <Picture 1>\nThe subject says "Hello." 🌊'
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": status,
                                         "created_at": time.time(), "supervisor": {"pid": 54321, "start": "active"}})
    h3._write(directory / "manifest.json", {"config": {"prompt": prompt, "model": "/private/weights.safetensors"},
                                            "reference_images": ["/private/upload.png"], "worker_path": "/private/worker.py"})
    monkeypatch.setattr(h3, "_snapshot", lambda: {54321: (1, "active")})
    restarted = h3.H3JobManager(manager.root)
    viewed = restarted.view(directory.name, "corey")
    assert viewed["status"] == status and viewed["prompt"] == prompt
    assert restarted.jobs("corey")[0]["prompt"] == prompt
    assert "/private/" not in json.dumps(viewed)
    assert not {"config", "reference_images", "worker_path", "manifest"} & viewed.keys()


@pytest.mark.parametrize("manifest", [None, "not JSON", "[]", "{}", '{"config":null}',
                                     '{"config":[]}', '{"config":{"prompt":7}}', '{"config":{"prompt":null}}'])
def test_legacy_or_malformed_manifest_leaves_job_visible_without_prompt(tmp_path, manifest):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory = manager.stage()
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": "failed", "created_at": time.time()})
    if manifest is not None:
        (directory / "manifest.json").write_text(manifest)
    viewed = manager.view(directory.name, "corey")
    assert viewed["status"] == "failed" and viewed["prompt"] is None
    assert manager.jobs("corey")[0]["prompt"] is None


def test_prompt_manifest_not_read_until_owner_is_verified(tmp_path, monkeypatch):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory = manager.stage()
    h3._write(directory / "state.json", {"id": directory.name, "owner": "corey", "status": "completed", "created_at": time.time()})
    h3._write(directory / "manifest.json", {"config": {"prompt": "Private prompt"}})
    original_read = h3._read

    def read(path, default=None):
        assert Path(path).name != "manifest.json", "Another owner's prompt must not even be read"
        return original_read(path, default)

    monkeypatch.setattr(h3, "_read", read)
    with pytest.raises(FileNotFoundError):
        manager.view(directory.name, "other")
    assert manager.jobs("other") == []
