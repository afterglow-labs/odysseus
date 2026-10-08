"""Identify VFX adapters and migrate the old source-in-reference-slot layout."""
from pathlib import PurePosixPath

VFX_LORAS = {"minimax_h3_vfx_edit_v1.0_r128.safetensors",
             "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"}


def is_vfx_config(config):
    rows = config.get("loras") if "loras" in config else [
        {"path": config.get("lora", ""), "strength": config.get("lora_scale", 1)}]
    return any(isinstance(row, dict) and row.get("strength", 1) != 0
               and PurePosixPath(str(row.get("path", "")).replace("\\", "/")).name.lower() in VFX_LORAS
               for row in rows or [])


def normalize_vfx_inputs(config, inputs):
    """Only old layouts lacking the source field can promote a lone video.

    Values can be paths, upload lists, or retained-file descriptors. Preserve
    those objects and never mutate an existing job or shared batch reference.
    """
    result = dict(inputs)
    refs = result.get("reference_videos") or []
    if ("source_video" not in result and is_vfx_config(config)
            and isinstance(refs, list) and len(refs) == 1):
        result["source_video"] = refs[:]
        result["reference_videos"] = []
    return result
