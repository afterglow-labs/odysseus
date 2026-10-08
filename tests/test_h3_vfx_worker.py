"""CPU checks for standard VFX Edit source guides, timing, and soundtrack muxing."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from scripts import h3_video_worker as worker


@pytest.fixture
def job(tmp_path):
    config = {"mode": "ref2va", "prompt": "Add sparks to the hands. Keep the camera motion.",
              "width": 64, "height": 32, "frames": 124, "steps": 4, "gpu": "0"}
    for name in ("model", "encoder", "video_vae", "audio_vae", "lora"):
        path = tmp_path / (worker.VFX_EDIT_LORA if name == "lora" else name + ".safetensors")
        path.touch()
        config[name] = str(path)
    source = tmp_path / "source.mp4"
    source.touch()
    return {"config": config, "uploads": {"reference_videos": [str(source)]},
            "runtime_path": str(tmp_path / "core"), "output_path": str(tmp_path / "result.mp4"),
            "status_path": str(tmp_path / "status.json")}


@pytest.mark.parametrize("name,expected", [
    (worker.VFX_EDIT_LORA, True), (worker.VFX_EDIT_LORA.upper(), True),
    ("minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors", False),
    ("custom_vfx_edit.safetensors", False), ("turbo.safetensors", False), (None, False),
])
def test_only_standard_basename_selects_recipe(name, expected):
    assert worker.is_vfx_edit({"lora": str(Path("/models") / name) if name else None}) is expected


@pytest.mark.parametrize("prompt", [
    "Add sparks. Keep the camera motion.",
    "vfx_edit: Add sparks. Keep the camera motion.",
    " VFX_EDIT: vfx_edit: Add sparks. Keep the camera motion. ",
])
def test_prompt_has_one_trigger_and_preserves_edit_words(job, prompt):
    job["config"]["prompt"] = prompt
    config, _ = worker.validate_job(job)
    assert config["prompt"] == "vfx_edit: Add sparks. Keep the camera motion."
    assert worker.vfx_edit_prompt(config["prompt"]) == config["prompt"]
    assert job["config"]["prompt"] == prompt


@pytest.mark.parametrize("change,match", [
    ({"mode": "t2va"}, "requires reference mode"),
    ({"reference_videos": []}, "exactly one source"),
    ({"reference_videos": ["one.mp4", "two.mp4"]}, "exactly one source"),
    ({"reference_images": ["image.png"]}, "only the source video"),
    ({"reference_audio": ["sound.wav"]}, "only the source video"),
    ({"first_frame": "first.png"}, "only the source video"),
    ({"last_frame": "last.png"}, "only the source video"),
    ({"prompt": "vfx_edit: vfx_edit:"}, "edit instruction"),
    ({"prompt": "x" * 15999}, "16,000 characters"),
    ({"prompt": "Edit <Picture 1>."}, "without numbered"),
    ({"prompt": "Edit <Video 1>."}, "without numbered"),
    ({"prompt": "Keep <Audio 1>."}, "without numbered"),
    ({"prompt": "Keep <subject 2>."}, "without numbered"),
])
def test_invalid_vfx_inputs_rejected_before_runtime(job, change, match):
    for key, value in change.items():
        (job["config"] if key in ("mode", "prompt") else job["uploads"])[key] = value
    with pytest.raises(ValueError, match=match):
        worker.run_job(job, runtime_factory=lambda _: pytest.fail("Runtime must not initialize"))


def source_media(monkeypatch, *, count=200, duration=None, soundtrack=True):
    # Portrait source deliberately differs from the selected landscape canvas.
    frames = torch.arange(count, dtype=torch.float32).reshape(count, 1, 1, 1).expand(count, 16, 8, 3) / count
    audio = {"sample_rate": worker.AUDIO_RATE,
             "waveform": torch.full((1, 2, round(count / worker.FPS * worker.AUDIO_RATE)), .375)} if soundtrack else None
    monkeypatch.setattr(worker, "read_video", lambda _: (
        frames, audio, count / worker.FPS if duration is None else duration, 0))
    return frames, audio


def test_full_source_sets_grid_length_aspect_and_separate_silent_guide(job, monkeypatch):
    frames, audio = source_media(monkeypatch)
    config, media = worker.validate_job(job)
    prepared = worker.prepare_media(media, config)
    assert config["frames"] == 209  # Covers all 200 source frames, beyond the generic 124-frame setting.
    assert prepared["output_frames"] == 200
    assert (config["width"], config["height"]) == (32, 64)
    assert torch.equal(prepared["guide_video"][:200], frames)
    assert torch.equal(prepared["guide_video"][200:], frames[-1:].expand(9, *frames.shape[1:]))
    assert prepared["guide_audio"]["waveform"].count_nonzero() == 0
    assert prepared["guide_audio"]["waveform"].shape == (1, 2, round(209 / 24 * worker.AUDIO_RATE))
    assert torch.equal(prepared["source_audio"]["waveform"], audio["waveform"])
    assert audio["waveform"].shape[-1] == round(200 / 24 * worker.AUDIO_RATE)
    assert job["config"]["frames"] == 124


@pytest.mark.parametrize("count,expected", [(73, 73), (89, 90), (90, 90), (124, 124), (200, 209), (360, 362)])
def test_source_alignment_preserves_all_frames_ignoring_generic_limit(job, monkeypatch, count, expected):
    source, _ = source_media(monkeypatch, count=count, soundtrack=False)
    job["config"]["frames"] = 73
    config, media = worker.validate_job(job)
    prepared = worker.prepare_media(media, config)
    assert config["frames"] == len(prepared["guide_video"]) == expected
    assert prepared["output_frames"] == count <= expected <= 362 and (expected - 5) % 17 == 0
    assert torch.equal(prepared["guide_video"][:count], source)
    assert torch.equal(prepared["guide_video"][count:], source[-1:].expand(expected - count, *source.shape[1:]))
    assert prepared["source_audio"]["waveform"].count_nonzero() == 0
    assert prepared["source_audio"]["waveform"].shape[-1] == round(count / 24 * worker.AUDIO_RATE)


def test_partial_last_decoded_frame_is_preserved_in_24fps_output(job, monkeypatch):
    frames, _ = source_media(monkeypatch, count=90, duration=89.6 / 24, soundtrack=False)
    config, media = worker.validate_job(job)
    prepared = worker.prepare_media(media, config)
    assert config["frames"] == prepared["output_frames"] == 90
    assert torch.equal(prepared["guide_video"], frames)


@pytest.mark.parametrize("count", [48, 72])
def test_source_shorter_than_training_minimum_fails_before_weights(job, monkeypatch, count):
    source_media(monkeypatch, count=count, soundtrack=False)
    with pytest.raises(ValueError, match="at least 73 source frames"):
        worker.run_job(job, runtime_factory=lambda _: SimpleNamespace(
            load=lambda *_: pytest.fail("Must validate source before loading weights")))


@pytest.mark.parametrize("source_width,source_height,area", [
    (1920, 1080, 672 * 384), (1080, 1920, 960 * 544), (1080, 1920, 768 * 1344),
    (10000, 100, 32 * 32), (100, 10000, 512 * 512),
])
def test_source_canvas_respects_pixel_and_dimension_limits(source_width, source_height, area):
    width, height = worker._vfx_canvas(source_width, source_height, area)
    assert 32 <= width <= 2048 and width % 32 == 0
    assert 32 <= height <= 2048 and height % 32 == 0
    assert width * height <= area
    if min(source_width, source_height) >= 1000:
        assert abs(width / height - source_width / source_height) < .1


@pytest.mark.parametrize("soundtrack", [True, False])
@pytest.mark.parametrize("count,sampling_count", [(200, 209), (360, 362)])
def test_worker_guides_source_at_zero_and_muxes_complete_edited_clip_and_original_audio(
        job, monkeypatch, soundtrack, count, sampling_count):
    source, original_audio = source_media(monkeypatch, count=count, soundtrack=soundtrack)
    calls = {}

    class Runtime(worker.H3Runtime):
        def __init__(self, path):
            self.h3 = SimpleNamespace(
                MiniMaxH3ImageToVideo=SimpleNamespace(execute=self.text_condition),
                MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=lambda **kw: pytest.fail("No native references")),
                MiniMaxH3AddGuide=SimpleNamespace(execute=self.guide))
            self.nodes = SimpleNamespace(VAEDecode=lambda: SimpleNamespace(
                decode=lambda vae, samples: [calls["guide"]["image"] + .01]))
            self.audio = SimpleNamespace(VAEDecodeAudio=SimpleNamespace(
                execute=lambda *_: pytest.fail("Source soundtrack must not be replaced")))

        def load(self, config, progress):
            calls["config"] = config
            assert config["lora"] == job["config"]["lora"]  # Still uses the normal/bypass LoRA loader.
            return "model", "clip", "video-vae", "audio-vae"

        def text_condition(self, **kwargs):
            calls["text"] = kwargs
            return "plain-positive", {"samples": "empty-av"}

        def guide(self, **kwargs):
            calls["guide"] = kwargs
            return ["guided-positive"]

        def generate(self, config, model, positive, latent, progress):
            assert positive == "guided-positive" and latent == {"samples": "empty-av"}
            progress("sampling", 1)
            calls["sampling_status"] = json.loads(Path(job["status_path"]).read_text())
            return {"samples": "sampled-av"}

    def mux(path, frames, audio):
        calls["mux_audio"] = audio
        assert len(frames) == count
        assert torch.equal(frames, source + .01)  # Every edited frame survives; padding does not.
        Path(path).write_bytes(b"fixture-MP4")

    worker.run_job(job, runtime_factory=Runtime, muxer=mux)
    assert set(calls["text"]) == {"clip", "vae", "prompt", "width", "height", "length"}
    assert calls["text"]["length"] == sampling_count
    assert calls["text"]["prompt"].startswith("vfx_edit: ")
    guide = calls["guide"]
    assert guide["positive"] == "plain-positive" and guide["latent"] == {"samples": "empty-av"}
    assert guide["frame_idx"] == 0 and guide["vae"] == "video-vae" and guide["audio_vae"] == "audio-vae"
    assert len(guide["image"]) == sampling_count
    assert torch.equal(guide["image"][:count], source)
    assert guide["audio"]["waveform"].count_nonzero() == 0
    mux_audio = calls["mux_audio"]["waveform"]
    assert mux_audio.shape[-1] == round(count / 24 * worker.AUDIO_RATE)
    if soundtrack:
        assert torch.equal(mux_audio, original_audio["waveform"])
    else:
        assert mux_audio.count_nonzero() == 0
    final = json.loads(Path(job["status_path"]).read_text())
    for status in (calls["sampling_status"], final):
        assert {k: status[k] for k in ("frames", "sampling_frames", "fps", "width", "height", "audio_mode")} == {
            "frames": count, "sampling_frames": sampling_count,
            "fps": 24, "width": 32, "height": 64, "audio_mode": "source"}
    assert final["phase"] == "completed" and final["duration_seconds"] == count / 24


def test_generic_reference_adapter_still_uses_native_references_and_generated_audio(job):
    config, _ = worker.validate_job(job)
    config["lora"] = "/models/turbo.safetensors"
    config["loras"] = [{"path": config["lora"], "strength": 1.0}]
    calls = {}
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.h3 = SimpleNamespace(MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=lambda **kw: (
        calls.update(reference=kw) or ("positive", "latent"))))
    runtime.nodes = SimpleNamespace(VAEDecode=lambda: SimpleNamespace(decode=lambda *_: ["frames"]))
    runtime.audio = SimpleNamespace(VAEDecodeAudio=SimpleNamespace(execute=lambda *_: ["generated-audio"]))
    prepared = {"ref_images": {}, "ref_videos": {"ref_video_1": "video"},
                "ref_video_audios": {"ref_video_audio_1": "sound"}, "ref_audios": {}}
    assert runtime.condition(config, prepared, "clip", "vae", "audio-vae") == ("positive", "latent")
    assert calls["reference"]["ref_videos"] == prepared["ref_videos"]
    assert calls["reference"]["ref_video_audios"] == prepared["ref_video_audios"]
    assert runtime.decode("samples", "vae", "audio-vae", lambda *_: None) == ("frames", "generated-audio")
