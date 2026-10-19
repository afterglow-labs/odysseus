"""Selected video windows retain their soundtrack and survive job operations."""
import copy
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
import pytest

from scripts import h3_video_worker as worker
from scripts.h3_reference_video import read_selected_video, selected_duration, validate_ranges
from src import h3_video as h3, h3_video_length as length, video_workflow as portable
from tests.test_video_submission import backend, send
from tests.test_video_queue_bulk import request


def selection(start=0, duration=15):
    return {"start_seconds": start, "duration_seconds": duration}


@pytest.mark.parametrize("value,count", [
    (None, 1), ({}, 1), ([selection()] * 4, 4), ([selection()], 0),
    ([selection()], 2), ([selection(-1)], 1), ([selection(float("inf"))], 1),
    ([selection(True)], 1), ([selection(duration=False)], 1),
    ([selection(duration=1.99)], 1), ([selection(duration=15.01)], 1),
    ([selection(duration=float("nan"))], 1), ([{"start_seconds": 0}], 1),
    ([{**selection(), "path": "/tmp/other"}], 1),
])
def test_ranges_are_strict_finite_and_aligned(value, count):
    with pytest.raises(ValueError, match="[Rr]eference video|range"):
        validate_ranges(value, count)


def test_legacy_empty_ranges_and_short_remaining_selection():
    assert validate_ranges([], 3) == []
    assert selected_duration(22, selection(17)) == 5
    with pytest.raises(ValueError, match="earlier start"):
        selected_duration(22, selection(21))


@pytest.fixture(scope="module")
def timeline(tmp_path_factory):
    path = tmp_path_factory.mktemp("reference-video-ranges") / "timeline.mp4"
    # Every second has its own red intensity and audio frequency. H.264's
    # default GOP spans this clip, so a non-keyframe seek must decode accurately.
    frames = np.zeros((20 * 24, 16, 16, 3), dtype=np.uint8)
    for index in range(len(frames)):
        frames[index, :, :, 0] = 10 + index // 24 * 10
    rate = 32000
    waveform = np.concatenate([.25 * np.sin(2 * np.pi * (200 + second * 40)
                               * np.arange(rate) / rate) for second in range(20)]).astype(np.float32)
    worker.mux_mp4(path, frames, {"sample_rate": rate, "waveform": waveform[None, :]})
    return path


def frequency(waveform, rate=32000):
    data = waveform.detach().numpy()
    return np.fft.rfftfreq(len(data), 1 / rate)[np.argmax(abs(np.fft.rfft(data)))]


def test_non_keyframe_selection_gets_matching_video_and_audio(timeline):
    before = timeline.read_bytes()
    frames, audio, duration, audio_duration = read_selected_video(timeline, selection(8.25, 3), worker.read_video)
    assert len(frames) == 72 and duration == pytest.approx(3, abs=.02)
    assert audio_duration == pytest.approx(3, abs=.02)
    assert frames[0, :, :, 0].mean().item() * 255 == pytest.approx(90, abs=3)
    assert frames[24, :, :, 0].mean().item() * 255 == pytest.approx(100, abs=3)
    assert frequency(audio["waveform"][0, 0, 1600:17600]) == pytest.approx(520, abs=2)
    assert frequency(audio["waveform"][0, 0, 33600:49600]) == pytest.approx(560, abs=2)
    assert timeline.read_bytes() == before


def test_tail_selection_uses_remaining_length_and_rejects_past_end(timeline):
    frames, audio, duration, _ = read_selected_video(timeline, selection(17), worker.read_video)
    assert len(frames) == 72 and duration == pytest.approx(3, abs=.02)
    assert frequency(audio["waveform"][0, 0, 1600:17600]) == pytest.approx(880, abs=2)
    with pytest.raises(ValueError, match="earlier start|at least 2 seconds"):
        read_selected_video(timeline, selection(21), worker.read_video)


@pytest.mark.parametrize("delayed_track", ["audio", "video"])
def test_selection_preserves_original_stream_offsets(timeline, tmp_path, delayed_track):
    import imageio_ffmpeg
    executable = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
    target = tmp_path / "offset.mp4"
    # The first input is delayed by two seconds; map that input to one stream
    # and the unshifted input to the other, retaining their original timeline.
    maps = ["1:v:0", "0:a:0"] if delayed_track == "audio" else ["0:v:0", "1:a:0"]
    subprocess.run([executable, "-nostdin", "-loglevel", "error", "-itsoffset", "2", "-i", str(timeline),
                    "-i", str(timeline), "-map", maps[0], "-map", maps[1], "-c", "copy", str(target)],
                   check=True, capture_output=True, timeout=30)
    if delayed_track == "audio":
        frames, audio, _, _ = read_selected_video(target, selection(1, 3), worker.read_video)
        assert frames[0, :, :, 0].mean().item() * 255 == pytest.approx(20, abs=3)
        waveform = audio["waveform"][0, 0]
        assert waveform[1600:20000].abs().max() < .01
        assert frequency(waveform[38400:54400]) == pytest.approx(200, abs=2)
    else:
        frames, audio, _, _ = read_selected_video(target, selection(8.25, 3), worker.read_video)
        assert frames[0, :, :, 0].mean().item() * 255 == pytest.approx(90, abs=3)
        assert frequency(audio["waveform"][0, 0, 1600:17600]) == pytest.approx(600, abs=2)


@pytest.mark.parametrize("count", [2, 3])
def test_prepare_media_keeps_each_fifteen_second_selection_and_soundtrack(timeline, count):
    media = {"reference_images": [], "reference_audio": [], "first_frame": None,
             "last_frame": None, "reference_videos": [str(timeline)] * count}
    config = {"width": 32, "height": 32, "frames": 362, "reference_size": "match",
              "reference_video_ranges": [selection(i, 15) for i in range(count)]}
    prepared = worker.prepare_media(media, config)
    assert list(prepared["ref_videos"]) == [f"ref_video_{i}" for i in range(1, count + 1)]
    for i in range(1, count + 1):
        assert len(prepared["ref_videos"][f"ref_video_{i}"]) == 362
        audio = prepared["ref_video_audios"][f"ref_video_audio_{i}"]["waveform"]
        assert audio.shape[-1] == round(362 / 24 * worker.AUDIO_RATE)
        assert frequency(audio[0, 0, 1600:17600]) == pytest.approx(200 + (i - 1) * 40, abs=2)


def test_auto_batch_length_uses_selected_window_not_full_movie(monkeypatch):
    monkeypatch.setattr(length, "probe_video", lambda *a, **kw:
                        {"duration_seconds": 120, "source_frames": 360})
    config = {"mode": "ref2va", "frames": 362, "reference_video_ranges": [selection(114, 15)]}
    before = copy.deepcopy(config)
    chosen, info = length.select_batch_video_length(config, {"reference_videos": ["movie.mp4"]})
    assert chosen["frames"] == 158 and info["duration_seconds"] == 6
    assert info["source_duration_seconds"] == 120 and info["reference_video_range"] == selection(114)
    assert config == before


def test_queue_edit_reorders_ranges_with_retained_videos_then_rerun_preserves(backend):
    backend.configs["h3"]["reference_video_ranges"] = [selection(2, 5), selection(20, 5)]
    first = send(backend, "h3", extra=[("reference_videos", ("a.mp4", b"video A", "video/mp4"))])
    assert first.status_code == 201, first.text
    job = first.json()
    client = backend.client()
    url = "/api/video/h3/jobs/" + job["id"]
    draft = client.get(url + "/edit").json()
    assert draft["config"]["reference_video_ranges"] == [selection(2, 5), selection(20, 5)]
    config = {**draft["config"], "reference_video_ranges": [selection(20, 5), selection(33, 5)]}
    edited = client.patch(url, data={"config": json.dumps(config), "revision": "0",
                                    "retain_inputs": json.dumps({"reference_videos": [1]})},
                          files=[("reference_videos", ("new.mp4", b"video new", "video/mp4"))])
    assert edited.status_code == 200, edited.text
    directory = backend.managers["h3"].directory(job["id"])
    manifest = h3._read(directory / "manifest.json")
    assert manifest["config"]["reference_video_ranges"] == config["reference_video_ranges"]
    assert manifest["input_names"]["reference_videos"] == ["clip01.mp4", "new.mp4"]
    assert [Path(p).read_bytes() for p in manifest["reference_videos"]] == [b"video fixture", b"video new"]
    state = h3._read(directory / "state.json")
    h3._write(directory / "state.json", {**state, "status": "completed"})
    retry = request(backend, "rerun", [edited.json()], {"steps": 3})
    assert retry.status_code == 200, retry.text
    child = backend.managers["h3"].directory(retry.json()["jobs"][0]["id"])
    assert h3._read(child / "manifest.json")["config"]["reference_video_ranges"] == config["reference_video_ranges"]


def test_portable_recipe_preserves_ranges_without_attachments_and_final_is_immutable():
    config = {"mode": "ref2va", "reference_video_ranges": [selection(32.25, 12)]}
    document, _ = portable.make_recipe(config, {}, {}, "h3", portable.export_options({}))
    portable.validate_document(document, "h3")
    assert document["config"]["reference_video_ranges"] == config["reference_video_ranges"]
    from src.h3_seed_hunter import guard_edit
    original = {**config, "recipe": "seed_hunter", "seed_hunter": {"stage": "final"}}
    with pytest.raises(ValueError, match="source prompt"):
        guard_edit(original, {**original, "reference_video_ranges": [selection(0, 12)]})


def test_new_job_rejects_range_count_mismatch_before_launch(backend):
    backend.configs["h3"]["reference_video_ranges"] = [selection(), selection()]
    response = send(backend, "h3")
    assert response.status_code == 400 and "one reference video range" in response.text
    assert not backend.calls


def test_legacy_reader_still_rejects_untrimmed_long_source(timeline):
    with pytest.raises(ValueError, match="15 seconds"):
        worker.read_video(timeline)


def test_portable_archive_roundtrip_keeps_selection_with_its_source(tmp_path):
    from tests.test_video_workflow import manager
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"saved whole source for future edits")
    config = {"mode": "ref2va", "reference_video_ranges": [selection(17, 8)]}
    doc, assets = portable.make_recipe(config, {},
        {"reference_videos": [{"path": str(video), "name": video.name}]}, "h3",
        portable.export_options({"include_attachments": True}))
    service = portable.WorkflowTransfers(manager(tmp_path / "target"), "h3")
    directory = service.stage("corey")
    archive = directory / "upload.zip"
    # ZIP streaming accepts already-open handles, just as the export route does.
    handles = [(row, open(row["path"], "rb")) for row in assets]
    try:
        archive.write_bytes(b"".join(portable.stream_zip(doc, handles)))
    finally:
        for _, handle in handles:
            handle.close()
    result = service.import_file(directory, "corey", archive)
    assert result["workflow"]["config"]["reference_video_ranges"] == config["reference_video_ranges"]
    assert result["workflow"]["attachments"][0]["name"] == "movie.mp4"
