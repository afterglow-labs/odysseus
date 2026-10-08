"""CPU checks for H3 job boundaries, real media encoding, and core argument flow."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


SPEC = importlib.util.spec_from_file_location("h3_video_worker", Path(__file__).parents[1] / "scripts/h3_video_worker.py")
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


@pytest.fixture
def job(tmp_path):
    config = {"mode": "t2va", "prompt": "A red ball rolls across a table.", "seed": 123,
              "gpu": "GPU-00000000-0000-0000-0000-000000000001", "width": 64, "height": 32,
              "frames": 124, "steps": 4}
    for name in ("model", "encoder", "video_vae", "audio_vae"):
        path = tmp_path / f"{name}.safetensors"
        path.touch()
        config[name] = str(path)
    return {"config": config, "uploads": {}, "output_path": str(tmp_path / "result.mp4"),
            "status_path": str(tmp_path / "status.json"), "runtime_path": str(tmp_path / "core")}


def test_validate_preserves_full_64_bit_seed_and_snap_grid(job):
    job["config"].update(seed=2**64 - 1, frames=125)
    config, _ = worker.validate_job(job)
    assert config["frames"] == 141
    assert config["seed"] == 2**64 - 1
    assert config["gpu"].startswith("GPU-")
    job["config"]["model"] = job["config"]["model"].replace(".safetensors", ".gguf")
    with pytest.raises(ValueError, match="safetensors"):
        worker.validate_job(job)


@pytest.mark.parametrize("updates,match", [({"width": 33}, "multiple of 32"),
                                         ({"width": 2048, "height": 2048}, "pixels"),
                                         ({"steps": 0}, "steps"),
                                         ({"seed": -1}, "seed"),
                                         ({"shift_audio": float("nan")}, "shift_audio"),
                                         ({"gpu": "0,1"}, "GPU"),
                                         ({"mode": "fl2va"}, "first frame"),
                                         ({"mode": "ref2va"}, "at least one")])
def test_invalid_jobs_fail_before_ml_initialization(job, updates, match):
    job["config"].update(updates)
    with pytest.raises(ValueError, match=match):
        worker.run_job(job, runtime_factory=lambda _: pytest.fail("must not initialize GPU"))


def test_audio_only_reference_rejected_and_top_level_upload_contract(job, tmp_path):
    audio = tmp_path / "reference.wav"
    audio.touch()
    del job["uploads"]
    job["config"]["mode"] = "ref2va"
    job["reference_audio"] = [str(audio)]
    with pytest.raises(ValueError, match="audio alone"):
        worker.validate_job(job)
    image = tmp_path / "reference.png"
    image.touch()
    job["reference_images"] = [str(image)]
    _, media = worker.validate_job(job)
    assert media["reference_images"] == [str(image)]
    assert media["reference_audio"] == [str(audio)]


def test_run_sets_gpu_before_core_calls_and_publishes_complete_file(job):
    events = []
    class FakeRuntime:
        def __init__(self, path):
            assert os.environ["CUDA_VISIBLE_DEVICES"] == job["config"]["gpu"]
            events.append("init")
        def load(self, config, progress):
            events.append("load")
            return "model", "clip", "video-vae", "audio-vae"
        def condition(self, config, prepared, clip, vae, audio_vae):
            assert prepared == "prepared"
            return "positive", "av-latent"
        def generate(self, config, model, positive, latent, progress):
            assert (positive, latent) == ("positive", "av-latent")
            progress("sampling", 2)
            state = json.loads(Path(job["status_path"]).read_text())
            assert state["step"] == 2 and state["total_steps"] == 4
            return "sampled-av"
        def decode(self, samples, vae, audio_vae, progress):
            assert samples == "sampled-av"
            return [0] * 124, "audio"
    def prepare(*_):
        assert events == ["init"]
        events.append("media")
        return "prepared"
    def mux(path, frames, audio):
        assert json.loads(Path(job["status_path"]).read_text())["phase"] == "encoding_mp4"
        Path(path).write_bytes(b"test MP4")
    worker.run_job(job, runtime_factory=FakeRuntime, media_loader=prepare, muxer=mux)
    assert events == ["init", "media", "load"]
    state = json.loads(Path(job["status_path"]).read_text())
    assert state["phase"] == "completed"
    assert state["duration_seconds"] == 124 / 24
    assert not list(Path(job["status_path"]).parent.glob("*.tmp"))


def test_core_reference_pairing_sampler_cfg_and_seed(job):
    config, _ = worker.validate_job(job)
    config["mode"] = "ref2va"
    recorded = {}
    def ref(**kwargs):
        recorded["reference"] = kwargs
        return ["positive", {"samples": "nested-av"}]
    def shifted(model, video, audio):
        recorded["shifts"] = (video, audio)
        return ["shifted"]
    def noise(latent, seed):
        assert latent == "nested-av" and seed == 123
        return "seeded-noise"
    def sample(*args, **kwargs):
        recorded["sample"] = (args, kwargs)
        kwargs["callback"](0, None, None, 4)
        return "result"
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.h3 = SimpleNamespace(MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=ref),
                                MiniMaxH3SigmaShift=SimpleNamespace(execute=shifted))
    runtime.sample = SimpleNamespace(prepare_noise=noise, sample=sample)
    prepared = {"ref_images": {"ref_image_1": "image"}, "ref_videos": {"ref_video_1": "clipframes"},
                "ref_video_audios": {"ref_video_audio_1": "paired-sound"}, "ref_audios": {"ref_audio_1": "audio"}}
    positive, latent = runtime.condition(config, prepared, "clip", "vae", "audio-vae")
    updates = []
    result = runtime.generate(config, "model", positive, latent, lambda *args: updates.append(args))
    assert recorded["reference"]["ref_video_audios"] == {"ref_video_audio_1": "paired-sound"}
    assert recorded["reference"]["ref_image_size"] == "match"
    assert recorded["shifts"] == (12, 3)
    args, kwargs = recorded["sample"]
    assert args[3] == 1.0 and args[7] == []  # H3 fixed CFG and no negative prompt
    assert kwargs["seed"] == 123
    assert updates == [("sampling", 1)]
    assert result["samples"] == "result"


def lora_runtime(*, quant_format=None, patches=None, injections=None):
    """Exercise the loader boundary without importing Comfy or opening CUDA."""
    base = SimpleNamespace(model=SimpleNamespace(modules=lambda: iter([
        SimpleNamespace(quant_format=quant_format)])), patches={})
    updated = SimpleNamespace(patches=patches or {}, injections=injections or {})
    calls = []
    def loader(kind):
        def load(model, clip, state, strength, clip_strength):
            assert model is base and clip is None and clip_strength == 0
            assert state == {"adapter": "safe tensors"}
            calls.append((kind, strength))
            return updated, None
        return load
    def read(path, *, safe_load):
        assert path == "/models/turbo.safetensors" and safe_load is True
        calls.append("read")
        return {"adapter": "safe tensors"}
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.sd = SimpleNamespace(load_lora_for_models=loader("normal"),
                                 load_bypass_lora_for_models=loader("bypass"))
    runtime.utils = SimpleNamespace(load_torch_file=read)
    config = {"model": "/models/h3.safetensors", "lora": "/models/turbo.safetensors", "lora_scale": 1.0}
    return runtime, base, updated, config, calls


@pytest.mark.parametrize("metadata,named_nvfp4", [("nvfp4", False), (None, True)])
def test_nvfp4_lora_uses_forward_bypass_and_accepts_injections(metadata, named_nvfp4, caplog):
    rt, base, updated, config, calls = lora_runtime(quant_format=metadata, injections={"bypass_lora": [object()]})
    if named_nvfp4:
        config["model"] = "/models/H3_NVFP4.safetensors"
    progress = []
    import logging
    with caplog.at_level(logging.INFO):
        assert rt.load_lora(base, config, progress.append) is updated
    assert calls == ["read", ("bypass", 1.0)]
    assert progress == ["loading_adapter"]
    assert "NVFP4 bypass (base weights unchanged)" in caplog.text
    assert not updated.patches


@pytest.mark.parametrize("metadata", [None, "int8_tensorwise", "float8_e4m3fn"])
def test_non_nvfp4_lora_keeps_normal_weight_patching(metadata):
    rt, base, updated, config, calls = lora_runtime(quant_format=metadata, patches={"layer.weight": [object()]})
    # An NVFP4 text encoder is independent of the transformer's LoRA path.
    config["encoder"] = "/models/qwen_nvfp4.safetensors"
    assert rt.load_lora(base, config, lambda _: None) is updated
    assert calls == ["read", ("normal", 1.0)]


@pytest.mark.parametrize("metadata", [None, "nvfp4"])
def test_empty_lora_rejected_even_with_unrelated_injection(metadata):
    rt, base, _, config, _ = lora_runtime(quant_format=metadata, injections={"unrelated": [object()]})
    with pytest.raises(ValueError, match="no weights compatible"):
        rt.load_lora(base, config, lambda _: None)


def test_bypass_can_load_regular_non_adapter_patches_too():
    rt, base, updated, config, calls = lora_runtime(quant_format="nvfp4", patches={"layer.bias": [object()]})
    assert rt.load_lora(base, config, lambda _: None) is updated
    assert calls == ["read", ("bypass", 1.0)]


@pytest.mark.parametrize("metadata", [None, "nvfp4"])
def test_zero_lora_strength_returns_base_without_loading_or_injecting(metadata):
    rt, base, _, config, calls = lora_runtime(quant_format=metadata)
    config["lora_scale"] = 0
    del rt.sd.load_bypass_lora_for_models
    assert rt.load_lora(base, config, lambda _: pytest.fail("No adapter should load")) is base
    assert calls == []


def test_missing_bypass_runtime_fails_before_adapter_read_instead_of_merging_fp4():
    rt, base, _, config, calls = lora_runtime(quant_format="nvfp4")
    del rt.sd.load_bypass_lora_for_models
    with pytest.raises(RuntimeError, match="needs bypass LoRA support"):
        rt.load_lora(base, config, lambda _: pytest.fail("No adapter should load"))
    assert calls == []


def test_failure_updates_atomic_status_and_exits_nonzero(job, monkeypatch, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(job))
    def fail(*args, **kwargs):
        raise ValueError("Reference video must be 2–15 seconds")
    monkeypatch.setattr(worker, "run_job", fail)
    assert worker.main(["--job", str(manifest)]) == 1
    state = json.loads(Path(job["status_path"]).read_text())
    assert state["phase"] == "failed" and "2–15" in state["error"]
    assert not Path(job["output_path"]).exists()


def test_progress_preserves_completed_sampling_steps_during_decode_and_mux(tmp_path):
    path = tmp_path / "status.json"
    progress = worker.Progress(path, 20)
    progress("loading_model")
    assert json.loads(path.read_text())["step"] == 0
    progress("sampling", 20)
    for phase in ("decoding_video", "decoding_audio", "encoding_mp4"):
        progress(phase)
        status = json.loads(path.read_text())
        assert status["phase"] == phase
        assert status["step"] == status["total_steps"] == 20


def test_real_mp4_mux_preserves_rgb_audio_rate_and_resamples_reference(tmp_path):
    av = pytest.importorskip("av")
    frames = np.zeros((48, 32, 64, 3), dtype=np.float32)
    frames[..., 0] = 1.0
    t = np.arange(64000) / 32000
    waveform = np.stack([np.sin(2 * np.pi * 440 * t), np.sin(2 * np.pi * 880 * t)]).astype(np.float32)[None] * .2
    path = tmp_path / "test.mp4"
    worker.mux_mp4(path, frames, {"waveform": waveform, "sample_rate": 32000})
    with av.open(str(path)) as source:
        assert source.streams.video[0].average_rate == 24
        assert source.streams.audio[0].sample_rate == 32000
        assert source.streams.audio[0].layout.name == "stereo"
        rgb = next(source.decode(video=0)).to_ndarray(format="rgb24")
        assert rgb[..., 0].mean() > 245 and rgb[..., 2].mean() < 8
    decoded, audio, duration, audio_duration = worker.read_video(path)
    assert decoded.shape == (48, 32, 64, 3)
    assert duration == pytest.approx(2, abs=.05)
    assert audio_duration == pytest.approx(2, abs=.05)
    assert audio["sample_rate"] == 32000
    assert audio["waveform"].shape == (1, 2, 64000)
    # Stereo channels must not collapse during conversion.
    assert not np.allclose(audio["waveform"][0, 0], audio["waveform"][0, 1])


def test_30fps_video_without_soundtrack_and_44100_mono_audio(tmp_path):
    av = pytest.importorskip("av")
    path = tmp_path / "silent.mp4"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("libx264", rate=30)
        stream.width, stream.height, stream.pix_fmt = 64, 32, "yuv420p"
        for i in range(60):
            frame = av.VideoFrame.from_ndarray(np.full((32, 64, 3), i * 3, np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    frames, audio, duration, _ = worker.read_video(path)
    assert frames.shape == (48, 32, 64, 3) and audio is None
    assert duration == pytest.approx(2)
    assert frames[-1].mean() > frames[0].mean()
    wav = tmp_path / "mono.wav"
    import wave
    with wave.open(str(wav), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes((np.sin(np.arange(88200) * .1) * 4000).astype("<i2").tobytes())
    audio, duration = worker.read_audio(wav)
    assert duration == 2 and audio["waveform"].shape == (1, 2, 64000)
    assert np.allclose(audio["waveform"][0, 0], audio["waveform"][0, 1])


def test_short_references_and_combined_duration_rejected(tmp_path, job, monkeypatch):
    pytest.importorskip("av")
    short = tmp_path / "short.mp4"
    worker.mux_mp4(short, np.zeros((24, 32, 64, 3), np.float32),
                    {"waveform": np.zeros((1, 2, 32000), np.float32), "sample_rate": 32000})
    with pytest.raises(ValueError, match="2–15"):
        worker.read_video(short)
    config, media = worker.validate_job(job)
    media["reference_videos"] = ["clip1", "clip2"]
    monkeypatch.setattr(worker, "read_video", lambda _: ([0] * 192, None, 8.0, 0.0))
    with pytest.raises(ValueError, match="total at most 15"):
        worker.prepare_media(media, config)


def test_reference_video_and_soundtrack_trim_to_same_model_grid(job, monkeypatch):
    config, media = worker.validate_job(job)
    media["reference_videos"] = ["paired"]
    soundtrack = {"waveform": np.zeros((1, 2, 32000 * 8), np.float32), "sample_rate": 32000}
    monkeypatch.setattr(worker, "read_video", lambda _: ([0] * 192, soundtrack, 8.0, 8.0))
    result = worker.prepare_media(media, config)
    assert len(result["ref_videos"]["ref_video_1"]) == 124
    assert result["ref_video_audios"]["ref_video_audio_1"]["waveform"].shape[-1] == round(124 / 24 * 32000)


@pytest.mark.parametrize("stream", ["video", "audio"])
def test_nonfinite_model_outputs_fail_without_publishing_partial_mp4(tmp_path, stream):
    pytest.importorskip("av")
    frames = np.zeros((5, 32, 64, 3), np.float32)
    waveform = np.zeros((1, 2, 8000), np.float32)
    if stream == "video":
        frames[2, 0, 0, 0] = float("nan")
    else:
        waveform[0, 0, 0] = float("inf")
    output = tmp_path / "result.mp4"
    with pytest.raises(FloatingPointError, match="non-finite"):
        worker.mux_mp4(output, frames, {"waveform": waveform, "sample_rate": 32000})
    assert not output.exists()
    assert not list(tmp_path.glob("*.tmp.mp4"))
