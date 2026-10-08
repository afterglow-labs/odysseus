"""CPU checks for VFX Edit source guides, native refs, timing and source audio."""
import ast
import json
import math
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
    ("minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors", True),
    ("custom_vfx_edit.safetensors", False), ("turbo.safetensors", False), (None, False),
])
def test_only_published_standard_and_ffp_basenames_select_recipe(name, expected):
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
    ({"source_video": None}, "exactly one source"),
    ({"source_video": ["one.mp4", "two.mp4"]}, "exactly one source"),
    ({"first_frame": "first.png"}, "not first/last keyframes"),
    ({"last_frame": "last.png"}, "not first/last keyframes"),
    ({"prompt": "vfx_edit: vfx_edit:"}, "edit instruction"),
    ({"prompt": "x" * 15999}, "16,000 characters"),
])
def test_invalid_vfx_inputs_rejected_before_runtime(job, change, match):
    for key, value in change.items():
        (job["config"] if key in ("mode", "prompt") else job["uploads"])[key] = value
    with pytest.raises(ValueError, match=match):
        worker.run_job(job, runtime_factory=lambda _: pytest.fail("Runtime must not initialize"))


def test_legacy_source_is_migrated_without_changing_manifest(job):
    original = json.loads(json.dumps(job))
    _, media = worker.validate_job(job)
    assert media["source_video"] == job["uploads"]["reference_videos"][0]
    assert media["reference_videos"] == []
    assert job == original


def explicit_reference_job(job, tmp_path, *, ffp=False):
    """Different source, image, video and audio with distinct conditioning roles."""
    source = job["uploads"]["reference_videos"][0]
    files = {}
    for role, name in (("reference_images", "edited-first-frame.png"),
                       ("reference_videos", "reference-motion.mp4"),
                       ("reference_audio", "reference-sound.wav")):
        path = tmp_path / name
        path.touch()
        files[role] = [str(path)]
    job["uploads"] = {"source_video": source, **files}
    if ffp:
        path = tmp_path / "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"
        path.touch()
        job["config"]["lora"] = str(path)
    job["config"]["prompt"] = (
        "Apply the jacket from <Picture 1>, following <Video 1> and <Audio 2>, "
        "while preserving the source camera and background.")
    return job


@pytest.mark.parametrize("ffp", [False, True])
def test_vfx_accepts_native_references_and_preserves_numbered_prompt(job, tmp_path, ffp):
    explicit_reference_job(job, tmp_path, ffp=ffp)
    config, media = worker.validate_job(job)
    assert config["prompt"] == "vfx_edit: " + job["config"]["prompt"]
    assert media["source_video"] == job["uploads"]["source_video"]
    for field in ("reference_images", "reference_videos", "reference_audio"):
        assert media[field] == job["uploads"][field]
    assert worker.is_vfx_edit(config)


def test_reference_image_can_accompany_legacy_source_without_being_discarded(job, tmp_path):
    image = tmp_path / "edited.png"
    image.touch()
    job["uploads"]["reference_images"] = [str(image)]
    _, media = worker.validate_job(job)
    assert media["source_video"] == job["uploads"]["reference_videos"][0]
    assert media["reference_images"] == [str(image)]
    assert media["reference_videos"] == []


def test_distinct_source_is_rejected_without_active_editing_adapter(job, tmp_path):
    explicit_reference_job(job, tmp_path)
    job["config"]["lora_scale"] = 0
    with pytest.raises(ValueError, match="source video requires an active VFX"):
        worker.validate_job(job)


@pytest.mark.parametrize("field,limit", [("reference_images", 9), ("reference_videos", 3), ("reference_audio", 3)])
def test_vfx_reference_count_limits_are_still_enforced(job, tmp_path, field, limit):
    explicit_reference_job(job, tmp_path)
    job["uploads"][field] *= limit + 1
    with pytest.raises(ValueError, match=f"At most {limit}"):
        worker.validate_job(job)


def test_source_does_not_consume_native_reference_budget(job, tmp_path):
    explicit_reference_job(job, tmp_path)
    job["uploads"]["reference_images"] *= 9
    job["uploads"]["reference_videos"] *= 3
    job["uploads"]["reference_audio"] = []
    _, media = worker.validate_job(job)
    assert sum(len(media[field]) for field in ("reference_images", "reference_videos", "reference_audio")) == 12
    assert media["source_video"]
    job["uploads"]["reference_audio"] = [str(tmp_path / "reference-sound.wav")]
    with pytest.raises(ValueError, match="At most 12 references"):
        worker.validate_job(job)


def reference_media(job, tmp_path, monkeypatch, *, ffp=False):
    explicit_reference_job(job, tmp_path, ffp=ffp)
    source, soundtrack = source_media(monkeypatch)
    image = torch.full((1, 16, 8, 3), .25)
    video = torch.full((72, 16, 8, 3), .75)
    paired = {"sample_rate": worker.AUDIO_RATE, "waveform": torch.full((1, 2, 3 * worker.AUDIO_RATE), .125)}
    audio = {"sample_rate": worker.AUDIO_RATE, "waveform": torch.full((1, 2, 2 * worker.AUDIO_RATE), .625)}
    reads = []
    def read_video(path):
        reads.append(path)
        if path == job["uploads"]["source_video"]:
            return source, soundtrack, len(source) / 24, len(source) / 24
        assert path == job["uploads"]["reference_videos"][0]
        return video, paired, 3, 3
    monkeypatch.setattr(worker, "read_video", read_video)
    monkeypatch.setattr(worker, "read_image", lambda path, area: image)
    monkeypatch.setattr(worker, "read_audio", lambda path: (audio, 2))
    return source, soundtrack, image, video, paired, audio, reads


@pytest.mark.parametrize("ffp", [False, True])
def test_source_and_native_reference_channels_stay_distinct(job, tmp_path, monkeypatch, ffp):
    source, soundtrack, image, video, paired, audio, reads = reference_media(job, tmp_path, monkeypatch, ffp=ffp)
    config, media = worker.validate_job(job)
    prepared = worker.prepare_media(media, config)
    assert reads == [media["source_video"], media["reference_videos"][0]]
    assert torch.equal(prepared["guide_video"][:200], source)
    assert len(prepared["guide_video"]) == 209
    assert prepared["ref_images"]["ref_image_1"] is image
    assert torch.equal(prepared["ref_videos"]["ref_video_1"], video[:56])
    assert prepared["ref_video_audios"]["ref_video_audio_1"] is paired
    assert paired["waveform"].shape[-1] == round(56 / 24 * worker.AUDIO_RATE)
    assert prepared["ref_audios"]["ref_audio_1"] is audio
    assert prepared["guide_audio"]["waveform"].count_nonzero() == 0
    assert torch.equal(prepared["source_audio"]["waveform"], soundtrack["waveform"])
    assert soundtrack["waveform"].shape[-1] == round(200 / 24 * worker.AUDIO_RATE)


def test_source_and_reference_duration_budgets_are_independent(job, tmp_path, monkeypatch):
    reference_media(job, tmp_path, monkeypatch)
    # Fifteen seconds of source + fifteen seconds of native video are distinct
    # channels. Native reference videos still share their own 15-second budget.
    native_path = job["uploads"]["reference_videos"][0]
    frames = torch.zeros((360, 16, 8, 3))
    monkeypatch.setattr(worker, "read_video", lambda path: (frames, None, 15, 0))
    job["uploads"]["reference_audio"] = []
    config, media = worker.validate_job(job)
    prepared = worker.prepare_media(media, config)
    assert prepared["output_frames"] == 360 and len(prepared["ref_videos"]["ref_video_1"]) == 345
    media["reference_videos"].append(native_path)
    with pytest.raises(ValueError, match="Reference clips must total at most 15 seconds"):
        worker.prepare_media(media, config)


def pinned_reference_nodes():
    """Execute the installed core's conditioning code with tiny CPU-only VAEs."""
    root = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI"
    if not (root / "comfy_extras/nodes_minimax_h3.py").is_file():
        pytest.skip("Pinned H3 inference checkout is not installed")
    namespace = {"torch": torch, "math": math, "FPS": 24, "AUDIO_LATENT_FPS": 40,
                 "CANVAS_MULTIPLE": 32, "BASE_SHORT_EDGE": 768, "MAX_PIXELS": 768 * 1344,
                 "REF_IMAGE_SHORT_EDGE": 2048, "FRAME_PER_TOKEN": (1, 4, 4, 4, 4), "FRAME_RESCALE": 5 / 3,
                 "io": SimpleNamespace(ComfyNode=object, NodeOutput=lambda *args: args)}
    namespace["comfy"] = SimpleNamespace(
        model_management=SimpleNamespace(intermediate_device=lambda: "cpu"),
        nested_tensor=SimpleNamespace(NestedTensor=lambda tensors: SimpleNamespace(tensors=tensors, is_nested=True)),
        utils=SimpleNamespace(common_upscale=lambda images, width, height, *args:
                              torch.nn.functional.interpolate(images, size=(height, width), mode="nearest")))
    def definitions(path, names):
        module = ast.parse(path.read_text())
        selected = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
        assert {node.name for node in selected} == names
        future = ast.parse("from __future__ import annotations").body
        exec(compile(ast.Module(body=[*future, *selected], type_ignores=[]), str(path), "exec"), namespace)
    definitions(root / "node_helpers.py", {"conditioning_set_values"})
    namespace["node_helpers"] = SimpleNamespace(conditioning_set_values=namespace["conditioning_set_values"])
    definitions(root / "comfy_extras/nodes_minimax_h3.py", {
        "align_frame_count", "video_latent_t", "temporal_shape", "adapt_canvas", "_resize", "_encode_ref_audio",
        "_empty_av_latent", "MiniMaxH3ReferenceToVideo", "MiniMaxH3AddGuide", "MiniMaxH3ImageToVideo"})
    calls = {}
    def tokenize(prompt, **kwargs):
        calls["tokenize"] = (prompt, kwargs)
        return "tokens"
    clip = SimpleNamespace(tokenize=tokenize, encode_from_tokens_scheduled=lambda tokens: [["embeddings", {}]])
    def encode_video(frames):
        return torch.full((1, 24, namespace["video_latent_t"](len(frames)), frames.shape[1] // 16,
                           frames.shape[2] // 16), float(frames.mean()))
    def encode_audio(waveform):
        return torch.full((1, 32, 2, round(waveform.shape[1] / worker.AUDIO_RATE * 40)), float(waveform.mean()))
    return SimpleNamespace(**namespace), clip, SimpleNamespace(encode=encode_video), SimpleNamespace(encode=encode_audio), calls


@pytest.mark.parametrize("ffp", [False, True])
def test_pinned_core_combines_native_refs_with_source_guide_and_preserves_output_audio(
        job, tmp_path, monkeypatch, ffp):
    source, soundtrack, image, video, paired, audio, _ = reference_media(job, tmp_path, monkeypatch, ffp=ffp)
    # The editing adapter need not be first in a multi-LoRA stack.
    turbo = tmp_path / "turbo.safetensors"
    turbo.touch()
    job["config"]["loras"] = [{"path": str(turbo), "strength": .7},
                              {"path": job["config"]["lora"], "strength": 1.0}]
    core, clip, vae, audio_vae, calls = pinned_reference_nodes()
    class Runtime(worker.H3Runtime):
        def __init__(self, path):
            self.h3 = core
            self.nodes = SimpleNamespace(VAEDecode=lambda: SimpleNamespace(
                decode=lambda *_: [torch.cat((source + .01, source[-1:].expand(9, *source.shape[1:])), dim=0)]))
            self.audio = SimpleNamespace(VAEDecodeAudio=SimpleNamespace(
                execute=lambda *_: pytest.fail("Source audio must not be regenerated")))
        def load(self, config, progress):
            assert config["loras"] == job["config"]["loras"]
            return "model", clip, vae, audio_vae
        def generate(self, config, model, positive, latent, progress):
            calls["positive"] = positive
            calls["latent"] = latent
            assert len(positive[0][1]["minimax_refs"]) == 3
            assert len(positive[0][1]["minimax_keyframes"]) == 1
            return {"samples": "sampled"}
    def mux(path, frames, output_audio):
        assert len(frames) == 200
        assert torch.equal(frames, source + .01)
        assert torch.equal(output_audio["waveform"], soundtrack["waveform"])
        Path(path).write_bytes(b"fixture-MP4")
    worker.run_job(job, runtime_factory=Runtime, muxer=mux)
    prompt, tokens = calls["tokenize"]
    assert prompt.startswith("vfx_edit: ") and "<Picture 1>" in prompt
    items = tokens["minimax_ref_items"]
    assert [item["type"] for item in items] == ["image", "audio", "video", "audio"]
    assert torch.all(items[0]["data"] == .25)  # Edited reference image.
    assert torch.all(items[2]["data"] == .75)  # Reference video, never the aligned source.
    conditioning = calls["positive"][0][1]
    assert [item["kind"] for item in conditioning["minimax_refs"]] == ["image", "video_audio", "audio"]
    guide = conditioning["minimax_keyframes"][0]
    assert guide["resolved_frame_index"] == 0
    padded_source = torch.cat((source, source[-1:].expand(9, *source.shape[1:])), dim=0)
    assert float(guide["latent"].mean()) == pytest.approx(float(padded_source.mean()))
    assert guide["audio_latent"].count_nonzero() == 0
    assert json.loads(Path(job["status_path"]).read_text())["audio_mode"] == "source"


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
