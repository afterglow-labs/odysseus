"""Peak-memory protection for the pinned H3 dense INT8 attention path."""
from __future__ import annotations

import logging


def configure_bounded_attention(core, attention_backend, *, max_temporary_bytes=1024**3):
    """Release consumed inputs and attend over independent head groups.

    The full QKV projection and every key/value token are preserved. Only the
    INT8 attention kernel's head axis is split; no time/spatial windows, sparse
    approximation, weight changes or sampler changes are introduced. Explicit
    object patches and other attention overrides retain their own forwards.
    """
    import torch

    if max_temporary_bytes <= 0:
        raise ValueError("Attention temporary memory limit must be positive")
    attention_type, block_type = core.Attention, core.DiTBlock
    original_attention = getattr(attention_type, "_odysseus_original_forward", attention_type.forward)
    original_block = getattr(block_type, "_odysseus_original_forward", block_type.forward)
    attention_type._odysseus_original_forward = original_attention
    block_type._odysseus_original_forward = original_block
    kitchen = attention_backend.attention_comfy_kitchen_int8
    reported = set()

    def heads_per_call(module, x, options):
        if torch.is_grad_enabled() or core.comfy.model_management.in_training or x.ndim != 2:
            return module.heads
        override = options.get("optimized_attention_override")
        if override is not None:
            if getattr(override, "container_function", None) is not kitchen.container_function:
                return module.heads
        elif module.comfy_attention.function is not kitchen:
            return module.heads
        # Q/K/V INT8 copies, kernel output and its flattened copy. Full QKV
        # storage and the destination are unavoidable and outside this budget.
        # H3's head dimension is 128; keep padding accounted for on other sizes.
        width = 64 if module.head_dim <= 64 else 128 if module.head_dim <= 128 else 256
        per_head = x.shape[0] * width * (3 + 2 * x.element_size())
        return min(module.heads, max(1, max_temporary_bytes // max(1, per_head)))

    def bounded_attention(module, x, rope_freqs=None, transformer_options=None):
        options = {} if transformer_options is None else transformer_options
        # Only our block wrapper supplies this single-owner list. Emptying it
        # allows the normalized block input to die after QKV projection.
        if isinstance(x, list):
            x = x.pop()
        group = heads_per_call(module, x, options)
        if group >= module.heads:
            return original_attention(module, x, rope_freqs=rope_freqs, transformer_options=options)
        s, device = x.shape[0], x.device
        signature = (s, module.heads, group)
        if signature not in reported:
            logging.info("H3 attention memory protection: %d tokens, %d heads per call out of %d; full context retained",
                         s, group, module.heads)
            reported.add(signature)
        qkv = module.qkv_proj(x)
        del x
        q, k, v = qkv.split(module.heads * module.head_dim, dim=-1)
        v = v.view(s, module.heads, module.head_dim)
        if rope_freqs is not None:
            q = q.view(1, s, module.heads, module.head_dim)
            k = k.view(1, s, module.heads, module.head_dim)
            qw = core.comfy.model_management.cast_to(module.q_norm.weight, device=device)
            kw = core.comfy.model_management.cast_to(module.k_norm.weight, device=device)
            core.comfy.quant_ops.ck.rms_rope_split_half_(
                q, k, rope_freqs, qw, kw, epsilon=module.q_norm.eps,
                rot_dim=rope_freqs.shape[-3] * 2)
            q, k = q[0], k[0]
        else:
            q = module.q_norm(q.view(s, module.heads, module.head_dim))
            k = module.k_norm(k.view(s, module.heads, module.head_dim))
        q, k, v = (value.transpose(0, 1).unsqueeze(0) for value in (q, k, v))
        out = torch.empty((s, module.heads * module.head_dim), dtype=q.dtype, device=device)
        for first in range(0, module.heads, group):
            last = min(first + group, module.heads)
            piece = core.optimized_attention(
                q[:, first:last], k[:, first:last], v[:, first:last], last - first,
                preferred_attention=module.comfy_attention, mask=None, skip_reshape=True,
                transformer_options=options)
            out[:, first * module.head_dim:last * module.head_dim].copy_(piece.squeeze(0))
            del piece
        del q, k, v, qkv
        return module.out_proj(out)

    def bounded_block(module, x, t_emb, mod_segments, rope_freqs, transformer_options=None, attention=None):
        options = {} if transformer_options is None else transformer_options
        if (attention is not None or getattr(module.attn.forward, "__func__", None) is not bounded_attention
                or heads_per_call(module.attn, x, options) >= module.attn.heads):
            return original_block(module, x, t_emb, mod_segments, rope_freqs,
                                  transformer_options=options, attention=attention)
        shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = module.adaln_proj(t_emb)
        h = [core._mod_scale_shift(module.norm1(x), shift_msa, scale_msa, mod_segments)]
        x = core._mod_gate(x, gate_msa, module.attn(h, rope_freqs=rope_freqs, transformer_options=options), mod_segments)
        h = core._mod_scale_shift(module.norm2(x), shift_mlp, scale_mlp, mod_segments)
        return core._mod_gate(x, gate_mlp, module.mlp(h), mod_segments)

    attention_type.forward = bounded_attention
    block_type.forward = bounded_block
    logging.info("Automatic H3 INT8 attention temporary memory budget: %d MiB", max_temporary_bytes // 1024**2)
