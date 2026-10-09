"""CPU checks for full-width time-conditioning LoRAs on pruned H3."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
import torch.nn.functional as F

from scripts import h3_lora_compat as compat
from scripts import h3_video_worker as worker


class Patcher:
    def __init__(self, dm):
        self.model = SimpleNamespace(diffusion_model=dm, modules=dm.modules)
        self.object_patches, self.patches, self.injections = {}, {}, {}

    def clone(self):
        cloned = Patcher(self.model.diffusion_model)
        cloned.object_patches = self.object_patches.copy()
        cloned.patches = {k: list(v) for k, v in self.patches.items()}
        cloned.injections = {k: list(v) for k, v in self.injections.items()}
        return cloned

    def get_model_object(self, key):
        if key in self.object_patches:
            return self.object_patches[key]
        result = self.model
        for name in key.split("."):
            result = getattr(result, name)
        return result

    def add_object_patch(self, key, value):
        self.object_patches[key] = value


@pytest.fixture
def pruned(monkeypatch):
    path = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy/ldm/minimax/model.py"
    if not path.is_file():
        pytest.skip("Pinned H3 inference checkout is not installed")
    namespace = {"nn": torch.nn}
    definition, = [node for node in ast.parse(path.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "AdalnProj"]
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    projector = namespace["AdalnProj"]
    dm = torch.nn.Module()
    dm.use_adaln_curves = True
    dm.blocks = torch.nn.ModuleList([torch.nn.Module()])
    dm.blocks[0].adaln_proj = projector(8, 4, 6, 3, apply_silu=False, operations=torch.nn)
    dm.final_layer = torch.nn.Module()
    dm.final_layer.adaln_proj = projector(8, 4, 2, 1, apply_silu=False, operations=torch.nn)
    t = torch.linspace(0, 1, 7)
    curve = torch.stack([t, t.square(), t**3, t.sin(), t.cos(), t * 2, t * .5, t * .3], dim=1)
    dm.register_buffer("adaln_t_table", curve)
    grid = t[:, None] * torch.linspace(.1, 1, compat.FULL_TIME_DIM)[None]
    monkeypatch.setattr(compat, "load_time_embedding_grid", lambda: grid)
    return Patcher(dm), grid


def lerp_rows(table, times):
    pos = times * (table.shape[0] - 1)
    lower = pos.floor().long().clamp(max=table.shape[0] - 2)
    return torch.lerp(table[lower], table[lower + 1], (pos - lower)[:, None])


def adapter_state(prefix="blocks.0.adaln_proj.linear", *, out_features=72, width=2688):
    return {prefix + ".lora_A.weight": torch.randn(2, width) * .03,
            prefix + ".lora_B.weight": torch.randn(out_features, 2) * .04}


def test_interpolation_recovers_exact_rows_between_knots_with_references_and_masks(pruned):
    model, grid = pruned
    curve = model.model.diffusion_model.adaln_t_table
    times = torch.tensor([1., .123456, 0., .999, .23, .123456, .7, .93, .31])
    actual = compat.interpolate_time_grid(lerp_rows(curve, times), curve, grid)
    torch.testing.assert_close(actual, lerp_rows(grid, times), atol=1e-6, rtol=1e-6)
    assert actual.shape[0] > 3


def test_interpolation_bounds_workspace_across_many_distinct_mask_rows(pruned):
    model, grid = pruned
    curve = model.model.diffusion_model.adaln_t_table
    times = torch.linspace(0, 1, 137)
    actual = compat.interpolate_time_grid(lerp_rows(curve, times), curve, grid)
    torch.testing.assert_close(actual, lerp_rows(grid, times), atol=1e-6, rtol=1e-6)


@pytest.mark.parametrize("bad", ["off-curve", "nonfinite", "degenerate"])
def test_invalid_curve_input_fails_without_silently_dropping_delta(pruned, bad):
    model, grid = pruned
    curve = model.model.diffusion_model.adaln_t_table.clone()
    rows = lerp_rows(curve, torch.tensor([.3]))
    if bad == "off-curve":
        rows[0, 0] += .1
    elif bad == "nonfinite":
        rows[0, 0] = float("nan")
    else:
        curve[1] = curve[0]
    with pytest.raises(ValueError, match="curve"):
        compat.interpolate_time_grid(rows, curve, grid)


@pytest.mark.parametrize("projection,out_features,alpha", [("blocks.0.adaln_proj", 72, None),
                                                           ("final_layer.adaln_proj", 8, 6.)])
def test_all_adaln_outputs_match_full_width_residual_and_keep_module_tree(pruned, projection, out_features, alpha):
    model, grid = pruned
    prefix = projection + ".linear"
    state = adapter_state(prefix, out_features=out_features)
    if alpha is not None:
        state[prefix + ".alpha"] = torch.tensor(alpha)
    backbone = torch.tensor([42.])
    state["blocks.0.mlp.fc1.lora_A.weight"] = backbone
    ordinary, adapters = compat.split_curve_lora(model, state)
    assert ordinary == {"blocks.0.mlp.fc1.lora_A.weight": backbone}
    assert len(adapters) == 1
    times = torch.tensor([.12, 1., 0., .31, .76])
    dm = model.model.diffusion_model
    rows = lerp_rows(dm.adaln_t_table, times)
    original = model.get_model_object("diffusion_model." + projection)
    weights = {name: value.clone() for name, value in dm.state_dict().items()}
    base_outputs = original(rows)
    strength = 3.0
    assert compat.apply_curve_lora(model, adapters, strength, grid=grid) == 1
    result = model.get_model_object("diffusion_model." + projection + ".forward")(rows)
    delta = F.linear(F.linear(lerp_rows(grid, times), state[prefix + ".lora_A.weight"]),
                     state[prefix + ".lora_B.weight"]) * strength * (alpha / 2 if alpha is not None else 1)
    updates = delta.reshape(rows.shape[0] * original.modalities, original.expand * original.hidden).chunk(original.expand, -1)
    for actual, base, update in zip(result, base_outputs, updates):
        torch.testing.assert_close(actual, base + update, atol=1e-5, rtol=1e-5)
    assert dm.state_dict().keys() == weights.keys()
    assert all(torch.equal(dm.state_dict()[key], value) for key, value in weights.items())


def test_multiple_full_width_adapters_compose_in_order_and_clone_preserves_prior(pruned):
    model, grid = pruned
    first, second = adapter_state(), adapter_state()
    _, adapters1 = compat.split_curve_lora(model, first)
    _, adapters2 = compat.split_curve_lora(model, second)
    compat.apply_curve_lora(model, adapters1, .7, grid=grid)
    cloned = model.clone()
    forward_path = "diffusion_model.blocks.0.adaln_proj.forward"
    previous = model.get_model_object(forward_path)
    compat.apply_curve_lora(cloned, adapters2, -.25, grid=grid)
    assert model.get_model_object(forward_path) is previous
    times = torch.tensor([.12, .89])
    rows = lerp_rows(model.model.diffusion_model.adaln_t_table, times)
    original = previous(rows)
    actual = cloned.get_model_object(forward_path)(rows)
    delta = F.linear(F.linear(lerp_rows(grid, times), adapters2[0].down), adapters2[0].up) * -.25
    delta = delta.reshape(6, 24).chunk(6, -1)
    for result, earlier, added in zip(actual, original, delta):
        torch.testing.assert_close(result, earlier + added, atol=1e-5, rtol=1e-5)


def test_full_bases_and_already_converted_curve_adapters_stay_native(pruned):
    model, _ = pruned
    state = adapter_state(width=8)
    ordinary, adapters = compat.split_curve_lora(model, state)
    assert ordinary is state and adapters == ()
    model.model.diffusion_model.use_adaln_curves = False
    state = adapter_state()
    assert compat.split_curve_lora(model, state) == (state, ())


@pytest.mark.parametrize("full_first", [False, True])
def test_full_width_and_native_curve_lora_compose_in_either_order(pruned, full_first):
    model, grid = pruned
    projection = model.model.diffusion_model.blocks[0].adaln_proj
    full_state, curve_state = adapter_state(), adapter_state(width=8)
    prefix = "blocks.0.adaln_proj.linear"
    ordinary, full_adapters = compat.split_curve_lora(model, full_state)
    assert not ordinary
    assert compat.split_curve_lora(model, curve_state) == (curve_state, ())
    times = torch.tensor([.24, .81, 1.])
    rows = lerp_rows(model.model.diffusion_model.adaln_t_table, times)
    original = projection(rows)
    native_linear = projection.linear.forward

    def native_curve_bypass(values):
        return native_linear(values) + F.linear(F.linear(values, curve_state[prefix + ".lora_A.weight"]),
                                              curve_state[prefix + ".lora_B.weight"]) * .4

    if full_first:
        compat.apply_curve_lora(model, full_adapters, -.8, grid=grid)
        projection.linear.forward = native_curve_bypass
    else:
        projection.linear.forward = native_curve_bypass
        compat.apply_curve_lora(model, full_adapters, -.8, grid=grid)
    result = model.get_model_object("diffusion_model.blocks.0.adaln_proj.forward")(rows)
    delta_curve = F.linear(F.linear(rows, curve_state[prefix + ".lora_A.weight"]),
                           curve_state[prefix + ".lora_B.weight"]) * .4
    delta_full = F.linear(F.linear(lerp_rows(grid, times), full_state[prefix + ".lora_A.weight"]),
                          full_state[prefix + ".lora_B.weight"]) * -.8
    for actual, base, curve_delta, full_delta in zip(result, original,
            delta_curve.reshape(9, 24).chunk(6, -1), delta_full.reshape(9, 24).chunk(6, -1)):
        torch.testing.assert_close(actual, base + curve_delta + full_delta, atol=1e-5, rtol=1e-5)


@pytest.mark.parametrize("fault", ["width", "output", "missing-up", "mid", "dora", "alpha"])
def test_unsupported_adaln_pairs_fail_before_loader_or_sampling(pruned, fault):
    model, _ = pruned
    prefix = "blocks.0.adaln_proj.linear"
    state = adapter_state()
    if fault == "width":
        state[prefix + ".lora_A.weight"] = torch.zeros(2, 17)
    elif fault == "output":
        state[prefix + ".lora_B.weight"] = torch.zeros(73, 2)
    elif fault == "missing-up":
        del state[prefix + ".lora_B.weight"]
    elif fault == "alpha":
        state[prefix + ".alpha"] = torch.tensor(float("nan"))
    else:
        state[prefix + (".lora_mid.weight" if fault == "mid" else ".dora_scale")] = torch.ones(1)
    with pytest.raises(ValueError, match="AdaLN"):
        compat.split_curve_lora(model, state)


def test_worker_passes_backbone_and_preserves_adaln_only_adapter(pruned):
    model, _ = pruned
    state = adapter_state()
    runtime = worker.H3Runtime.__new__(worker.H3Runtime)
    runtime.utils = SimpleNamespace(load_torch_file=lambda *args, **kwargs: state)
    received = []

    def loader(base, clip, lora, strength, clip_strength):
        received.append((lora, strength))
        return base.clone(), None

    runtime.sd = SimpleNamespace(load_bypass_lora_for_models=loader)
    updated = runtime.load_lora(model, {"model": "/h3_nvfp4.safetensors", "lora": "/full.safetensors", "lora_scale": 3}, lambda _: None)
    assert received == [({}, 3)]
    assert not model.object_patches
    assert set(updated.object_patches) == {"diffusion_model.blocks.0.adaln_proj.forward"}


def test_bundled_asset_validates_hash_shape_and_dtype():
    compat.load_time_embedding_grid.cache_clear()
    grid = compat.load_time_embedding_grid()
    assert grid.shape == (1025, 2688) and grid.dtype == torch.bfloat16


def test_changed_asset_fails_hash_before_load(tmp_path, monkeypatch):
    path = tmp_path / "grid.safetensors"
    path.write_bytes(b"changed")
    compat.load_time_embedding_grid.cache_clear()
    monkeypatch.setattr(compat, "GRID_PATH", path)
    with pytest.raises(ValueError, match="SHA-256"):
        compat.load_time_embedding_grid()
    compat.load_time_embedding_grid.cache_clear()
