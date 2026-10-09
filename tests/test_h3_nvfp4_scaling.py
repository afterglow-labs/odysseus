"""Native NVFP4 scale equivalence without input-sized absolute-value buffers."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
from torch.utils._python_dispatch import TorchDispatchMode

from scripts import h3_video_worker as worker


@pytest.fixture
def native():
    path = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy/quant_ops.py"
    if not path.is_file():
        pytest.skip("Pinned H3 inference checkout is not installed")
    calls = []

    class Parent:
        Params = SimpleNamespace

        @staticmethod
        def get_padded_shape(shape):
            return tuple((dim + 15) // 16 * 16 for dim in shape)

    def encode(tensor, scale, **kwargs):
        calls.append((tensor, scale, kwargs))
        return "quantized-data", "block-scales"

    ck = SimpleNamespace(float_utils=SimpleNamespace(F8_E4M3_MAX=448.0, F4_E2M1_MAX=6.0),
                         quantize_nvfp4=encode)
    comfy = SimpleNamespace(float=SimpleNamespace(stochastic_round_quantize_nvfp4_by_block=encode))
    namespace = {"torch": torch, "_CKNvfp4Layout": Parent, "ck": ck, "comfy": comfy}
    definition, = [node for node in ast.parse(path.read_text()).body
                   if isinstance(node, ast.ClassDef) and node.name == "TensorCoreNVFP4Layout"]
    exec(compile(ast.Module(body=[definition], type_ignores=[]), str(path), "exec"), namespace)
    return SimpleNamespace(TensorCoreNVFP4Layout=namespace["TensorCoreNVFP4Layout"], ck=ck), calls


class AbsTracker(TorchDispatchMode):
    def __init__(self):
        super().__init__()
        self.abs_sizes = []

    def __torch_dispatch__(self, func, types, args=(), kwargs=None):
        if func == torch.ops.aten.abs.default:
            self.abs_sizes.append(args[0].numel())
        return func(*args, **(kwargs or {}))


@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32, torch.float64])
@pytest.mark.parametrize("kind", ["mixed", "negative", "positive", "zero", "strided", "nan", "inf"])
def test_scale_is_exactly_native_and_abs_only_receives_scalars(native, dtype, kind):
    quant_ops, calls = native
    values = torch.tensor([[-1.1875, 2.625, 0., -.0], [-3.9375, .5, -.25, 1.125]], dtype=dtype)
    if kind == "negative":
        values = -values.abs()
    elif kind == "positive":
        values = values.abs()
    elif kind == "zero":
        values.zero_()
    elif kind == "strided":
        values = values.T
    elif kind == "nan":
        values[0, 0] = float("nan")
    elif kind == "inf":
        values[0, 0], values[0, 1] = -float("inf"), float("inf")
    layout = quant_ops.TensorCoreNVFP4Layout
    with torch.inference_mode():
        expected_data, expected = layout.quantize(values)
    expected_call = calls.pop()
    worker.configure_bounded_nvfp4_scaling(quant_ops)
    tracker = AbsTracker()
    with torch.inference_mode(), tracker:
        actual_data, actual = layout.quantize(values)
    assert tracker.abs_sizes == [1, 1]
    assert actual_data == expected_data
    torch.testing.assert_close(actual.scale, expected.scale, rtol=0, atol=0, equal_nan=True)
    assert actual.scale.dtype == torch.float32 and actual.orig_dtype == dtype
    assert actual.orig_shape == expected.orig_shape
    assert actual.block_scale == expected.block_scale
    assert calls[0][0] is values and expected_call[0] is values
    assert calls[0][2] == expected_call[2]


@pytest.mark.parametrize("scale", [None, "recalculate", .375, torch.tensor(.375)])
@pytest.mark.parametrize("stochastic", [0, 17])
def test_explicit_scales_padding_and_stochastic_options_reach_native_quantizer(native, scale, stochastic):
    quant_ops, calls = native
    values = torch.arange(35, dtype=torch.bfloat16).reshape(5, 7)
    layout = quant_ops.TensorCoreNVFP4Layout
    with torch.inference_mode():
        _, expected = layout.quantize(values, scale=scale, stochastic_rounding=stochastic, inplace_ops=True)
    expected_call = calls.pop()
    worker.configure_bounded_nvfp4_scaling(quant_ops)
    # Repeated setup must preserve the original classmethod, not nest wrappers.
    worker.configure_bounded_nvfp4_scaling(quant_ops)
    tracker = AbsTracker()
    with torch.inference_mode(), tracker:
        _, actual = layout.quantize(values, scale=scale, stochastic_rounding=stochastic, inplace_ops=True)
    assert tracker.abs_sizes == ([1, 1] if scale is None or isinstance(scale, str) else [])
    torch.testing.assert_close(actual.scale, expected.scale, rtol=0, atol=0)
    assert calls[0][0] is values
    assert calls[0][2] == expected_call[2]
    assert calls[0][2]["pad_16x"] is True
    if stochastic:
        assert calls[0][2]["seed"] == stochastic


def test_gradient_enabled_calls_keep_native_backward_behavior(native):
    quant_ops, _ = native
    values = torch.tensor([[-3., 2.], [1., -.5]], requires_grad=True)
    worker.configure_bounded_nvfp4_scaling(quant_ops)
    tracker = AbsTracker()
    with tracker:
        _, params = quant_ops.TensorCoreNVFP4Layout.quantize(values)
    assert tracker.abs_sizes == [values.numel()]
    params.scale.backward()
    assert values.grad[0, 0] < 0 and torch.count_nonzero(values.grad) == 1


def test_invalid_tensor_rank_keeps_native_error(native):
    quant_ops, _ = native
    worker.configure_bounded_nvfp4_scaling(quant_ops)
    with torch.inference_mode(), pytest.raises(ValueError, match="NVFP4 requires 2D tensor"):
        quant_ops.TensorCoreNVFP4Layout.quantize(torch.zeros(3))
