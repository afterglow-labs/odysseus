"""Video source snapshots use their own budget and retain selected ranges."""
import io
import json
from pathlib import Path
from types import SimpleNamespace
import uuid
import zipfile

import pytest

from src import h3_video as h3, upload_limits
from tests.test_h3_seed_hunter_api import hunter
from tests.test_video_queue_bulk import request as queue_request
from tests.test_video_server_inputs_routes import create as create_server, server_media
from tests.test_video_submission import backend, image, send


# Staging validates container extensions without decoding source movies. These
# opaque synthetic bytes exercise the real copy/receipt paths with no worker.
SOURCE = bytes(range(256))
RANGE = {"start_seconds": 30, "duration_seconds": 5}


@pytest.fixture(autouse=True)
def separate_video_policy(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_CHAT_UPLOAD_MAX_BYTES", "16")
    monkeypatch.delenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", raising=False)


def _field(family):
    return "reference_videos" if family == "h3" else "source_video"


def _configure(backend, family):
    if family == "h3":
        backend.configs[family]["reference_video_ranges"] = [dict(RANGE)]


def _submit(backend, server_media, family, source_kind, *, content=SOURCE):
    _configure(backend, family)
    if source_kind == "multipart":
        return send(backend, family, video=content), None
    source, descriptor = server_media.put("source.mp4", content)
    files = [("identity_image", ("head.png", image(), "image/png"))] if family == "bfs" else []
    return create_server(backend, family, {_field(family): [descriptor]}, files=files), source


def _manifest(backend, job, family):
    directory = backend.managers[family].directory(job["id"])
    return directory, h3._read(directory / "manifest.json")


def _video_paths(manifest, family):
    value = manifest[_field(family)]
    return [Path(path) for path in value] if isinstance(value, list) else [Path(value)]


def _assert_no_published_job(backend, family):
    root = backend.managers[family].root
    assert not backend.calls
    assert not list(root.glob("*/manifest.json"))
    assert not list(root.glob("*/*.mp4"))
    assert not list((root / ".submissions").glob("*.json"))
    if root.exists():
        assert not [path for path in root.iterdir() if h3.JOB_ID.fullmatch(path.name)]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_video_policy_defaults_to_disk_capacity_independently_of_chat(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", value)
    assert upload_limits.get_chat_upload_max_bytes() == 16
    assert upload_limits.get_video_upload_max_bytes() is None


def test_explicit_video_budget_does_not_change_chat_budget(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", "64")
    assert upload_limits.get_video_upload_max_bytes() == 64
    assert upload_limits.get_chat_upload_max_bytes() == 16


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "not-bytes"])
def test_video_policy_rejects_invalid_explicit_budget(monkeypatch, value):
    monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", value)
    with pytest.raises(ValueError, match="ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES"):
        upload_limits.get_video_upload_max_bytes()


@pytest.mark.parametrize("family", ["h3", "bfs"])
@pytest.mark.parametrize("source_kind", ["multipart", "server"])
def test_sources_above_chat_budget_keep_complete_private_snapshots(
        backend, server_media, family, source_kind):
    response, source = _submit(backend, server_media, family, source_kind)
    assert response.status_code == 201, response.text
    directory, manifest = _manifest(backend, response.json(), family)
    saved = _video_paths(manifest, family)
    assert len(saved) == 1
    assert saved[0].parent == directory and not saved[0].is_symlink()
    assert saved[0].read_bytes() == SOURCE
    if family == "h3":
        assert manifest["config"]["reference_video_ranges"] == [RANGE]
    if source is not None:
        source.unlink()
        assert saved[0].read_bytes() == SOURCE
    assert len(backend.calls) == 1


@pytest.mark.parametrize("stage", ["preview", "scout"])
@pytest.mark.parametrize("source_kind", ["multipart", "server"])
def test_hunter_and_scout_keep_selected_range_and_independent_lengths(
        hunter, server_media, stage, source_kind):
    config = hunter.configs["h3"]
    config["frames"] = 107
    config["seed_hunter"].update(stage=stage, final_frames=311)
    if stage == "scout":
        hunter.inventories["h3"]["seed_hunter"]["features"]["seed_scout"] = True
        config["seed_hunter"].update(preview_count=1, scout_step=1)
    response, source = _submit(hunter, server_media, "h3", source_kind)
    assert response.status_code == 201, response.text
    directory, manifest = _manifest(hunter, response.json(), "h3")
    saved = manifest["config"]
    assert saved["recipe"] == "seed_hunter"
    assert saved["seed_hunter"]["stage"] == stage
    assert saved["frames"] == 107
    assert saved["seed_hunter"]["final_frames"] == 311
    assert saved["seed_hunter"]["preview_count"] == (3 if stage == "preview" else 1)
    assert saved["reference_video_ranges"] == [RANGE]
    if stage == "scout":
        assert saved["seed_hunter"]["scout_step"] == 1 < saved["steps"] == 6
    if source is not None:
        source.unlink()
    video = _video_paths(manifest, "h3")[0]
    assert video.parent == directory and video.read_bytes() == SOURCE
    assert len(hunter.calls) == 1


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_retained_edit_and_rerun_keep_full_sources_above_chat_budget(
        backend, server_media, family):
    first, source = _submit(backend, server_media, family, "server")
    assert first.status_code == 201, first.text
    source.unlink()
    url = f"/api/video/{family}/jobs/{first.json()['id']}"
    client = backend.client()
    draft = client.get(url + "/edit")
    assert draft.status_code == 200, draft.text
    saved = client.patch(url, data={
        "config": json.dumps({**draft.json()["config"], "prompt": "Keep the entire original source."}),
        "revision": "0",
    })
    assert saved.status_code == 200, saved.text
    original_directory, original = _manifest(backend, saved.json(), family)
    assert _video_paths(original, family)[0].read_bytes() == SOURCE
    if family == "h3":
        assert original["config"]["reference_video_ranges"] == [RANGE]
    rerun = queue_request(backend, "rerun", [saved.json()], {"steps": 9}, family=family)
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["rerun"] == 1 and rerun.json()["failed"] == 0
    directory, copied = _manifest(backend, rerun.json()["jobs"][0], family)
    assert directory != original_directory
    assert _video_paths(copied, family)[0].parent == directory
    assert _video_paths(copied, family)[0].read_bytes() == SOURCE
    if family == "h3":
        assert copied["config"]["reference_video_ranges"] == [RANGE]
    assert len(backend.calls) == 2


@pytest.mark.parametrize("source_kind", ["multipart", "server"])
def test_queued_edit_adds_whole_reference_without_resetting_ranges(
        backend, server_media, source_kind):
    _configure(backend, "h3")
    first = send(backend, "h3", video=SOURCE)
    assert first.status_code == 201, first.text
    second_range = {"start_seconds": 70, "duration_seconds": 3}
    data = {"config": json.dumps({**backend.configs["h3"],
            "reference_video_ranges": [RANGE, second_range]}), "revision": "0"}
    files = []
    if source_kind == "server":
        _, descriptor = server_media.put("second.mp4", SOURCE[::-1])
        data["server_inputs"] = json.dumps({"reference_videos": [descriptor]})
    else:
        files = [("reference_videos", ("second.mp4", SOURCE[::-1], "video/mp4"))]
    response = backend.client().patch("/api/video/h3/jobs/" + first.json()["id"],
                                      data=data, files=files)
    assert response.status_code == 200, response.text
    directory, manifest = _manifest(backend, response.json(), "h3")
    assert [path.read_bytes() for path in _video_paths(manifest, "h3")] == [SOURCE, SOURCE[::-1]]
    assert manifest["config"]["reference_video_ranges"] == [RANGE, second_range]
    assert not list((directory.parent / ".edits").iterdir())
    assert len(backend.calls) == 1


@pytest.mark.parametrize("family", ["h3", "bfs"])
@pytest.mark.parametrize("source_kind", ["multipart", "server"])
def test_draft_export_keeps_whole_source_and_original_range_above_chat_budget(
        backend, server_media, family, source_kind):
    _configure(backend, family)
    data = {"config": json.dumps(backend.configs[family]),
            "options": json.dumps({"include_weights": False, "include_attachments": True})}
    files, source = [], None
    if source_kind == "server":
        source, descriptor = server_media.put("source.mp4", SOURCE)
        data["server_inputs"] = json.dumps({_field(family): [descriptor]})
    else:
        files.append((_field(family), ("source.mp4", SOURCE, "video/mp4")))
    if family == "bfs":
        files.append(("identity_image", ("head.png", image(), "image/png")))
    client = backend.client()
    response = client.post(f"/api/video/{family}/workflow/export", data=data, files=files)
    assert response.status_code == 200, response.text
    if source is not None:
        source.unlink()
    download = client.get(response.json()["download_url"])
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        document = json.loads(archive.read("workflow.json"))
        attachment = next(item for item in document["attachments"] if item["field"] == _field(family))
        assert attachment["size"] == len(SOURCE)
        assert archive.read(attachment["path"]) == SOURCE
        if family == "h3":
            assert document["config"]["reference_video_ranges"] == [RANGE]
    assert not backend.calls


@pytest.mark.parametrize("family", ["h3", "bfs"])
@pytest.mark.parametrize("source_kind", ["multipart", "server"])
def test_explicit_video_budget_rejects_whole_source_before_launch(
        backend, server_media, monkeypatch, family, source_kind):
    monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", "64")
    response, _ = _submit(backend, server_media, family, source_kind)
    assert response.status_code == 413, response.text
    _assert_no_published_job(backend, family)


def test_explicit_budget_applies_to_combined_server_and_multipart_inputs(
        backend, server_media, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", "64")
    _, descriptor = server_media.put("server.mp4", b"s" * 40)
    response = create_server(backend, "h3", {"reference_videos": [descriptor]},
        files=[("reference_videos", ("phone.mp4", b"p" * 40, "video/mp4"))])
    assert response.status_code == 413, response.text
    _assert_no_published_job(backend, "h3")


def test_explicit_budget_applies_to_retained_and_new_bytes_atomically(backend, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_VIDEO_UPLOAD_MAX_BYTES", "64")
    first = send(backend, "h3", video=b"s" * 40)
    assert first.status_code == 201, first.text
    directory, original = _manifest(backend, first.json(), "h3")
    before = (directory / "manifest.json").read_bytes()
    response = backend.client().patch("/api/video/h3/jobs/" + first.json()["id"],
        data={"config": json.dumps(backend.configs["h3"]), "revision": "0"},
        files=[("reference_videos", ("extra.mp4", b"n" * 40, "video/mp4"))])
    assert response.status_code == 413, response.text
    assert (directory / "manifest.json").read_bytes() == before
    assert _video_paths(original, "h3")[0].read_bytes() == b"s" * 40
    assert len(backend.calls) == 1
    assert not list((directory.parent / ".edits").glob("*"))


@pytest.mark.parametrize("family", ["h3", "bfs"])
def test_no_fixed_video_cap_still_reserves_disk_and_cleans_failed_submission(
        backend, monkeypatch, family):
    monkeypatch.setattr("src.video_submission.shutil.disk_usage",
                        lambda _: SimpleNamespace(free=512 * 1024 * 1024 + 1))
    response = send(backend, family, str(uuid.uuid4()), video=SOURCE)
    assert response.status_code == 507 and "disk space" in response.text
    _assert_no_published_job(backend, family)
