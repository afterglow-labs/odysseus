"""CPU comparisons against the pinned bypass hook and native LoRA residual."""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
import torch
import torch.nn.functional as F

from scripts import h3_video_worker as worker


@pytest.fixture
def native():
    core = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy/weight_adapter"
    if not core.is_dir():
        pytest.skip("Pinned H3 inference checkout is not installed")
    namespace = dict(torch=torch, nn=torch.nn, F=F, logging=logging,
                     comfy=SimpleNamespace(model_management=SimpleNamespace(get_torch_device=lambda: torch.device("cpu"))))
    for filename, names in [
        ("base.py", {"WeightAdapterBase", "WeightAdapterTrainBase"}),
        ("lora.py", {"LoRAAdapter"}),
        ("bypass.py", {"get_module_type_info", "BypassForwardHook"}),
    ]:
        path = core / filename
        definitions = [node for node in ast.parse(path.read_text()).body
                       if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
        assert len(definitions) == len(names)
        future = ast.parse("from __future__ import annotations").body
        exec(compile(ast.Module(body=future + definitions, type_ignores=[]), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


def make_hook(native, module, *, strength, alpha, mid=False):
    dtype = module.weight.dtype
    weights = (torch.randn(12, 3, dtype=dtype), torch.randn(3, 8, dtype=dtype), alpha,
               torch.randn(3, 3, dtype=dtype) if mid else None, None, None)
    adapter = native.LoRAAdapter(set(), weights)
    return native.BypassForwardHook(module, adapter, multiplier=strength)


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
@pytest.mark.parametrize("strength,alpha,mid", [(1.0, 6.0, False), (-0.35, None, False), (.7, 3.0, True)])
def test_streamed_residual_matches_native_and_calls_full_base_once(native, monkeypatch, dtype, strength, alpha, mid):
    torch.manual_seed(29)
    base_calls = []

    class Base(torch.nn.Linear):
        def forward(self, x):
            base_calls.append(x.shape)
            # Model a global activation scale: splitting the base input would
            # change this output, unlike splitting the additive residual.
            scale = x.abs().amax().clamp_min(1e-6)
            quantized = (x / scale * 15).round() / 15 * scale
            return F.linear(quantized, self.weight)

    module = Base(8, 12, bias=False, dtype=dtype)
    hook = make_hook(native, module, strength=strength, alpha=alpha, mid=mid)
    x = torch.randn(17, 8, dtype=dtype)
    weights_before = module.weight.detach().clone()
    with torch.inference_mode():
        hook.inject()
        expected = module(x)
        hook.eject()
    base_calls.clear()
    up_calls = []
    linear = F.linear

    def tracked_linear(values, weight, *args, **kwargs):
        if tuple(weight.shape) == (12, 3):
            up_calls.append(tuple(values.shape))
        return linear(values, weight, *args, **kwargs)

    monkeypatch.setattr(F, "linear", tracked_linear)
    worker.configure_bounded_lora(native.BypassForwardHook, native.LoRAAdapter,
                                 max_residual_bytes=3 * 12 * x.element_size())
    with torch.inference_mode():
        hook.inject()
        actual = module(x)
        hook.eject()
    assert base_calls == [x.shape]
    assert [shape[0] for shape in up_calls] == [3, 3, 3, 3, 3, 2]
    torch.testing.assert_close(actual, expected, rtol=.01 if dtype == torch.bfloat16 else 1e-5,
                               atol=.0625 if dtype == torch.bfloat16 else 1e-5)
    assert torch.equal(module.weight, weights_before)


def test_multiple_adapters_keep_order_strength_and_repeated_injection(native):
    torch.manual_seed(30)
    module = torch.nn.Linear(8, 12, bias=False)
    hooks = [make_hook(native, module, strength=.65, alpha=3),
             make_hook(native, module, strength=-.4, alpha=6)]
    x = torch.randn(17, 8)
    with torch.inference_mode():
        for hook in hooks:
            hook.inject()
        expected = module(x)
        for hook in reversed(hooks):
            hook.eject()
        unadapted = module(x)
    worker.configure_bounded_lora(native.BypassForwardHook, native.LoRAAdapter, max_residual_bytes=144)
    # Reconfiguration must replace the wrapper rather than nesting wrappers.
    worker.configure_bounded_lora(native.BypassForwardHook, native.LoRAAdapter, max_residual_bytes=144)
    for _ in range(2):
        with torch.inference_mode():
            for hook in hooks:
                hook.inject()
            torch.testing.assert_close(module(x), expected)
            for hook in reversed(hooks):
                hook.eject()
            torch.testing.assert_close(module(x), unadapted)


def test_autograd_uses_native_path_and_preserves_backward(native):
    torch.manual_seed(31)
    module = torch.nn.Linear(8, 12, bias=False)
    hook = make_hook(native, module, strength=.5, alpha=3)
    worker.configure_bounded_lora(native.BypassForwardHook, native.LoRAAdapter, max_residual_bytes=1)
    x = torch.randn(17, 8, requires_grad=True)
    hook.inject()
    module(x).sum().backward()
    hook.eject()
    assert x.grad is not None and module.weight.grad is not None


@pytest.mark.parametrize("kind", ["custom", "convolution", "training"])
def test_nonstandard_adapters_use_original_hook(native, kind):
    class Custom(native.LoRAAdapter):
        def bypass_forward(self, forward, x, *args, **kwargs):
            return forward(x, *args, **kwargs) + 17

    class Training(native.WeightAdapterTrainBase):
        def h(self, x, base_out):
            return torch.ones_like(base_out) * 2

    module = torch.nn.Conv1d(8, 12, 1, bias=False) if kind == "convolution" else torch.nn.Linear(8, 12, bias=False)
    x = torch.randn(2, 8, 5) if kind == "convolution" else torch.randn(17, 8)
    if kind == "training":
        adapter = Training()
    else:
        adapter_type = Custom if kind == "custom" else native.LoRAAdapter
        adapter = adapter_type(set(), (torch.randn(12, 3), torch.randn(3, 8), 3, None, None, None))
    hook = native.BypassForwardHook(module, adapter, multiplier=.5)
    with torch.inference_mode():
        hook.inject()
        expected = module(x)
        hook.eject()
    worker.configure_bounded_lora(native.BypassForwardHook, native.LoRAAdapter, max_residual_bytes=1)
    with torch.inference_mode():
        hook.inject()
        actual = module(x)
        hook.eject()
    torch.testing.assert_close(actual, expected)
