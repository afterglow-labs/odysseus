"""Compare the guarded forward with the pinned H3 implementation, without CUDA."""
import ast
from pathlib import Path
from types import SimpleNamespace
import weakref

import pytest
import torch
from torch import nn
from torch.nn import functional as F

from scripts.h3_attention_memory import configure_bounded_attention


@pytest.fixture
def native():
    root = Path(__file__).parents[1] / "runtimes/minimax-h3/ComfyUI/comfy"
    model_path = root / "ldm/minimax/model.py"
    if not model_path.is_file():
        pytest.skip("Pinned H3 core is not installed")
    calls, normalized, buffers = [], [], []
    assert_released = [False]

    class Container:
        def __init__(self, tensor):
            self.tensor = tensor

        def take(self):
            value, self.tensor = self.tensor, None
            return value

    def kitchen(q, k, v, heads, **kwargs):
        calls.append((heads, q.shape[2], k.shape[2], v.shape[2]))
        if assert_released[0] and normalized:
            assert normalized[-1]() is None, "consumed normalized input is still held"
        value = F.scaled_dot_product_attention(q, k, v)
        return value.transpose(1, 2).reshape(q.shape[0], q.shape[2], -1)

    kitchen.container_function = object()

    class Preference(nn.Module):
        def __init__(self):
            super().__init__()
            self.function = kitchen

    def optimized(q, k, v, heads, **kwargs):
        if isinstance(q, Container):
            q, k, v = (item.take() for item in (q, k, v))
        return kitchen(q, k, v, heads, **kwargs)

    def rms_rope(q, k, freqs, qw, kw, epsilon, rot_dim):
        for x, weight in [(q, qw), (k, kw)]:
            value = F.rms_norm(x, (x.shape[-1],), weight, epsilon)
            # A deterministic partial rotation, identical for every head group.
            value[..., :rot_dim] = -value[..., :rot_dim].flip(-1)
            x.copy_(value)

    comfy = SimpleNamespace(model_management=SimpleNamespace(in_training=False, cast_to=lambda x, **kw: x.to(**kw)),
                            quant_ops=SimpleNamespace(ck=SimpleNamespace(rms_rope_split_half_=rms_rope)))
    namespace = dict(nn=nn, torch=torch, comfy=comfy, ComfyAttention=Preference,
                     AttentionTensorContainer=Container, optimized_attention=optimized)
    definitions = [n for n in ast.parse(model_path.read_text()).body if isinstance(n, (ast.ClassDef, ast.FunctionDef))
                   and n.name in {"Attention", "DiTBlock", "_mod_row", "_mod_scale_shift", "_mod_gate"}]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(model_path), "exec"), namespace)
    core = SimpleNamespace(**namespace)

    def make(dtype=torch.float32):
        torch.manual_seed(312)
        block = core.DiTBlock.__new__(core.DiTBlock)
        nn.Module.__init__(block)
        block.attn = core.Attention(40, 5, 8, 1e-6, operations=nn, dtype=dtype)
        block.norm1, block.norm2 = nn.RMSNorm(40, dtype=dtype), nn.RMSNorm(40, dtype=dtype)
        block.mlp = nn.Linear(40, 40, dtype=dtype)

        class Modulation(nn.Module):
            def forward(self, x):
                return x.chunk(6, dim=-1)

        block.adaln_proj = Modulation()
        block.norm1.register_forward_hook(lambda _m, _a, out: normalized.append(weakref.ref(out)))
        block.attn.qkv_proj.register_forward_hook(lambda _m, _a, out: buffers.append(weakref.ref(out)))

        def before_projection(*_):
            if assert_released[0]:
                assert buffers[-1]() is None, "QKV storage remains live during output projection"

        block.attn.out_proj.register_forward_pre_hook(before_projection)
        return block

    return SimpleNamespace(core=core, backend=SimpleNamespace(attention_comfy_kitchen_int8=kitchen),
                           make=make, calls=calls, assert_released=assert_released)


@pytest.mark.parametrize("dtype", [torch.float32, torch.bfloat16])
@pytest.mark.parametrize("rope", [False, True])
def test_head_groups_keep_full_context_and_release_consumed_buffers(native, dtype, rope):
    block = native.make(dtype)
    x, modulation = torch.randn(19, 40, dtype=dtype), torch.randn(2, 240, dtype=dtype)
    segments = [(0, 7, 0), (7, 19, 1)]
    freqs = torch.empty(1, 19, 1, 4, 2, 2) if rope else None
    with torch.inference_mode():
        expected = block(x.clone(), modulation, segments, freqs)
    # Force groups of two with an uneven final head; include padded head width.
    configure_bounded_attention(native.core, native.backend, max_temporary_bytes=19 * 64 * (3 + 2 * x.element_size()) * 2)
    native.calls.clear()
    native.assert_released[0] = True
    with torch.inference_mode():
        actual = block(x.clone(), modulation, segments, freqs)
    assert native.calls == [(2, 19, 19, 19), (2, 19, 19, 19), (1, 19, 19, 19)]
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)


@pytest.mark.parametrize("case", ["small", "gradient", "training", "sparse", "other_backend", "object_patch"])
def test_unrelated_or_explicit_paths_keep_original_forward(native, case):
    block = native.make()
    original = native.core.Attention.forward
    configure_bounded_attention(native.core, native.backend,
                                max_temporary_bytes=1024**3 if case == "small" else 1)
    options = {}
    if case == "training":
        native.core.comfy.model_management.in_training = True
    if case == "sparse":
        options["optimized_attention_override"] = lambda *a, **kw: None
    if case == "other_backend":
        block.attn.comfy_attention.function = lambda *a, **kw: None
    if case == "object_patch":
        import types
        block.attn.forward = types.MethodType(original, block.attn)
    with torch.set_grad_enabled(case == "gradient"):
        # No residual gates: avoid the pinned inference block's in-place update
        # of leaf tensors when checking the gradient-path early return.
        block(torch.randn(19, 40), torch.zeros(1, 240), [(0, 19, 0)], None, transformer_options=options)
    assert native.calls == [(5, 19, 19, 19)]


def test_explicit_dense_override_and_reconfiguration(native):
    block = native.make()
    original = native.core.Attention.forward
    override = SimpleNamespace(container_function=native.backend.attention_comfy_kitchen_int8.container_function)
    for _ in range(2):
        configure_bounded_attention(native.core, native.backend, max_temporary_bytes=1)
    assert native.core.Attention._odysseus_original_forward is original
    with torch.inference_mode():
        block(torch.randn(19, 40), torch.zeros(1, 240), [(0, 19, 0)], None,
              transformer_options={"optimized_attention_override": override})
    assert native.calls == [(1, 19, 19, 19)] * 5


def test_invalid_budget(native):
    with pytest.raises(ValueError, match="positive"):
        configure_bounded_attention(native.core, native.backend, max_temporary_bytes=0)
