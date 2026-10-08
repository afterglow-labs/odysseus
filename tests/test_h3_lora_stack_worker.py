"""CPU coverage for ordered H3 adapters, including pinned NVFP4 bypass hooks."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import h3_video_worker as worker


@pytest.fixture
def job(tmp_path):
    config = {"mode": "t2va", "prompt": "A purple jacket.", "gpu": "0"}
    for name in ("model", "encoder", "video_vae", "audio_vae"):
        path = tmp_path / (name + ".safetensors")
        path.touch()
        config[name] = str(path)
    return {"config": config, "uploads": {}, "output_path": str(tmp_path / "out.mp4")}


def adapter(tmp_path, name="first", strength=1):
    path = tmp_path / (name + ".safetensors")
    path.touch()
    return {"path": str(path), "strength": strength}


def test_order_strength_and_legacy_mirror_are_preserved(job, tmp_path):
    stack = [adapter(tmp_path, "turbo", .6), adapter(tmp_path, "edit", -1.2)]
    job["config"].update(loras=stack, lora="/stale/missing.safetensors", lora_scale="invalid")
    config, _ = worker.validate_job(job)
    assert config["loras"] == stack
    assert config["lora"] == stack[0]["path"]
    assert config["lora_scale"] == .6
    assert config["loras"] is not stack


def test_empty_stack_overrides_stale_legacy_fields(job):
    job["config"].update(loras=[], lora="/missing.safetensors", lora_scale="invalid")
    config, _ = worker.validate_job(job)
    assert config["loras"] == [] and config["lora"] is None
    assert not worker.is_vfx_edit(config)


def test_legacy_scalar_job_becomes_one_adapter(job, tmp_path):
    item = adapter(tmp_path, strength=.75)
    job["config"].update(lora=item["path"], lora_scale=item["strength"])
    config, _ = worker.validate_job(job)
    assert config["loras"] == [item]


@pytest.mark.parametrize("stack", [None, {}, "first", [None], ["first"], [{}], [{"path": 5}]])
def test_malformed_stacks_rejected(job, stack):
    job["config"]["loras"] = stack
    with pytest.raises(ValueError, match="LoRA|loras"):
        worker.validate_job(job)


@pytest.mark.parametrize("strength", [True, False, None, "bad", float("nan"), float("inf"), -4.1, 4.1])
def test_invalid_strength_rejected(job, tmp_path, strength):
    job["config"]["loras"] = [adapter(tmp_path, strength=strength)]
    with pytest.raises(ValueError, match="strength"):
        worker.validate_job(job)


def test_stack_limit_duplicate_and_symlink_duplicate_rejected(job, tmp_path):
    first = adapter(tmp_path)
    job["config"]["loras"] = [adapter(tmp_path, str(i)) for i in range(9)]
    with pytest.raises(ValueError, match="at most 8"):
        worker.validate_job(job)
    for second in (first, {"path": str(tmp_path / "link.safetensors"), "strength": .5}):
        (tmp_path / "link.safetensors").unlink(missing_ok=True)
        (tmp_path / "link.safetensors").symlink_to(first["path"])
        job["config"]["loras"] = [first, second]
        with pytest.raises(ValueError, match="only once"):
            worker.validate_job(job)


def test_vfx_in_second_slot_activates_recipe_but_zero_strength_does_not(job, tmp_path):
    vfx = adapter(tmp_path, worker.VFX_EDIT_LORA.removesuffix(".safetensors"))
    job["config"]["loras"] = [adapter(tmp_path, "turbo"), vfx]
    with pytest.raises(ValueError, match="requires reference mode"):
        worker.validate_job(job)
    vfx["strength"] = 0
    config, _ = worker.validate_job(job)
    assert not worker.is_vfx_edit(config)
    vfx["strength"] = .8
    video = tmp_path / "source.mp4"
    video.touch()
    job["config"]["mode"] = "ref2va"
    job["uploads"]["reference_videos"] = [str(video)]
    config, _ = worker.validate_job(job)
    assert worker.is_vfx_edit(config)
    assert config["prompt"] == "vfx_edit: A purple jacket."


def test_load_passes_each_previous_model_and_skips_zero_without_reading(tmp_path, caplog):
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    base_model = SimpleNamespace(modules=lambda: ())
    base = SimpleNamespace(model=base_model, patches={})
    read, calls = [], []
    def loader(model, clip, state, strength, clip_strength):
        calls.append((model, state, strength))
        patches = {key: list(value) for key, value in model.patches.items()}
        patches.setdefault("weight", []).append((strength, state))
        return SimpleNamespace(model=base_model, patches=patches), None
    runtime.sd = SimpleNamespace(load_lora_for_models=loader)
    runtime.utils = SimpleNamespace(load_torch_file=lambda path, **_: (read.append(path) or path))
    stack = [adapter(tmp_path, "turbo", .5), adapter(tmp_path, "disabled", 0), adapter(tmp_path, "edit", 1.2)]
    config = {"model": "/models/h3.safetensors", "loras": stack,
              "lora": stack[0]["path"], "lora_scale": .5}
    with caplog.at_level(logging.INFO):
        updated = runtime.load_loras(base, config, lambda _: None)
    assert read == [stack[0]["path"], stack[2]["path"]]
    assert calls[0][0] is base
    assert calls[1][0].patches == {"weight": [(.5, stack[0]["path"])]}
    assert updated.patches == {"weight": [(.5, stack[0]["path"]), (1.2, stack[2]["path"])]}
    assert "LoRA 1/3: turbo.safetensors" in caplog.text
    assert "LoRA 3/3: edit.safetensors" in caplog.text
    assert "strength is zero" in caplog.text


def test_incompatible_later_weight_adapter_does_not_pass_due_to_previous_patches():
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    base = SimpleNamespace(model=SimpleNamespace(modules=lambda: ()), patches={"weight": [object()]})
    runtime.sd = SimpleNamespace(load_lora_for_models=lambda *args: (base, None))
    runtime.utils = SimpleNamespace(load_torch_file=lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match="incompatible.safetensors has no weights compatible"):
        runtime.load_lora(base, {"model": "/h3.safetensors", "lora": "/incompatible.safetensors", "lora_scale": 1}, lambda _: None)


def test_composite_rolls_back_partial_hook_installation():
    events = []
    first = SimpleNamespace(inject=lambda _: events.append("first+"), eject=lambda _: events.append("first-"))
    def fail(_):
        events.append("second+")
        raise RuntimeError("failed")
    second = SimpleNamespace(inject=fail, eject=lambda _: events.append("second-"))
    stack = worker._LoRAInjectionStack([first, second])
    with pytest.raises(RuntimeError, match="failed"):
        stack.inject(None)
    assert events == ["first+", "second+", "second-", "first-"]


def isolated_definitions(path, names, namespace):
    """Load pinned pure-Python functions without importing its CUDA initializer."""
    if not path.is_file():
        pytest.skip("Pinned H3 inference checkout is not installed")
    module = ast.parse(path.read_text())
    selected = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names]
    assert {node.name for node in selected} == names
    future = ast.parse("from __future__ import annotations").body
    exec(compile(ast.Module(body=[*future, *selected], type_ignores=[]), str(path), "exec"), namespace)


@pytest.fixture
def pinned_bypass():
    """Use actual pinned hook/loader code, a tiny CPU linear layer and fake I/O."""
    import torch
    class Adapter:
        def __init__(self, value):
            self.weights = (torch.tensor(value, dtype=torch.float32),)
        def bypass_forward(self, *args):
            raise AssertionError("Default bypass should use h()")
        def h(self, x, base):
            return x * self.weights[0] * self.multiplier
        def g(self, result):
            return result
    class Patcher:
        def __init__(self, model):
            self.model, self.injections, self.patches = model, {}, {}
        def clone(self):
            clone = Patcher(self.model)
            clone.injections = {key: list(value) for key, value in self.injections.items()}
            clone.patches = {key: list(value) for key, value in self.patches.items()}
            return clone
        def set_injections(self, key, injections):
            self.injections[key] = injections
        def add_patches(self, patches, strength):
            for key, patch in patches.items():
                self.patches.setdefault(key, []).append((strength, patch))
            return list(patches)
    comfy = SimpleNamespace(
        model_management=SimpleNamespace(get_torch_device=lambda: torch.device("cpu")),
        lora=SimpleNamespace(model_lora_keys_unet=lambda *args: {}, load_lora=lambda state, _: state),
        lora_convert=SimpleNamespace(convert_lora=lambda state: state),
        weight_adapter=SimpleNamespace(WeightAdapterBase=Adapter))
    namespace = {"torch": torch, "nn": torch.nn, "logging": logging, "comfy": comfy,
                 "WeightAdapterBase": Adapter, "WeightAdapterTrainBase": Adapter,
                 "PatcherInjection": SimpleNamespace}
    core = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy"
    isolated_definitions(core / "weight_adapter/bypass.py", {
        "get_module_type_info", "BypassForwardHook", "BypassInjectionManager"}, namespace)
    comfy.weight_adapter.BypassInjectionManager = namespace["BypassInjectionManager"]
    isolated_definitions(core / "sd.py", {"load_bypass_lora_for_models"}, namespace)
    model = torch.nn.Sequential(torch.nn.Linear(1, 1, bias=False))
    model[0].weight.data.fill_(2)
    model[0].quant_format = "nvfp4"
    base = Patcher(model)
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.sd = SimpleNamespace(load_bypass_lora_for_models=namespace["load_bypass_lora_for_models"])
    states = {
        "/first.safetensors": {"0.weight": Adapter(3)},
        "/second.safetensors": {"0.weight": Adapter(5)},
        "/third.safetensors": {"0.weight": Adapter(7)},
        "/incompatible.safetensors": {"missing.weight": Adapter(11)},
        "/bias.safetensors": {"0.bias": ("diff", torch.tensor(1.0))},
    }
    runtime.utils = SimpleNamespace(load_torch_file=lambda path, **_: states[path])
    return runtime, base, torch


@pytest.mark.parametrize("count", [2, 3])
def test_pinned_nvfp4_keeps_all_adapters_and_ejects_cleanly(pinned_bypass, count, caplog):
    runtime, base, torch = pinned_bypass
    paths = ["/first.safetensors", "/second.safetensors", "/third.safetensors"][:count]
    stack = [{"path": path, "strength": strength} for path, strength in zip(paths, [.5, 1.2, -.2])]
    weights_before = base.model[0].weight.detach().clone()
    with caplog.at_level(logging.INFO):
        updated = runtime.load_loras(base, {"model": "/h3.safetensors", "loras": stack}, lambda _: None)
    assert not base.injections  # Cloning must not mutate the initial patcher.
    injections = updated.injections["bypass_lora"]
    assert len(injections) == 1
    assert len(injections[0].injections) == count
    expected = 2 + 3 * .5 + 5 * 1.2 + (7 * -.2 if count == 3 else 0)
    for _ in range(2):
        # Mirrors ModelPatcher's forward traversal for both operations.
        for injection in injections:
            injection.inject(updated)
        assert updated.model(torch.ones(1, 1)).item() == pytest.approx(expected)
        for injection in injections:
            injection.eject(updated)
        assert updated.model(torch.ones(1, 1)).item() == 2
    assert torch.equal(updated.model[0].weight, weights_before)
    for path in paths:
        assert f"LoRA applied: {Path(path).name}" in caplog.text


def test_pinned_nvfp4_rejects_unmatched_second_adapter(pinned_bypass):
    runtime, base, _ = pinned_bypass
    config = {"model": "/h3.safetensors", "loras": [
        {"path": "/first.safetensors", "strength": 1},
        {"path": "/incompatible.safetensors", "strength": 1}]}
    with pytest.raises(ValueError, match="incompatible.safetensors has no weights compatible"):
        runtime.load_loras(base, config, lambda _: None)


def test_pinned_nvfp4_regular_patch_preserves_previous_bypass(pinned_bypass):
    runtime, base, torch = pinned_bypass
    config = {"model": "/h3.safetensors", "loras": [
        {"path": "/first.safetensors", "strength": 1},
        {"path": "/bias.safetensors", "strength": .5}]}
    updated = runtime.load_loras(base, config, lambda _: None)
    assert "0.bias" in updated.patches
    injection, = updated.injections["bypass_lora"]
    injection.inject(updated)
    assert updated.model(torch.ones(1, 1)).item() == 5
    injection.eject(updated)
    assert updated.model(torch.ones(1, 1)).item() == 2
