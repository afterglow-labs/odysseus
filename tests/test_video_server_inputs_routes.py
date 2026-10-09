"""Computer-selected videos use the same durable jobs as phone uploads."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from src import h3_video as h3, h3_video_length
from src.video_submission import HEADER
from tests.test_video_submission import backend, image, send


@pytest.fixture
def server_media(tmp_path, monkeypatch):
    root = tmp_path / "E"
    directory = root / "TV Shows" / "Cuts"
    directory.mkdir(parents=True)
    # The picker only returns public root IDs and relative paths to the client.
    monkeypatch.setenv("ODYSSEUS_VIDEO_BROWSE_ROOTS", json.dumps({"e": str(root)}))

    def put(name, content, index=0):
        path = directory / name
        path.write_bytes(content)
        st = path.stat()
        return path, {"root": "e", "path": path.relative_to(root).as_posix(),
                      "size": st.st_size, "mtime_ns": str(st.st_mtime_ns), "index": index}
    return SimpleNamespace(root=root, put=put)


def create(backend, family, rows, *, files=(), key=None, config=None, extra=None, owner="corey"):
    data = {"config": json.dumps(config or backend.configs[family]), "server_inputs": json.dumps(rows)}
    data.update(extra or {})
    return backend.client().post("/api/video/" + family + "/jobs", data=data, files=files,
        headers={"x-user": owner, HEADER: key or uuid.uuid4().hex})


def manifest(backend, response, family="h3"):
    assert response.status_code == 201, response.text
    manager = backend.managers[family]
    directory = manager.directory(response.json()["id"])
    return directory, h3._read(directory / "manifest.json")


def test_mixed_phone_and_computer_references_keep_exact_order(backend, server_media):
    first, one = server_media.put("01.mp4", b"first reference", index=0)
    last, three = server_media.put("03.mp4", b"third reference", index=2)
    response = create(backend, "h3", {"reference_videos": [one, three]},
                      files=[("reference_videos", ("02.mp4", b"phone reference", "video/mp4"))])
    directory, job = manifest(backend, response)
    assert [Path(p).read_bytes() for p in job["reference_videos"]] == [
        b"first reference", b"phone reference", b"third reference"]
    assert job["input_names"]["reference_videos"] == ["01.mp4", "02.mp4", "03.mp4"]
    assert all(Path(p).parent == directory and not Path(p).is_symlink() for p in job["reference_videos"])
    assert str(server_media.root) not in response.text
    first.unlink()
    last.unlink()
    assert Path(job["reference_videos"][0]).read_bytes() == b"first reference"


def test_bfs_combines_server_target_with_phone_identity(backend, server_media):
    source, descriptor = server_media.put("target.mov", b"target video")
    response = create(backend, "bfs", {"source_video": [descriptor]},
                      files=[("identity_image", ("head.png", image(), "image/png"))])
    directory, job = manifest(backend, response, "bfs")
    assert Path(job["source_video"]).parent == directory
    assert Path(job["source_video"]).read_bytes() == source.read_bytes()
    assert Path(job["identity_image"]).read_bytes() == image()
    assert job["source_name"] == "target.mov"


def test_accepted_retry_does_not_reopen_missing_server_source(backend, server_media):
    source, descriptor = server_media.put("source.mp4", b"saved original")
    key = uuid.uuid4().hex
    first = create(backend, "h3", {"reference_videos": [descriptor]}, key=key)
    _, job = manifest(backend, first)
    source.unlink()
    retry = backend.client().post("/api/video/h3/jobs", headers={HEADER: key})
    assert retry.status_code == 201 and retry.json()["id"] == first.json()["id"]
    assert Path(job["reference_videos"][0]).read_bytes() == b"saved original"
    assert len(backend.calls) == 1


def test_batch_auto_length_uses_each_server_video(backend, server_media, monkeypatch):
    durations = {b"short": 4., b"long": 14.}
    seen = []
    def probe(path, *, exact_frames):
        data = Path(path).read_bytes()
        seen.append(data)
        return {"duration_seconds": durations[data], "source_frames": round(durations[data] * 24)}
    monkeypatch.setattr(h3_video_length, "probe_video", probe)
    frames = []
    for content in durations:
        _, row = server_media.put(content.decode() + ".mp4", content)
        result = create(backend, "h3", {"reference_videos": [row]}, extra={"auto_video_length": "true"})
        _, job = manifest(backend, result)
        frames.append(job["config"]["frames"])
    assert frames == [124, 345] and seen == [b"short", b"long"]


def test_queued_edit_retains_prior_reference_and_adds_server_video(backend, server_media):
    initial = send(backend, "h3", name="retained.mp4", video=b"retained reference")
    directory, before = manifest(backend, initial)
    _, row = server_media.put("new.mp4", b"new reference")
    response = backend.client().patch("/api/video/h3/jobs/" + initial.json()["id"], data={
        "config": json.dumps({**backend.configs["h3"], "prompt": "Updated instruction"}),
        "revision": "0", "server_inputs": json.dumps({"reference_videos": [row]})})
    assert response.status_code == 200, response.text
    saved = h3._read(directory / "manifest.json")
    assert saved["revision"] == 1 and saved["reference_videos"][0] == before["reference_videos"][0]
    assert [Path(p).read_bytes() for p in saved["reference_videos"]] == [b"retained reference", b"new reference"]
    assert saved["input_names"]["reference_videos"] == ["retained.mp4", "new.mp4"]
    assert len(backend.calls) == 1


def test_export_copies_server_media_preserving_portable_order(backend, server_media):
    source, row = server_media.put("server.mov", b"server export video", index=0)
    client = backend.client()
    prepared = client.post("/api/video/h3/workflow/export", data={
        "config": json.dumps(backend.configs["h3"]),
        "options": json.dumps({"include_weights": False, "include_attachments": True}),
        "server_inputs": json.dumps({"reference_videos": [row]})},
        files=[("reference_videos", ("phone.mp4", b"phone export video", "video/mp4"))])
    assert prepared.status_code == 200, prepared.text
    source.unlink()
    downloaded = client.get(prepared.json()["download_url"])
    assert downloaded.status_code == 200, downloaded.text
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        recipe = json.loads(archive.read("workflow.json"))
        assets = recipe["attachments"]
        assert [asset["name"] for asset in assets] == ["server.mov", "phone.mp4"]
        assert [archive.read(asset["path"]) for asset in assets] == [b"server export video", b"phone export video"]
        assert str(server_media.root) not in json.dumps(recipe)
    assert not backend.calls


def test_source_changed_after_selection_does_not_publish_job(backend, server_media):
    source, row = server_media.put("changed.mp4", b"original")
    source.write_bytes(b"changed content")
    result = create(backend, "h3", {"reference_videos": [row]})
    assert result.status_code in {400, 409}, result.text
    assert not backend.calls and not list(backend.managers["h3"].root.glob("*/manifest.json"))


def test_source_changed_during_copy_cleans_staging_before_launch(backend, server_media, monkeypatch):
    from routes import h3_video_routes
    source, row = server_media.put("changing.mp4", b"original video")
    original_write = h3_video_routes.store_upload_chunk
    async def write_then_change(stream, directory, chunk):
        await original_write(stream, directory, chunk)
        source.write_bytes(b"changed while copying")
    monkeypatch.setattr(h3_video_routes, "store_upload_chunk", write_then_change)
    result = create(backend, "h3", {"reference_videos": [row]})
    assert result.status_code == 409 and "changed" in result.text
    assert not backend.calls
    assert not list(backend.managers["h3"].root.glob("*/manifest.json"))
    assert not list(backend.managers["h3"].root.glob("*/*.mp4"))
    assert not list((backend.managers["h3"].root / ".submissions").glob("*.json"))


def test_rerun_uses_snapshot_when_computer_video_is_gone(backend, server_media):
    source, row = server_media.put("rerun.mp4", b"original source")
    first = create(backend, "h3", {"reference_videos": [row]})
    original_directory, original = manifest(backend, first)
    source.unlink()
    response = backend.client().post("/api/video/h3/queue/rerun", json={
        "jobs": [{"id": first.json()["id"], "revision": 0}],
        "patch": {"steps": 9}, "request_id": uuid.uuid4().hex})
    assert response.status_code == 200, response.text
    assert response.json()["rerun"] == 1 and response.json()["failed"] == 0
    result = response.json()["jobs"][0]
    directory = backend.managers["h3"].directory(result["id"])
    saved = h3._read(directory / "manifest.json")
    assert directory != original_directory and saved["config"]["steps"] == 9
    assert saved["config"]["prompt"] == original["config"]["prompt"]
    assert Path(saved["reference_videos"][0]).parent == directory
    assert Path(saved["reference_videos"][0]).read_bytes() == b"original source"
    assert Path(original["reference_videos"][0]).read_bytes() == b"original source"


def test_server_media_counts_toward_upload_limit(backend, server_media, monkeypatch):
    monkeypatch.setattr("routes.h3_video_routes.get_chat_upload_max_bytes", lambda: 8)
    _, row = server_media.put("large.mp4", b"12345678")
    result = create(backend, "h3", {"reference_videos": [row]},
                    files=[("reference_videos", ("phone.mp4", b"9", "video/mp4"))])
    assert result.status_code == 413, result.text
    assert not backend.calls and not list(backend.managers["h3"].root.glob("*/manifest.json"))


def test_combined_reference_count_is_validated_before_copy(backend, server_media):
    _, row = server_media.put("fourth.mp4", b"extra reference", index=3)
    result = create(backend, "h3", {"reference_videos": [row]}, files=[
        ("reference_videos", (f"phone{i}.mp4", b"phone", "video/mp4")) for i in range(3)])
    assert result.status_code == 400 and "3 reference videos" in result.text
    assert not backend.calls and not list(backend.managers["h3"].root.glob("*/manifest.json"))


def test_nonadmin_cannot_select_server_media(backend, server_media):
    _, row = server_media.put("private.mp4", b"private")
    result = create(backend, "h3", {"reference_videos": [row]}, owner="reader")
    assert result.status_code == 403 and not backend.calls
    assert not backend.managers["h3"].root.exists()
