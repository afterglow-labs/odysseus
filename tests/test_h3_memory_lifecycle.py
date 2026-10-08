"""CPU checks for native H3 cleanup and targeted encoder offload."""
import ast
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch

from scripts import h3_video_worker as worker


@pytest.fixture
def runtime():
    core = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy/model_management.py"
    if not core.is_file():
        pytest.skip("Pinned H3 inference checkout is not installed")
    events = []

    class Loaded:
        def __init__(self, name, device, clone_id):
            self.name, self.device = name, torch.device(device)
            self.model = SimpleNamespace(clone_base_uuid=clone_id, model=object(), is_dynamic=lambda: True)
            self.currently_used = True

        def is_dead(self):
            return False

        def model_offloaded_memory(self):
            return 0

        def model_memory(self):
            return 100

        def model_unload(self, memory_to_free):
            events.append(("unload", self.name))
            return True

    encoder = Loaded("encoder", "cuda:0", "qwen")
    clone = Loaded("encoder-clone", "cuda:0", "qwen")
    video = Loaded("video-vae", "cuda:1", "video")
    audio = Loaded("audio-vae", "cuda:1", "audio")
    transformer = Loaded("transformer", "cuda:0", "h3")
    loaded = [encoder, clone, video, audio, transformer]
    # Execute the pinned core's real selection/removal code. CUDA accounting
    # and model transfers are simulated so this never initializes a GPU.
    namespace = dict(current_loaded_models=loaded, torch=torch, sys=sys, logging=logging,
                     cleanup_models_gc=lambda: None, get_torch_device=lambda: torch.device("cuda:0"),
                     get_free_memory=lambda _: 0, DISABLE_SMART_MEMORY=False,
                     detail=lambda *a: None, soft_empty_cache=lambda: events.append("empty-cache"))
    functions = [node for node in ast.parse(core.read_text()).body
                 if isinstance(node, ast.FunctionDef)
                 and node.name in {"free_memory", "unload_model_and_clones"}]
    assert len(functions) == 2
    future = ast.parse("from __future__ import annotations").body
    exec(compile(ast.Module(body=future + functions, type_ignores=[]), str(core), "exec"), namespace)
    rt = worker.H3Runtime.__new__(worker.H3Runtime)
    rt._conditioning_patcher = encoder.model
    rt.model_management = SimpleNamespace(
        reset_cast_buffers=lambda: events.append("reset-cast-buffers"),
        unload_model_and_clones=namespace["unload_model_and_clones"],
        soft_empty_cache=namespace["soft_empty_cache"])
    rt.model_prefetch = SimpleNamespace(cleanup_prefetch_queues=lambda: events.append("cleanup-prefetch"))
    rt.model_vbar = SimpleNamespace(vbars_reset_watermark_limits=lambda: events.append("reset-watermarks"))
    return rt, events, loaded


def test_conditioning_cleanup_unloads_only_encoder_and_clones(runtime):
    rt, events, loaded = runtime
    rt.release_conditioning()
    assert events[:3] == ["cleanup-prefetch", "reset-cast-buffers", "reset-watermarks"]
    assert {item for item in events if isinstance(item, tuple)} == {
        ("unload", "encoder"), ("unload", "encoder-clone")}
    assert [model.name for model in loaded] == ["video-vae", "audio-vae", "transformer"]
    assert all(model.currently_used for model in loaded)
    assert events[-1] == "empty-cache"
    assert rt._conditioning_patcher is None
    events.clear()
    rt.release_conditioning()
    assert events == ["cleanup-prefetch", "reset-cast-buffers", "reset-watermarks"]


def test_sampler_runs_between_cleanup_boundaries_preserving_conditioning(runtime, monkeypatch):
    rt, events, loaded = runtime
    config = dict(shift_video=12, shift_audio=3, seed=666, steps=8, sampler="euler", scheduler="simple")
    positive, latent_values, samples = object(), object(), object()
    latent = {"samples": latent_values, "metadata": object()}
    monkeypatch.setattr(worker.H3Runtime, "log_gpu_memory", lambda _, phase: events.append(phase))
    rt.h3 = SimpleNamespace(MiniMaxH3SigmaShift=SimpleNamespace(execute=lambda *a: ["shifted"]))

    def noise(values, seed):
        assert values is latent_values and seed == 666
        events.append("prepare-noise")
        return "noise"

    def sample(*args, **kwargs):
        assert args == ("shifted", "noise", 8, 1.0, "euler", "simple", positive, [], latent_values)
        assert kwargs["seed"] == 666
        assert [model.name for model in loaded] == ["video-vae", "audio-vae", "transformer"]
        events.append("sample")
        return samples

    rt.sample = SimpleNamespace(prepare_noise=noise, sample=sample)
    result = rt.generate(config, "model", positive, latent, lambda *args: None)
    assert result["samples"] is samples and result["metadata"] is latent["metadata"]
    assert latent["samples"] is latent_values
    assert events[0] == "after conditioning"
    assert events.index("after conditioning cleanup") < events.index("prepare-noise")
    assert events[-4:] == ["sample", "cleanup-prefetch", "reset-cast-buffers", "reset-watermarks"]


def test_render_failure_logs_traceback_without_manifest_or_prompt(tmp_path, monkeypatch, caplog):
    private_prompt = "private-prompt-sentinel-87654321"
    private_attachment = "private-upload-sentinel-12345678"
    status = tmp_path / "status.json"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"config": {"prompt": private_prompt, "steps": 8},
                                    "reference_images": [private_attachment], "status_path": str(status)}))

    def failing_sampler(_):
        raise RuntimeError("simulated sampler allocation failure")

    monkeypatch.setattr(worker, "run_job", failing_sampler)
    with caplog.at_level(logging.ERROR):
        assert worker.main(["--job", str(manifest)]) == 1
    assert "Traceback (most recent call last)" in caplog.text
    assert "failing_sampler" in caplog.text
    assert private_prompt not in caplog.text and private_attachment not in caplog.text
    assert json.loads(status.read_text())["error"] == "RuntimeError: simulated sampler allocation failure"
