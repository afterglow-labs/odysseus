"""BFS recipe regressions, including a weight-free pinned-core overlap forward."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import torch

from scripts import bfs_ltx_wan as bfs

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = PROJECT / "runtimes/minimax-h3/ComfyUI"


def config(workflow="ltx25_v1", **values):
    family = bfs.WORKFLOWS[workflow]
    return dict(workflow_id=workflow, family=family, model="ltx-2.5-distilled.safetensors",
                encoder="encoder.safetensors", video_vae="video.safetensors", audio_vae="audio.safetensors",
                connector="projection.safetensors", lora="head_swap_v1_13500_first_frame.safetensors",
                model_low="low.safetensors", lora_low="low-lora.safetensors", width=64, height=32,
                frames=17, steps=8, seed=42, cfg=1, prompt="Keep the actor's blue jacket.",
                runtime_path=str(RUNTIME)) | values


def media():
    return dict(source_video=torch.full((18, 32, 64, 3), .25),
                identity_image=torch.ones((1, 32, 32, 3)),
                source_audio={"waveform": torch.ones((1, 2, 24000)), "sample_rate": 32000})


class FakeBackend:
    def __init__(self, runtime, config):
        self.config, self.calls = config, []
        runtime.append(self)

    def load(self, progress):
        self.calls.append(("load",))
        return "base", "low" if self.config["family"] == "wan22" else None, "positive", "negative"

    def empty_video(self):
        return {"samples": "video"}

    def keyframe(self, latent, image, last=False):
        self.calls.append(("keyframe", last, image))
        return latent

    def add_guide(self, positive, negative, latent, guide):
        self.calls.append(("guide", guide))
        return positive, negative, latent

    def overlap(self, model, positive, negative, latent, source, identity):
        self.calls.append(("overlap", source, identity))
        return model, positive, negative, latent

    def bernini(self, positive, negative, source, identity):
        self.calls.append(("bernini", source, identity))
        return positive, negative, {"samples": "video"}

    def av_latent(self, latent, audio):
        self.calls.append(("audio", audio))
        return latent

    def sample(self, model, positive, negative, latent, progress, *, low):
        self.calls.append(("sample", low))
        return latent

    def decode(self, *args):
        return torch.zeros((self.config["frames"], self.config["height"], self.config["width"], 3))


def run(c, m):
    runtimes, events = [], []
    output = bfs.generate(c, m, lambda *args, **kw: events.append((args, kw)), runtime=runtimes,
                          backend_factory=FakeBackend)
    return output, runtimes[0], events


@pytest.mark.parametrize("workflow", ["ltx25_v1", "ltx25_v11"])
def test_ltx25_uses_separate_identity_and_motion_without_keyframe_or_panel(workflow):
    m = media()
    (frames, audio), backend, events = run(config(workflow), m)
    assert frames.shape == (17, 32, 64, 3)
    assert audio is m["source_audio"]
    calls = [call[0] for call in backend.calls]
    assert "overlap" in calls and "guide" not in calls and "keyframe" not in calls
    overlap = next(call for call in backend.calls if call[0] == "overlap")
    assert torch.equal(overlap[1], m["source_video"][:17])
    assert torch.equal(overlap[2], m["identity_image"])
    assert backend.config["frames"] == 17


def test_v2_masks_only_selected_head_region_and_keeps_identity_separate():
    m = media()
    mask = torch.zeros_like(m["source_video"])
    mask[:, 8:16, 12:24] = 1
    m["mask_video"] = mask
    _, backend, _ = run(config("ltx2_v2"), m)
    guide = next(call[1] for call in backend.calls if call[0] == "guide")
    assert torch.equal(guide[:, 8:16, 12:24], torch.tensor([1., 0., 1.]).expand(17, 8, 12, 3))
    assert torch.equal(guide[:, :8], m["source_video"][:17, :8])
    assert torch.equal(m["source_video"], torch.full_like(m["source_video"], .25)), "Source uploads are never mutated"
    assert next(call for call in backend.calls if call[0] == "keyframe")[1] is False


def test_v1_last_frame_uses_only_matching_trained_adapter():
    m = media()
    m["last_frame"] = torch.full((1, 32, 64, 3), .8)
    with pytest.raises(ValueError, match="first-and-last"):
        run(config("ltx2_v1"), m)
    _, backend, _ = run(config("ltx2_v1", lora="head_swap_v1_8750_first_and_last_frame.safetensors"), m)
    assert [call[1] for call in backend.calls if call[0] == "keyframe"] == [False, True]
    del m["last_frame"]
    with pytest.raises(ValueError, match="requires both"):
        run(config("ltx2_v1", lora="head_swap_v1_8750_first_and_last_frame.safetensors"), m)


def test_wan_uses_both_experts_and_no_ltx_audio_conditioner():
    (_, audio), backend, _ = run(config("wan22_head_swap", frames=18), media())
    assert backend.config["frames"] == 17
    assert ("sample", "low") in backend.calls
    assert "bernini" in [c[0] for c in backend.calls]
    assert "audio" not in [c[0] for c in backend.calls]
    assert audio is not None


def test_silent_mode_keeps_silent_output():
    (frames, audio), _, _ = run(config(audio_mode="silent"), media())
    assert len(frames) == 17 and audio is None


@pytest.mark.parametrize("values,error", [
    ({"family": "wan22"}, "supported"), ({"width": 63}, "multiples"),
    ({"steps": True}, "steps"), ({"cfg": float("nan")}, "cfg"),
    ({"model": "ltx-2.5-dev.safetensors"}, "distilled"), ({"fps": 30}, "24 fps"),
    ({"audio_vae": None}, "audio_vae"), ({"prompt": []}, "prompt"),
])
def test_bad_settings_fail_before_model_loading(values, error):
    runtime = []
    with pytest.raises(ValueError, match=error):
        bfs.generate(config(**values), media(), lambda *a, **kw: None, runtime=runtime, backend_factory=FakeBackend)
    assert not runtime


@pytest.mark.parametrize("case", ["missing", "short", "blank", "size"])
def test_bad_masks_fail_before_model_loading(case):
    m = media()
    if case != "missing":
        m["mask_video"] = torch.ones((8 if case == "short" else 17, 16 if case == "size" else 32, 64, 3))
        if case == "blank":
            m["mask_video"].zero_()
    runtimes = []
    with pytest.raises(ValueError, match="mask"):
        bfs.generate(config("ltx2_v2"), m, lambda *a, **kw: None, runtime=runtimes, backend_factory=FakeBackend)
    assert not runtimes


def test_bernini_positive_and_negative_share_guide_but_not_identity():
    backend = object.__new__(bfs.CoreBackend)
    backend.torch, backend.management = torch, SimpleNamespace(intermediate_device=lambda: "cpu")
    encoded = []
    def encode(image):
        tensor = torch.tensor([float(image.mean())])
        encoded.append(tensor)
        return tensor
    backend.vae = SimpleNamespace(encode=encode)
    backend.helpers = SimpleNamespace(conditioning_set_values=lambda cond, values: [cond, values])
    backend.runtime = SimpleNamespace(utils=SimpleNamespace(common_upscale=lambda *a: pytest.fail("Already aligned")))
    m = media()
    positive, negative, latent = backend.bernini("p", "n", m["source_video"][:17], m["identity_image"])
    assert positive[1]["context_latents"] == encoded
    assert negative[1]["context_latents"] == [encoded[0]]
    assert latent["samples"].shape == (1, 16, 5, 4, 8)


def test_wan_second_expert_continues_latent_without_adding_noise():
    backend = object.__new__(bfs.CoreBackend)
    backend.config, backend.torch = config("wan22_head_swap", steps=6, cfg=4), torch
    calls = []
    def sample(model, noise, cfg, sampler, sigmas, positive, negative, latent, **kw):
        calls.append((model, noise.clone(), sigmas.clone(), latent.clone()))
        for step in range(len(sigmas) - 1):
            kw["callback"](step, None, None, len(sigmas) - 1)
        return latent + 10
    backend.runtime = SimpleNamespace(sample=SimpleNamespace(
        prepare_noise=lambda latent, seed: torch.ones_like(latent),
        prepare_empty_noise=torch.zeros_like, sample_custom=sample))
    backend.samplers = SimpleNamespace(calculate_sigmas=lambda *a: torch.linspace(1, 0, 7), sampler_object=lambda name: name)
    model = SimpleNamespace(get_model_object=lambda name: "sampling")
    progress = []
    result = backend.sample(model, "positive", "negative", {"samples": torch.zeros(1)},
                            lambda phase, step: progress.append(step), low="low")
    assert len(calls) == 2 and calls[1][0] == "low"
    assert calls[0][2][-1] == calls[1][2][0]
    assert calls[1][1].eq(0).all() and calls[1][3].eq(10).all()
    assert result["samples"].eq(20).all() and progress == [1, 2, 3, 4, 5, 6]


def test_v3_uses_author_persistent_green_panel_composition():
    if not (RUNTIME.parent / "bfs_nodes/nodes.py").exists():
        pytest.skip("Private BFS helper runtime not installed")
    source = torch.full((9, 128, 256, 3), .2)
    identity = torch.full((1, 32, 32, 3), .8)
    result = bfs.compose_panel(source, identity, RUNTIME)
    assert result.shape == source.shape
    assert torch.equal(result[0], result[-1]), "Reference panel must persist on every frame"
    assert result[0, 0, 0].tolist() == [0., 1., 0.]
    assert result[0, :, :64].amax() == 1
    assert source.eq(.2).all()


def test_pinned_overlap_and_bernini_cpu_forward_keep_output_geometry():
    """Exercise actual projection, source-tagged RoPE and compressed timestep trimming.

    No model files or CUDA are loaded; the tiny one-layer model uses CPU-only
    random-initialized shapes. A helper/core mismatch must fail this test.
    """
    if not (RUNTIME.parent / "bfs_nodes/UPSTREAM.json").exists():
        pytest.skip("Private BFS helper runtime not installed")
    code = '''
import sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import comfy.options
comfy.options.enable_args_parsing(False)
import comfy.cli_args
comfy.cli_args.args.cpu = True
import torch, comfy.ops
from comfy.ldm.lightricks.av_model import LTXAVModel
from bfs_nodes.ltx_identity_overlap import _install_patches
m = LTXAVModel(cross_attention_dim=48, audio_cross_attention_dim=48,
    attention_head_dim=24, audio_attention_head_dim=24, num_attention_heads=2,
    audio_num_attention_heads=2, caption_channels=32, num_layers=1,
    operations=comfy.ops.disable_weight_init, device='cpu', dtype=torch.float32)
for p in m.parameters(): torch.nn.init.zeros_(p)
_install_patches(m)
x = [torch.zeros(1,128,2,2,3), torch.zeros(1,8,3,16)]
refs = [{'latent':torch.zeros(1,128,t,2,w), 'seg_value':float(i),
         'layout':'overlap', 'strata_slot':i-1, 'downscale_factor':1}
        for i,t,w in [(1,2,3),(2,1,2)]]
with torch.inference_mode():
    out = m._forward(x, torch.ones(1), torch.zeros(1,2,64), None,
        frame_rate=24, transformer_options={'_id_ref_specs':refs}, a_timestep=torch.ones(1))
assert [tuple(v.shape) for v in out] == [tuple(v.shape) for v in x]
assert all(v.isfinite().all() for v in out)
assert m._id_blocks == [(12,12,1.), (24,4,2.)]
print('overlap-forward-ok')
from comfy.ldm.wan.model import WanModel
w = WanModel(dim=48, ffn_dim=96, freq_dim=16, text_dim=16, num_heads=4,
    num_layers=1, operations=comfy.ops.disable_weight_init, device='cpu', dtype=torch.float32)
for p in w.parameters(): torch.nn.init.zeros_(p)
target = torch.zeros(1,16,3,4,6)
guide, head = torch.ones_like(target), torch.ones(1,16,1,4,4)*2
source_ids, rope = [], w.rope_encode
def record_rope(*args, **kwargs):
    source_ids.append(kwargs.get('source_id', 0))
    return rope(*args, **kwargs)
w.rope_encode = record_rope
with torch.inference_mode():
    positive = w(target, torch.ones(1), torch.zeros(1,2,16),
        context_latents=[guide,head], transformer_options={})
    negative = w(target, torch.ones(1), torch.zeros(1,2,16),
        context_latents=[guide], transformer_options={})
assert positive.shape == negative.shape == target.shape
assert positive.isfinite().all() and negative.isfinite().all()
assert source_ids == [0,1,2,0,1]
print('bernini-forward-ok')
'''
    result = subprocess.run([sys.executable, "-c", code, str(RUNTIME), str(RUNTIME.parent)],
                            capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    assert "overlap-forward-ok" in result.stdout
    assert "bernini-forward-ok" in result.stdout
