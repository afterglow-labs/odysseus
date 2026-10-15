"""Data-only settings for the native port of Seed Hunter v2.5.1."""
import math

RECIPE = "seed_hunter"
SAMPLERS = ("euler", "res_multistep", "er_sde")
SCHEDULERS = ("simple", "normal", "beta", "linear_quadratic")
DEFAULTS = {
    "stage": "preview", "preview_count": 3, "denoise": .4, "final_frames": None,
    "shift_enabled": False, "first_frame_guide": False, "last_frame_guide": False,
    "force_audio": False, "continuation_overlap": 22, "output_fps": 24,
    "attention_backend": "comfy kitchen attention", "sparse_method": "none",
    "sparse_keep_percent": 10., "sparse_start": .2, "sparse_end": 1.,
    "sparse_min_tokens": 12288, "sparse_extra_tokens": 256,
    "sparse_sink_conditioning": "exact_kv_and_rows",
    "upscaler_chunking": True, "upscaler_force_unload": False,
    "ff_chunk_size": 0, "ff_chunk_threshold": 2048, "low_vram_chunks": 0,
    "source_start_seconds": 0., "source_end_seconds": None,
    "audio_start_seconds": 0., "audio_end_seconds": None,
    "refmod_strengths": [],
    "scout_count": 6, "scout_step": 3, "scout_mode": "interactive",
    "scout_timeout": 0, "scout_preview_mode": "vae",
}


def validate_options(raw):
    if not isinstance(raw, dict) or raw.keys() - DEFAULTS.keys():
        raise ValueError("Unknown Seed Hunter setting")
    result = {**DEFAULTS, **raw}
    enums = {"stage": ("preview", "final", "single", "continuation", "dialogue", "scout"),
             "scout_mode": ("interactive", "scout", "final"),
             "scout_preview_mode": ("vae", "latent2rgb"),
             "attention_backend": ("comfy kitchen attention", "pytorch"),
             "sparse_method": ("none", "sla", "sol_attn", "vsa"),
             "sparse_sink_conditioning": ("exact_kv_and_rows", "none")}
    for key, choices in enums.items():
        if not isinstance(result[key], str) or result[key] not in choices:
            raise ValueError(f"Unsupported Seed Hunter {key}")
    for key in ("shift_enabled", "first_frame_guide", "last_frame_guide", "force_audio",
                "upscaler_chunking", "upscaler_force_unload"):
        if type(result[key]) is not bool:
            raise ValueError(f"{key} must be true or false")
    ranges = {"preview_count": (1, 3, True), "denoise": (0., 1., False), "final_frames": (73, 362, True),
              "scout_count": (1, 32, True), "scout_step": (1, 100, True), "scout_timeout": (0, 86400, True),
              "sparse_keep_percent": (1., 100., False), "sparse_start": (0., 1., False),
              "sparse_end": (0., 1., False), "sparse_min_tokens": (0, 1000000, True),
              "sparse_extra_tokens": (0, 8192, True), "ff_chunk_size": (0, 64, True),
              "ff_chunk_threshold": (1, 1000000, True), "low_vram_chunks": (0, 64, True),
              "source_start_seconds": (0., 86400., False), "source_end_seconds": (0., 86400., False),
              "audio_start_seconds": (0., 3600., False), "audio_end_seconds": (0., 3600., False)}
    for key, (low, high, integer) in ranges.items():
        value = result[key]
        if value is None and key in {"source_end_seconds", "audio_end_seconds", "final_frames"}:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high
                or not math.isfinite(value) or integer and type(value) is not int):
            raise ValueError(f"Invalid Seed Hunter {key}")
    if result["final_frames"] is not None and (result["final_frames"] - 5) % 17:
        raise ValueError("Final H3 frame count must be 17 × n + 5 (73, 90, 107 … 362)")
    for prefix in ("source", "audio"):
        end = result[prefix + "_end_seconds"]
        if end is not None and end <= result[prefix + "_start_seconds"]:
            raise ValueError("Trim end must be after trim start")
    if result["sparse_start"] > result["sparse_end"]:
        raise ValueError("Sparse attention start must not exceed its end")
    if type(result["continuation_overlap"]) is not int or result["continuation_overlap"] not in {5, 22, 39, 56}:
        raise ValueError("Continuation overlap must be 5, 22, 39, or 56 frames")
    if type(result["output_fps"]) is not int or result["output_fps"] not in {24, 48}:
        raise ValueError("Output frame rate must be 24 or 48")
    strengths = result["refmod_strengths"]
    if not isinstance(strengths, list) or len(strengths) > 8 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1 or not math.isfinite(v)
            for v in strengths):
        raise ValueError("RefMod strengths must be a list of at most eight values from 0 to 1")
    result["refmod_strengths"] = list(strengths)
    if result["stage"] in {"single", "continuation", "final", "scout"}:
        result["preview_count"] = 1
    return result


def limits(options):
    stage = options["stage"]
    return {"min_edge": 64 if stage == "dialogue" else 256, "max_edge": 4096,
            "max_pixels": 1048576 if stage in {"preview", "dialogue"} else 4194304,
            "min_frames": 5 if stage == "continuation" else 73, "max_frames": 362}
