"""CPU comparisons and allocation-shape checks for H3 feedforward protection."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
import torch.nn.functional as F

from scripts import h3_video_worker as worker


@pytest.fixture
def native():
    path = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy/ldm/minimax/model.py"
    if not path.is_file():
        pytest.skip("Pinned MiniMax inference core is not installed")
    calls = []

    class Linear(torch.nn.Linear):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.quant_format = "int8_tensorwise"
            self.weight._layout_cls = "TensorWiseINT8Layout"

        def forward(self, x):
            calls.append((self.out_features, x.shape[0]))
            if self.quant_format is None:
                return F.linear(x, self.weight, self.bias)
            # Mirror INT8's independent activation scale for every token.
            scale = x.abs().amax(dim=-1, keepdim=True).clamp_min(1e-8) / 127
            activation = (x / scale).round().clamp(-127, 127) * scale
            return F.linear(activation, self.weight, self.bias)

    def fused(linear, x, activation):
        assert activation == "swiglu"
        gate, value = x.chunk(2, dim=-1)
        return linear(F.silu(gate) * value)

    namespace = {"nn": torch.nn, "comfy": SimpleNamespace(ops=SimpleNamespace(linear_input_act=fused))}
    definition, = [node for node in ast.parse(path.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "MLP"]
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["MLP"], SimpleNamespace(Linear=Linear), calls


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
def test_bounds_temporaries_and_matches_row_quantized_native_mlp(native, dtype):
    cls, operations, calls = native
    torch.manual_seed(112)
    module = cls(8, 16, operations=operations, dtype=dtype)
    x = torch.randn(19, 8, dtype=dtype)
    weights = [layer.weight.detach().clone() for layer in (module.fc1, module.fc2)]
    with torch.inference_mode():
        expected = module(x)
    row_bytes = (2 * 32 + 8) * x.element_size() + 16 + 4
    worker.configure_bounded_feedforward(cls, max_intermediate_bytes=3 * row_bytes)
    calls.clear()
    with torch.inference_mode():
        actual = module(x)
    assert [rows for width, rows in calls if width == 32] == [3, 3, 3, 3, 3, 3, 1]
    torch.testing.assert_close(actual, expected, rtol=1e-5, atol=1e-5)
    for layer, original in zip((module.fc1, module.fc2), weights):
        assert torch.equal(layer.weight, original)


@pytest.mark.parametrize("setting", ["nvfp4", "other_layout", "plain", "manual_one", "manual_eight", "grad"])
def test_unrelated_quantization_and_explicit_controls_keep_full_forward(native, setting):
    cls, operations, calls = native
    module = cls(8, 16, operations=operations)
    if setting == "nvfp4":
        module.fc1.quant_format = "nvfp4"
    elif setting == "other_layout":
        module.fc2.weight._layout_cls = "TensorCoreNVFP4Layout"
    elif setting == "plain":
        module.fc2.quant_format = None
    elif setting.startswith("manual"):
        module._odysseus_manual_ff_chunks = 1 if setting == "manual_one" else 8
    worker.configure_bounded_feedforward(cls, max_intermediate_bytes=1)
    with torch.set_grad_enabled(setting == "grad"):
        module(torch.randn(19, 8))
    assert calls == [(32, 19), (8, 19)]


def test_small_inputs_and_reconfiguration_do_not_nest_wrappers(native):
    cls, operations, calls = native
    module = cls(8, 16, operations=operations)
    original = cls.forward
    worker.configure_bounded_feedforward(cls, max_intermediate_bytes=1)
    worker.configure_bounded_feedforward(cls, max_intermediate_bytes=1024**2)
    assert cls._odysseus_original_feedforward is original
    with torch.inference_mode():
        module(torch.randn(19, 8))
    assert calls == [(32, 19), (8, 19)]


def test_invalid_budget_is_rejected(native):
    with pytest.raises(ValueError, match="positive"):
        worker.configure_bounded_feedforward(native[0], max_intermediate_bytes=0)


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16, torch.float16])
@pytest.mark.parametrize("plain_layers", [(0,), (1,), (0, 1)])
def test_mixed_and_plain_projections_match_full_forward(native, dtype, plain_layers):
    cls, operations, calls = native
    torch.manual_seed(35)
    module = cls(8, 16, operations=operations, dtype=dtype)
    for index in plain_layers:
        layer = (module.fc1, module.fc2)[index]
        layer.quant_format = None
        del layer.weight._layout_cls
    x = torch.randn(19, 8, dtype=dtype)
    weights = [layer.weight.detach().clone() for layer in (module.fc1, module.fc2)]
    with torch.inference_mode():
        expected = module(x)
    budget = ((2 * 32 + 8) * x.element_size() + 16 + 4) * 3
    worker.configure_bounded_feedforward(cls, max_intermediate_bytes=budget)
    calls.clear()
    with torch.inference_mode():
        actual = module(x)
    assert [rows for width, rows in calls if width == 32] == [3, 3, 3, 3, 3, 3, 1]
    torch.testing.assert_close(actual, expected)
    for layer, original in zip((module.fc1, module.fc2), weights):
        assert torch.equal(layer.weight, original)


def test_full_hd_refinement_never_requests_the_18_gib_projection(native):
    """Replay the failed job's tensor dimensions on meta; allocate no GPU RAM."""
    cls, _, _ = native
    with torch.device("meta"):
        module = cls(5376, 14336, operations=torch.nn, dtype=torch.bfloat16)
        x = torch.empty(345260, 5376, dtype=torch.bfloat16)
    allocations = []
    hook = module.fc1.register_forward_hook(
        lambda _, inputs, output: allocations.append(output.numel() * output.element_size()))
    with torch.inference_mode():
        module(x)
    assert allocations == [19798589440]  # 18.44 GiB before the guard.
    worker.configure_bounded_feedforward(cls)
    allocations.clear()
    with torch.inference_mode():
        actual = module(x)
    hook.remove()
    assert actual.shape == x.shape and actual.dtype == x.dtype
    assert len(allocations) > 1
    assert max(allocations) < 1024**3
