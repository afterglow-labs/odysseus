"""Each auto-length submission gets its own duration and durable frame count."""
import copy
from fractions import Fraction
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import uuid

import pytest

from src import h3_video as h3, h3_video_length as length
from src.video_submission import HEADER
from tests.test_h3_video import inventory, weights
from tests.test_h3_vfx_install import vfx_inventory
from tests.test_video_submission import backend


@pytest.mark.parametrize("duration,expected", [
    (2, 124), (4, 124), (124 / 24, 124), (124 / 24 + .001, 141),
    (7, 175), (10, 243), (345 / 24, 345), (345 / 24 + .001, 362), (15, 362), (15.05, 362),
])
def test_ordinary_length_uses_shortest_choice_covering_whole_source(monkeypatch, duration, expected):
    monkeypatch.setattr(length, "probe_video", lambda *args, **kwargs:
                        {"duration_seconds": duration, "source_frames": round(duration * 24)})
    original = {"mode": "ref2va", "frames": 362, "loras": [], "prompt": "Same instruction"}
    uploads = {"reference_videos": ["source.mp4"], "reference_images": ["common.png"]}
    before = copy.deepcopy((original, uploads))
    chosen, info = length.select_batch_video_length(original, uploads)
    assert chosen["frames"] == expected and chosen is not original
    assert info == {"duration_seconds": duration, "frames": expected, "selected_seconds": expected / 24,
                    "output_seconds": expected / 24, "source_field": "reference_videos", "capped": False,
                    "preserve_source_duration": False}
    assert (original, uploads) == before


@pytest.mark.parametrize("duration", [1, 1.998, 15.051, 1000])
def test_unsupported_duration_is_rejected_before_queueing(monkeypatch, duration):
    monkeypatch.setattr(length, "probe_video", lambda *args, **kwargs:
                        {"duration_seconds": duration, "source_frames": round(duration * 24)})
    with pytest.raises(ValueError, match="2–15 seconds"):
        length.select_batch_video_length({"mode": "ref2va", "loras": []}, {"reference_videos": ["clip.mp4"]})


@pytest.mark.parametrize("mode,uploads", [("t2va", {}), ("ref2va", {}),
    ("ref2va", {"reference_videos": ["one.mp4", "two.mp4"]})])
def test_auto_length_never_guesses_which_reference_is_the_batch_source(monkeypatch, mode, uploads):
    monkeypatch.setattr(length, "probe_video", lambda *args, **kwargs: pytest.fail("No unambiguous source"))
    with pytest.raises(ValueError, match="exactly one source video"):
        length.select_batch_video_length({"mode": mode, "loras": []}, uploads)


@pytest.mark.parametrize("source_frames,expected", [(73, 73), (74, 90), (124, 124), (125, 141), (360, 362)])
def test_vfx_uses_only_source_and_preserves_actual_output_length(monkeypatch, source_frames, expected):
    def probe(path, *, exact_frames):
        assert path == "source.mp4" and exact_frames is True
        return {"duration_seconds": source_frames / 24 + .001, "source_frames": source_frames}
    monkeypatch.setattr(length, "probe_video", probe)
    config = {"mode": "ref2va", "frames": 362,
              "loras": [{"path": h3.VFX_EDIT_LORA, "strength": 1}]}
    chosen, info = length.select_batch_video_length(config,
        {"source_video": "source.mp4", "reference_videos": ["common-motion.mp4"]})
    assert chosen["frames"] == expected and info["selected_seconds"] == expected / 24
    assert info["output_seconds"] == source_frames / 24 and info["preserve_source_duration"] is True
    assert info["source_field"] == "source_video"


def test_vfx_too_short_source_is_rejected(monkeypatch):
    monkeypatch.setattr(length, "probe_video", lambda *args, **kwargs: {"duration_seconds": 3, "source_frames": 72})
    with pytest.raises(ValueError, match="73 source frames"):
        length.select_batch_video_length({"mode": "ref2va", "lora": h3.VFX_EDIT_LORA}, {"source_video": "clip.mp4"})


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout="", stderr="/private/path: bad video"),
    SimpleNamespace(returncode=0, stdout='{"duration_seconds": NaN, "source_frames": 120}'),
    SimpleNamespace(returncode=0, stdout='{"duration_seconds": 5, "source_frames": true}'),
    SimpleNamespace(returncode=0, stdout='{}'),
    SimpleNamespace(returncode=0, stdout='[]'),
])
def test_probe_failure_is_clear_and_does_not_publish_private_diagnostics(monkeypatch, result):
    monkeypatch.setattr(length.subprocess, "run", lambda *args, **kwargs: result)
    with pytest.raises(ValueError, match="Could not detect this video's duration") as error:
        length.probe_video("clip.mp4")
    assert "private" not in str(error.value)


def test_probe_has_process_timeout_and_private_python(monkeypatch):
    def run(argv, **kwargs):
        assert argv[0] == length.sys.executable and argv[1:4] == ["-m", "src.h3_video_length", "--probe"]
        assert kwargs["timeout"] == 30 and "shell" not in kwargs
        raise subprocess.TimeoutExpired(argv, 30)
    monkeypatch.setattr(length.subprocess, "run", run)
    with pytest.raises(ValueError, match="within 30 seconds"):
        length.probe_video("clip.mp4")


def _send(backend, *, auto="true", key=None, videos=(b"clip",), config=None):
    body = {"config": json.dumps(config or backend.configs["h3"])}
    if auto is not None:
        body["auto_video_length"] = auto
    return backend.client().post("/api/video/h3/jobs", data=body,
        headers={HEADER: key or uuid.uuid4().hex},
        files=[("reference_videos", (f"clip{i}.mp4", video, "video/mp4")) for i, video in enumerate(videos)])


def test_each_batch_file_saves_own_frames_and_retry_keeps_receipt(backend, monkeypatch):
    durations = {b"short": 4., b"medium": 7., b"long": 14.}
    probes = []
    def probe(path, *, exact_frames):
        assert exact_frames is False
        duration = durations[Path(path).read_bytes()]
        probes.append(path)
        return {"duration_seconds": duration, "source_frames": round(duration * 24)}
    monkeypatch.setattr(length, "probe_video", probe)
    original = copy.deepcopy(backend.configs["h3"])
    key = uuid.uuid4().hex
    replies = [_send(backend, key=key if data == b"short" else None, videos=(data,))
               for data in durations]
    assert [r.status_code for r in replies] == [201] * 3, [r.text for r in replies]
    manager = backend.managers["h3"]
    for reply, frames in zip(replies, [124, 175, 345]):
        job = reply.json()
        manifest = h3._read(manager.directory(job["id"]) / "manifest.json")
        assert manifest["config"]["frames"] == frames
        assert job["batch_video_length"] == manifest["batch_video_length"]
        assert job["batch_video_length"]["frames"] == frames
    assert backend.configs["h3"] == original
    retry = backend.client().post("/api/video/h3/jobs", headers={HEADER: key})
    assert retry.status_code == 201 and retry.json()["batch_video_length"] == replies[0].json()["batch_video_length"]
    assert len(probes) == len(backend.calls) == 3
    # Metadata describes the original automatic decision, not a later edit.
    directory = manager.directory(replies[0].json()["id"])
    manifest = h3._read(directory / "manifest.json")
    h3._write(directory / "manifest.json", {**manifest, "revision": 1})
    assert "batch_video_length" not in manager.view(directory.name, "corey")


@pytest.mark.parametrize("auto", [None, "false"])
def test_manual_and_multi_reference_submissions_are_unchanged(backend, monkeypatch, auto):
    monkeypatch.setattr(length, "probe_video", lambda *args, **kwargs: pytest.fail("Manual length must not probe"))
    response = _send(backend, auto=auto, videos=(b"one", b"two"))
    assert response.status_code == 201, response.text
    assert "batch_video_length" not in response.json()
    manifest = h3._read(backend.managers["h3"].directory(response.json()["id"]) / "manifest.json")
    assert len(manifest["reference_videos"]) == 2
    assert manifest["config"]["frames"] == backend.configs["h3"]["frames"]


def test_auto_probe_error_cleans_staging_without_queueing(backend, monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("Could not detect this video's duration")
    monkeypatch.setattr(length, "probe_video", fail)
    response = _send(backend)
    assert response.status_code == 400 and "duration" in response.text
    manager = backend.managers["h3"]
    assert not backend.calls and not list(manager.root.glob("*/manifest.json"))
    assert not list((manager.root / ".submissions").glob("*.json"))
    assert not list(manager.root.glob("*/*.mp4"))


@pytest.mark.parametrize("value", ["yes", "1", "", "TRUE"])
def test_auto_length_flag_must_be_explicit_boolean_text(backend, value):
    response = _send(backend, auto=value)
    assert response.status_code == 400 and "auto_video_length" in response.text
    assert not backend.calls


def test_vfx_route_keeps_common_references_separate_and_saves_actual_sampling_frames(backend, vfx_inventory, monkeypatch):
    inv, raw = vfx_inventory
    manager = backend.managers["h3"]
    manager.inventory = lambda: inv
    def probe(path, *, exact_frames):
        assert Path(path).read_bytes() == b"source" and exact_frames
        return {"duration_seconds": 4.125, "source_frames": 99}
    monkeypatch.setattr(length, "probe_video", probe)
    response = backend.client().post("/api/video/h3/jobs",
        data={"config": json.dumps(raw), "auto_video_length": "true"},
        files=[("source_video", ("source.mp4", b"source", "video/mp4")),
               ("reference_videos", ("common.mp4", b"common", "video/mp4"))])
    assert response.status_code == 201, response.text
    job = response.json()
    manifest = h3._read(manager.directory(job["id"]) / "manifest.json")
    assert manifest["config"]["frames"] == 107
    assert job["batch_video_length"]["output_seconds"] == 99 / 24
    assert Path(manifest["reference_videos"][0]).read_bytes() == b"common"


@pytest.mark.parametrize("rate,count,start", [(24, 124, 0), (30, 155, 60), (24, 125, 0)])
def test_real_probe_matches_video_reader_at_grid_boundaries(tmp_path, rate, count, start):
    av = pytest.importorskip("av")
    np = pytest.importorskip("numpy")
    from scripts.h3_video_worker import read_video
    path = tmp_path / "clip.mp4"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=rate)
        stream.width, stream.height, stream.pix_fmt = 32, 32, "yuv420p"
        for index in range(count):
            frame = av.VideoFrame.from_ndarray(np.full((32, 32, 3), index % 255, np.uint8), format="rgb24")
            frame.pts, frame.time_base = start + index, Fraction(1, rate)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    frames, _, duration, _ = read_video(path)
    actual = length.probe_video(path, exact_frames=True)
    assert actual["source_frames"] == len(frames)
    assert actual["duration_seconds"] == pytest.approx(duration)
    assert length.probe_video(path)["duration_seconds"] == pytest.approx(duration)


def test_real_invalid_media_returns_clear_error(tmp_path):
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"not a video")
    with pytest.raises(ValueError, match="Could not detect this video's duration"):
        length.probe_video(path)


@pytest.mark.parametrize("metadata_duration", [None, 5000])
def test_probe_uses_video_duration_or_vfr_packet_fallback_without_network_protocols(monkeypatch, metadata_duration):
    stream = SimpleNamespace(duration=metadata_duration, time_base=Fraction(1, 1000), average_rate=25)
    packets = [SimpleNamespace(size=1, pts=10000, time_base=Fraction(1, 1000), duration=40),
               SimpleNamespace(size=1, pts=14920, time_base=Fraction(1, 1000), duration=80)]
    source = SimpleNamespace(streams=SimpleNamespace(video=[stream]), duration=99_000_000)
    def demux(selected):
        assert selected is stream and metadata_duration is None
        return packets
    source.demux = demux
    class Context:
        def __enter__(self):
            return source
        def __exit__(self, *args):
            return None
    def open_video(path, **kwargs):
        assert kwargs["options"]["protocol_whitelist"] == "file,pipe"
        return Context()
    monkeypatch.setitem(length.sys.modules, "av", SimpleNamespace(open=open_video))
    actual = length._probe_video("local.mp4")
    assert actual == {"duration_seconds": 5., "source_frames": 120}
