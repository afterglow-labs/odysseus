"""Read-only driver telemetry and safe terminal H3/BFS job deletion."""
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database
from routes import h3_video_routes
from routes.bfs_video_routes import setup_bfs_video_routes
from src import h3_video as h3
from src.bfs_video import BFSJobManager


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "gallery.db"), connect_args={"check_same_thread": False})
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    yield factory
    engine.dispose()


def job(manager, status="completed", owner="corey"):
    directory = manager.stage()
    state = {"id": directory.name, "owner": owner, "status": status, "phase": status,
             "filename": directory.name + ".mp4", "created_at": time.time(), "steps": 20}
    h3._write(directory / "state.json", state)
    h3._write(directory / "manifest.json", {"config": {"prompt": "Original video", "gpu": "GPU-main", "vae_gpu": "GPU-vae"}})
    (directory / "output.mp4").write_bytes(b"video")
    (directory / "input.png").write_bytes(b"source")
    (directory / "render.log").write_text("finished")
    return directory, state


def gallery(db, manager, state, owner="corey"):
    manager.gallery_directory.mkdir(parents=True, exist_ok=True)
    output = manager.gallery_directory / state["filename"]
    output.write_bytes(b"gallery-video")
    with db() as session:
        session.add(database.GalleryImage(id=state["id"], filename=state["filename"], owner=owner))
        session.add(database.GalleryAlbum(id=state["id"], name="Videos", owner=owner, cover_id=state["id"]))
        session.commit()
    return output


@pytest.mark.parametrize("manager_class", [h3.H3JobManager, BFSJobManager])
@pytest.mark.parametrize("status", ["completed", "failed", "stopped"])
def test_delete_terminal_files_and_gallery_bookkeeping(tmp_path, db, manager_class, status):
    manager = manager_class(tmp_path / "jobs", gallery_directory=tmp_path / "gallery")
    directory, state = job(manager, status)
    output = gallery(db, manager, state)
    neighbor, _ = job(manager, owner="other")
    external = tmp_path / "outside.txt"
    external.write_text("keep")
    (directory / "link").symlink_to(external)
    assert manager.delete(directory.name, "corey") == {"deleted": True, "id": directory.name}
    assert not directory.exists() and not output.exists()
    assert neighbor.exists() and external.read_text() == "keep"
    with db() as session:
        assert session.get(database.GalleryImage, state["id"]) is None
        assert session.get(database.GalleryAlbum, state["id"]).cover_id is None
    with pytest.raises(FileNotFoundError):
        manager.view(directory.name, "corey")


@pytest.mark.parametrize("status", ["queued", "running"])
def test_active_job_requires_stop_first(tmp_path, db, status):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory, _ = job(manager, status)
    with pytest.raises(RuntimeError, match="Stop"):
        manager.delete(directory.name, "corey")
    assert (directory / "output.mp4").read_bytes() == b"video"


@pytest.mark.parametrize("supervisor", [False, True])
def test_terminal_label_does_not_allow_deleting_live_owned_process(tmp_path, db, supervisor):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory, state = job(manager, "failed")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
        env=dict(os.environ, **{h3.MARKER: directory.name}), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        identity = h3._identity(process.pid)
        assert identity
        if supervisor:
            state["supervisor"] = identity
            h3._write(directory / "state.json", state)
        else:
            h3._write(directory / "processes.json", {str(process.pid): identity["start"]})
        with pytest.raises(RuntimeError, match="still exiting"):
            manager.delete(directory.name, "corey")
        assert process.poll() is None and directory.exists()
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_stale_pid_receipt_preserves_unrelated_process(tmp_path, db):
    manager = h3.H3JobManager(tmp_path / "jobs", gallery_directory=tmp_path / "gallery")
    directory, state = job(manager, "stopped")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        state["supervisor"] = {"pid": process.pid, "start": "not-this-process"}
        h3._write(directory / "state.json", state)
        h3._write(directory / "processes.json", {str(process.pid): "not-this-process"})
        manager.delete(directory.name, "corey")
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_owner_path_checks_precede_process_or_gallery_access(tmp_path, db, monkeypatch):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory, _ = job(manager)
    monkeypatch.setattr(h3, "_owned_workers", lambda *_: pytest.fail("Unauthorized process access"))
    monkeypatch.setattr(manager, "_delete_gallery", lambda *_: pytest.fail("Unauthorized Gallery access"))
    for identity, owner in ((directory.name, "other"), ("../outside", "corey"), ("a" * 31, "corey")):
        with pytest.raises(FileNotFoundError):
            manager.delete(identity, owner)
    linked = manager.root / ("b" * 32)
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        manager.delete(linked.name, "corey")
    assert directory.exists()


def test_gallery_owner_mismatch_preserves_everything(tmp_path, db):
    manager = h3.H3JobManager(tmp_path / "jobs", gallery_directory=tmp_path / "gallery")
    directory, state = job(manager)
    output = gallery(db, manager, state, owner="other")
    with pytest.raises(RuntimeError, match="owner"):
        manager.delete(directory.name, "corey")
    assert directory.exists() and output.exists()
    with db() as session:
        assert session.get(database.GalleryImage, state["id"]).owner == "other"


def test_manifest_gallery_path_ignored_and_file_failure_retryable(tmp_path, db, monkeypatch):
    manager = h3.H3JobManager(tmp_path / "jobs", gallery_directory=tmp_path / "gallery")
    directory, state = job(manager)
    actual = gallery(db, manager, state)
    outside = tmp_path / "other-gallery"
    outside.mkdir()
    unrelated = outside / state["filename"]
    unrelated.write_bytes(b"keep")
    h3._write(directory / "manifest.json", h3._read(directory / "manifest.json") | {"gallery_directory": str(outside)})
    original_unlink = Path.unlink
    def fail_log(path, *args, **kwargs):
        if path == directory / "render.log":
            raise PermissionError("temporarily locked")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_log)
    with pytest.raises(RuntimeError, match="retry Delete"):
        manager.delete(directory.name, "corey")
    assert (directory / "state.json").exists() and not actual.exists()
    assert unrelated.read_bytes() == b"keep"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    assert manager.delete(directory.name, "corey")["deleted"]
    assert unrelated.read_bytes() == b"keep"


def test_driver_telemetry_cached_and_unsupported_metrics_explicit(monkeypatch):
    now, calls = [10.], []
    monkeypatch.setattr(h3.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(h3, "_GPU_TELEMETRY_CACHE", {"expires": 0., "payload": None})
    def query(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="GPU-first, RTX 5090, 2048, 32768, 42\nGPU-second, RTX 4090, 8, 24564, [N/A]\n")
    monkeypatch.setattr(h3.subprocess, "run", query)
    first = h3.gpu_telemetry()
    assert first["gpus"][0]["memory_used_mib"] == 2048 and first["gpus"][0]["memory_total_mib"] == 32768
    assert first["gpus"][1]["utilization_percent"] is None
    first["gpus"][0]["memory_used_mib"] = -1
    now[0] += 1
    assert h3.gpu_telemetry()["gpus"][0]["memory_used_mib"] == 2048 and len(calls) == 1
    now[0] += 2
    h3.gpu_telemetry()
    assert len(calls) == 2 and calls[0][1]["timeout"] == 2
    assert calls[0][0][0] == "nvidia-smi" and "shell" not in calls[0][1]


@pytest.mark.parametrize("failure", ["timeout", "missing", "exit", "malformed"])
def test_telemetry_failure_is_explicit_without_driver_error_leak(monkeypatch, failure):
    monkeypatch.setattr(h3, "_GPU_TELEMETRY_CACHE", {"expires": 0., "payload": None})
    def query(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("private command", 2)
        if failure == "missing":
            raise FileNotFoundError("private driver path")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout="bad output", stderr="private driver error")
    monkeypatch.setattr(h3.subprocess, "run", query)
    value = h3.gpu_telemetry()
    assert value["gpus"] == [] and value["error"] and "private" not in str(value)


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_routes_owner_admin_active_guards_and_existing_cancel(tmp_path, db, monkeypatch, family):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = (h3.H3JobManager if family == "h3" else BFSJobManager)(tmp_path / family, gallery_directory=tmp_path / "gallery")
    directory, _ = job(manager)
    active, _ = job(manager, "queued")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True, is_admin=lambda user: user in {"corey", "other"})
    @app.middleware("http")
    async def identify(request, call_next):
        request.state.current_user = request.headers.get("x-user", "corey")
        return await call_next(request)
    app.include_router(h3_video_routes.setup_h3_video_routes(manager) if family == "h3" else setup_bfs_video_routes(manager))
    prefix = "/api/video/" + family
    with TestClient(app) as client:
        path = f"{prefix}/jobs/{directory.name}/record"
        assert client.delete(path, headers={"x-user": "reader"}).status_code == 403
        assert client.delete(path, headers={"x-user": "other"}).status_code == 404
        assert client.delete(f"{prefix}/jobs/{active.name}/record").status_code == 409
        assert client.delete(f"{prefix}/jobs/{directory.name}").status_code == 200
        assert directory.exists()
        assert client.delete(path).json() == {"deleted": True, "id": directory.name}
        assert client.delete(path).status_code == 404
        if family == "h3":
            monkeypatch.setattr(h3_video_routes, "gpu_telemetry", lambda: {"gpus": [{"id": "GPU-test"}]})
            assert client.get(prefix + "/gpus", headers={"x-user": "reader"}).status_code == 403
            assert client.get(prefix + "/gpus").json() == {"gpus": [{"id": "GPU-test"}]}


def test_job_view_reports_saved_gpu_placement_without_manifest_leak(tmp_path):
    manager = h3.H3JobManager(tmp_path / "jobs")
    directory, _ = job(manager)
    view = manager.view(directory.name, "corey")
    assert view["gpu"] == "GPU-main" and view["vae_gpu"] == "GPU-vae"
    assert "config" not in view and "manifest" not in view
    h3._write(directory / "manifest.json", {"config": {"gpu": "GPU-main", "prompt": "old"}})
    assert manager.view(directory.name, "corey")["vae_gpu"] == ""
