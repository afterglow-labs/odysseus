"""Full-width H3 AdaLN LoRAs on pruned time-curve checkpoints.

Uses Larryvrh's original SiLU time-embedding grid and runtime residual approach:
https://github.com/Larryvrh/ComfyUI-MiniMax-H3-Turbo
Asset provenance and license are in assets/minimax_h3_turbo/. Recovering the
position on the actual input curve supports every native conditioning row,
including reference video and per-frame masks, without copying sampler logic.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import logging
import math
from pathlib import Path


GRID_PATH = Path(__file__).with_name("assets") / "minimax_h3_turbo/h3_silu_temb_grid.safetensors"
GRID_SHA256 = "30eb3c2cc7fb6b470d9717ff840d359313ac27cd64b705e32da1baa10f72d6a8"
FULL_TIME_DIM = 2688
PAIR_SUFFIXES = (("lora_A.weight", "lora_B.weight"), ("lora_down.weight", "lora_up.weight"),
                 ("lora.down.weight", "lora.up.weight"), ("lora_A", "lora_B"))


@lru_cache(maxsize=1)
def load_time_embedding_grid():
    import torch
    from safetensors.torch import load_file
    try:
        digest = hashlib.sha256(GRID_PATH.read_bytes()).hexdigest()
    except OSError as exc:
        raise ValueError("H3 full-width AdaLN compatibility asset is missing; restore scripts/assets/minimax_h3_turbo") from exc
    if digest != GRID_SHA256:
        raise ValueError("H3 full-width AdaLN compatibility asset failed its SHA-256 check")
    grid = load_file(str(GRID_PATH), device="cpu").get("silu_t_emb_grid")
    if (grid is None or tuple(grid.shape) != (1025, FULL_TIME_DIM)
            or grid.dtype != torch.bfloat16 or not bool(torch.isfinite(grid).all())):
        raise ValueError("H3 full-width AdaLN compatibility asset has an invalid embedding grid")
    return grid


@dataclass(frozen=True)
class CurveAdapter:
    projection: str
    down: object
    up: object
    scale: float


def split_curve_lora(model, state):
    """Remove full-width AdaLN pairs only when they need the curve bridge."""
    dm = getattr(model.model, "diffusion_model", None)
    if not getattr(dm, "use_adaln_curves", False):
        return state, ()
    adapters, consumed, seen = [], set(), set()
    for key, down in state.items():
        for down_suffix, up_suffix in PAIR_SUFFIXES:
            ending = "." + down_suffix
            if not key.endswith(ending):
                continue
            prefix = key[:-len(ending)]
            name = prefix.removeprefix("diffusion_model.")
            if not name.endswith(".adaln_proj.linear"):
                continue
            projection = "diffusion_model." + name[:-len(".linear")]
            linear = model.get_model_object(projection + ".linear")
            if down.ndim == 2 and down.shape[1] == linear.weight.shape[1]:
                continue  # Already converted curve-space adapters stay native.
            up_key = prefix + "." + up_suffix
            up = state.get(up_key)
            if (down.ndim != 2 or down.shape[0] <= 0 or down.shape[1] != FULL_TIME_DIM or up is None
                    or up.ndim != 2 or up.shape != (linear.weight.shape[0], down.shape[0])):
                raise ValueError(f"Incompatible full-width H3 AdaLN LoRA pair: {name}")
            if projection in seen:
                raise ValueError(f"Duplicate H3 AdaLN LoRA pair: {name}")
            if any(prefix + suffix in state for suffix in (".lora_mid.weight", ".dora_scale", ".reshape_weight")):
                raise ValueError(f"H3 curve compatibility requires a plain AdaLN LoRA pair: {name}")
            alpha_key = prefix + ".alpha"
            scale = float(state[alpha_key]) / down.shape[0] if alpha_key in state else 1.0
            if not math.isfinite(scale):
                raise ValueError(f"Invalid H3 AdaLN LoRA alpha: {name}")
            adapters.append(CurveAdapter(projection, down, up, scale))
            consumed.update((key, up_key))
            if alpha_key in state:
                consumed.add(alpha_key)
            seen.add(projection)
    if not adapters:
        return state, ()
    # Validate the small required asset before installing any model hooks.
    load_time_embedding_grid()
    return {key: value for key, value in state.items() if key not in consumed}, tuple(adapters)


def interpolate_time_grid(t_emb, curve, grid):
    """Invert the piecewise-linear curve, then interpolate its full SiLU grid."""
    import torch
    if (t_emb.ndim != 2 or curve.ndim != 2 or grid.ndim != 2
            or t_emb.shape[1] != curve.shape[1] or curve.shape[0] != grid.shape[0]
            or curve.shape[0] < 2):
        raise ValueError("H3 AdaLN curve and full time-embedding grid have incompatible shapes")
    curve = curve.to(device=t_emb.device, dtype=torch.float32)
    grid = grid.to(device=t_emb.device, dtype=torch.float32)
    start, direction = curve[:-1], curve[1:] - curve[:-1]
    norm = direction.square().sum(-1)
    if not bool(torch.isfinite(curve).all()) or bool((norm <= 0).any()):
        raise ValueError("H3 AdaLN curve contains non-finite or degenerate segments")
    rows = []
    for chunk in t_emb.float().split(64):
        offset = chunk[:, None, :] - start[None, :, :]
        fraction = (offset * direction).sum(-1).div(norm).clamp(0, 1)
        distance = (offset - fraction[..., None] * direction).square().sum(-1)
        nearest = distance.argmin(dim=1)
        index = torch.arange(chunk.shape[0], device=t_emb.device)
        error = distance[index, nearest]
        if not bool(torch.isfinite(error).all()) or bool((error > 1e-10).any()):
            raise ValueError("H3 AdaLN input does not lie on the checkpoint's time curve")
        weight = fraction[index, nearest, None]
        rows.append(torch.lerp(grid[nearest], grid[nearest + 1], weight))
    return torch.cat(rows) if rows else grid.new_empty((0, grid.shape[1]))


def _patched_projection(previous_forward, projection, adapter, curve, grid, strength):
    import torch.nn.functional as F

    def forward(t_emb):
        # Invoke the exact pending forward patch so ordered LoRAs compose.
        outputs = previous_forward(t_emb)
        dtype, device = outputs[0].dtype, outputs[0].device
        original_time = interpolate_time_grid(t_emb, curve, grid).to(device=device, dtype=dtype)
        delta = F.linear(F.linear(original_time, adapter.down.to(device=device, dtype=dtype)),
                         adapter.up.to(device=device, dtype=dtype)) * (strength * adapter.scale)
        delta = delta.view(t_emb.shape[0] * projection.modalities, projection.expand * projection.hidden)
        return tuple(value + update for value, update in zip(outputs, delta.chunk(projection.expand, dim=-1)))

    return forward


def apply_curve_lora(model, adapters, strength, *, grid=None):
    """Install forward-attribute patches without changing the module tree."""
    import torch
    if not adapters:
        return 0
    grid = load_time_embedding_grid() if grid is None else grid
    curve = model.get_model_object("diffusion_model.adaln_t_table").detach().to(device="cpu", dtype=torch.float32).clone()
    if grid.ndim != 2 or grid.shape[0] != curve.shape[0] or grid.shape[1] != FULL_TIME_DIM:
        raise ValueError("H3 AdaLN checkpoint curve and compatibility grid have incompatible shapes")
    for adapter in adapters:
        projection = model.get_model_object(adapter.projection)
        if projection.apply_silu:
            raise ValueError("H3 full-width AdaLN compatibility requires a pruned curve projection")
        forward_path = adapter.projection + ".forward"
        previous_forward = model.get_model_object(forward_path)
        model.add_object_patch(forward_path, _patched_projection(previous_forward, projection, adapter,
                                                               curve, grid, strength))
    logging.info("Applied %d full-width AdaLN LoRA projections through the original time-embedding grid", len(adapters))
    return len(adapters)
