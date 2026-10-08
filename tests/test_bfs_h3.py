"""CPU regressions for the authored H3 identity-reference + source-guide recipe."""
from types import SimpleNamespace

import pytest
import torch

from scripts import bfs_h3


@pytest.fixture
def config():
    return {"workflow_id": "h3_head_swap", "runtime_path": "/unused/core", "lora": "head.safetensors",
            "model": "ref2va.safetensors", "encoder": "qwen.safetensors", "video_vae": "video.safetensors",
            "audio_vae": "audio.safetensors", "width": 64, "height": 32, "frames": 56, "seed": 83,
            "prompt": "Keep the actor's red coat."}


@pytest.fixture
def media():
    return {"identity_image": torch.ones((1, 32, 32, 3)),
            "source_video": torch.rand((50, 32, 64, 3)),
            "source_audio": {"waveform": torch.ones((1, 2, 96000)), "sample_rate": 48000}}


class FakeRuntime:
    def __init__(self):
        self.events = []
        self.calls = {}
        self.incompatible = set()
        self.utils = SimpleNamespace(load_torch_file=lambda path, **kw: path,
                                     common_upscale=self.upscale)
        self.sd = SimpleNamespace(load_lora_for_models=self.lora)
        self.h3 = SimpleNamespace(MiniMaxH3ReferenceToVideo=SimpleNamespace(execute=self.reference),
                                  MiniMaxH3AddGuide=SimpleNamespace(execute=self.guide),
                                  MiniMaxH3SigmaShift=SimpleNamespace(execute=self.shift))
        self.sample = SimpleNamespace(prepare_noise=lambda latent, seed: ("noise", seed), sample=self.sample_model)
        self.nodes = SimpleNamespace(VAEDecode=lambda: SimpleNamespace(decode=self.decode))

    @staticmethod
    def upscale(image, width, height, method, crop):
        assert method == "lanczos" and crop == "disabled"
        return torch.nn.functional.interpolate(image, (height, width), mode="nearest")

    def load(self, config, progress):
        assert not config.get("lora"), "The speed LoRA must be applied before the head-swap LoRA"
        self.events.append("load")
        return SimpleNamespace(patches={}), "clip", "video-vae", "audio-vae"

    def lora(self, model, clip, state, strength, strength_clip):
        assert clip is None and strength_clip == 0
        self.events.append(("lora", state, strength))
        patches = {key: list(values) for key, values in model.patches.items()}
        if state not in self.incompatible:
            patches.setdefault("weight", []).append((state, strength))
        return SimpleNamespace(patches=patches), None

    def reference(self, **kwargs):
        self.calls["reference"] = kwargs
        return "identity-positive", {"samples": "empty-av"}

    def guide(self, **kwargs):
        self.calls["guide"] = kwargs
        return ["guided-positive"]

    def shift(self, model, video, audio):
        self.calls["shift"] = (video, audio)
        return [model]

    def sample_model(self, *args, **kwargs):
        self.calls["sample"] = args, kwargs
        kwargs["callback"](args[2] - 1, None, None, args[2])
        return "sampled-av"

    generate = bfs_h3.H3Runtime.generate

    def decode(self, vae, samples):
        assert vae == "video-vae" and samples == {"samples": "sampled-av"}
        return ["decoded-rgb"]


def test_authored_recipe_uses_identity_reference_and_source_guide(config, media):
    runtime = FakeRuntime()
    progress = []
    frames, audio = bfs_h3.generate(config, media, lambda *a, **kw: progress.append((a, kw)), runtime=runtime)
    ref = runtime.calls["reference"]
    assert set(ref) == {"clip", "vae", "audio_vae", "prompt", "width", "height", "length", "ref_image_size", "ref_images"}
    assert "ref_videos" not in ref  # Source is a guide, never a second generic reference.
    assert ref["length"] == 39  # Trim BOTH guide and target to the available 17k+5 source grid.
    assert ref["prompt"].startswith("head_swap:") and config["prompt"] in ref["prompt"]
    identity = ref["ref_images"]["ref_image_0"]
    assert identity.shape == (1, 32, 64, 3)
    assert identity[:, :, :16].count_nonzero() == 0  # Identity is padded, never stretched.
    assert torch.all(identity[:, :, 16:48] == 1)
    guide = runtime.calls["guide"]
    assert guide["positive"] == "identity-positive" and guide["latent"] == {"samples": "empty-av"}
    assert guide["frame_idx"] == 0 and torch.equal(guide["image"], media["source_video"][:39])
    assert guide["audio"] is audio
    assert audio["sample_rate"] == 48000 and audio["waveform"].shape == (1, 2, 78000)
    assert frames == "decoded-rgb"
    assert runtime.calls["shift"] == (12, 3)
    args, kwargs = runtime.calls["sample"]
    assert args[2:6] == (20, 1.0, "euler", "beta")
    assert args[6:9] == ("guided-positive", [], "empty-av")
    assert kwargs["seed"] == 83
    assert (("sampling", 20), {}) in progress
    assert config["frames"] == 56  # Caller's persisted settings are not mutated.


def test_speed_adapter_precedes_head_swap_and_source_audio_is_not_generated(config, media):
    config.update(speed_lora="speed.safetensors", speed_lora_scale=.8, lora_scale=.9, audio_mode="silent")
    runtime = FakeRuntime()
    _, audio = bfs_h3.generate(config, media, lambda *a, **kw: None, runtime=runtime)
    assert runtime.events == ["load", ("lora", "speed.safetensors", .8), ("lora", "head.safetensors", .9)]
    assert runtime.calls["sample"][0][2] == 8
    assert audio["sample_rate"] == 32000 and audio["waveform"].shape == (1, 2, 52000)
    assert audio["waveform"].count_nonzero() == 0
    assert runtime.calls["guide"]["audio"] is audio


def test_mismatched_head_swap_adapter_cannot_hide_behind_speed_adapter(config, media):
    config["speed_lora"] = "speed.safetensors"
    runtime = FakeRuntime()
    runtime.incompatible.add("head.safetensors")
    with pytest.raises(ValueError, match="head swap adapter has no compatible"):
        bfs_h3.generate(config, media, lambda *a, **kw: None, runtime=runtime)
    assert "reference" not in runtime.calls and "sample" not in runtime.calls


def test_source_audio_is_padded_without_mutating_source_and_mono_becomes_stereo():
    original = torch.tensor([[[.1, .2, .3]]])
    audio = bfs_h3._guide_audio({"waveform": original, "sample_rate": 24}, "source", 5)
    assert audio["waveform"].shape == (1, 2, 5)
    assert torch.equal(audio["waveform"][0, 0], audio["waveform"][0, 1])
    assert audio["waveform"][..., 3:].count_nonzero() == 0
    assert original.shape == (1, 1, 3)


@pytest.mark.parametrize("change,match", [({"workflow_id": "other"}, "workflow"),
                                          ({"lora": None}, "head-swap"),
                                          ({"scheduler": "invented"}, "scheduler"),
                                          ({"lora_scale": float("nan")}, "finite"),
                                          ({"reference_size": "invented"}, "reference size"),
                                          ({"second_pass": True}, "second-pass"),
                                          ({"audio_mode": "generated"}, "audio mode")])
def test_bad_recipe_fails_before_runtime_initialization(config, change, match):
    config.update(change)
    with pytest.raises(ValueError, match=match):
        bfs_h3.generate(config, {}, lambda *a, **kw: None,
                        runtime_factory=lambda _: pytest.fail("No runtime should be initialized"))


def test_complete_prompt_is_preserved_and_notes_are_not_claimed_as_observed_details():
    prompt = "head_swap:\nKeep the camera fixed."
    assert bfs_h3.build_prompt(prompt) == prompt
    default = bfs_h3.build_prompt()
    assert "<Picture 1>" in default and "<Video 1>" in default
    assert "Facial details" not in default
    assert bfs_h3.build_prompt("Keep the blue shirt.").endswith("Additional user instructions:\nKeep the blue shirt.")
