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
SCOUT_UPSCALE_DEFAULTS = {"scout_upscale": False, "scout_final_width": None,
                          "scout_final_height": None, "scout_refine_steps": 2}


def scout_upscale_enabled(config):
    options = config.get("seed_hunter", {})
    return options.get("stage") == "scout" and options.get("scout_mode", "interactive") != "scout" and options.get("scout_upscale", False)


def scout_output_size(config):
    options = config["seed_hunter"]
    return tuple(options.get("scout_final_" + axis) or config[axis] * 2 for axis in ("width", "height"))


def validate_scout_output(config):
    if not scout_upscale_enabled(config):
        return
    width, height = scout_output_size(config)
    if width < config["width"] or height < config["height"]:
        raise ValueError("Seed Scout final dimensions must be at least the scouting dimensions")
    if width > 4096 or height > 4096 or width * height > 4194304:
        raise ValueError("Seed Scout final dimensions must fit 4096 per side and 4,194,304 pixels; set a smaller final size")


def validate_options(raw):
    if not isinstance(raw, dict) or raw.keys() - (DEFAULTS.keys() | SCOUT_UPSCALE_DEFAULTS.keys() | {"auto_refine"}):
        raise ValueError("Unknown Seed Hunter setting")
    # Keep legacy defaults unchanged for clients that validate their known
    # options strictly. Only opted-in submissions receive the new settings.
    result = {**DEFAULTS, **(SCOUT_UPSCALE_DEFAULTS if raw.keys() & SCOUT_UPSCALE_DEFAULTS.keys() else {}), **raw}
    enums = {"stage": ("preview", "final", "single", "continuation", "dialogue", "scout"),
             "scout_mode": ("interactive", "scout", "final"),
             "scout_preview_mode": ("vae", "latent2rgb"),
             "attention_backend": ("comfy kitchen attention", "pytorch"),
             "sparse_method": ("none", "sla", "sol_attn", "vsa"),
             "sparse_sink_conditioning": ("exact_kv_and_rows", "none")}
    for key, choices in enums.items():
        if not isinstance(result[key], str) or result[key] not in choices:
            raise ValueError(f"Unsupported Seed Hunter {key}")
    boolean_keys = ["shift_enabled", "first_frame_guide", "last_frame_guide", "force_audio",
                    "upscaler_chunking", "upscaler_force_unload"]
    if "scout_upscale" in result:
        boolean_keys.append("scout_upscale")
    for key in boolean_keys:
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
    if "scout_upscale" in result:
        ranges.update(scout_final_width=(32, 4096, True), scout_final_height=(32, 4096, True),
                      scout_refine_steps=(1, 100, True))
    for key, (low, high, integer) in ranges.items():
        value = result[key]
        if value is None and key in {"source_end_seconds", "audio_end_seconds", "final_frames", "scout_final_width", "scout_final_height"}:
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high
                or not math.isfinite(value) or integer and type(value) is not int):
            raise ValueError(f"Invalid Seed Hunter {key}")
    if result["final_frames"] is not None and (result["final_frames"] - 5) % 17:
        raise ValueError("Final H3 frame count must be 17 × n + 5 (73, 90, 107 … 362)")
    for key in ("scout_final_width", "scout_final_height"):
        if result.get(key) is not None and result[key] % 32:
            raise ValueError("Seed Scout final dimensions must be multiples of 32")
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
    if "auto_refine" in result:
        try:
            from scripts.h3_auto_refine_controls import validate
        except ModuleNotFoundError:
            from h3_auto_refine_controls import validate
        result["auto_refine"] = validate(result["auto_refine"], result)
    return result


def limits(options):
    stage = options["stage"]
    return {"min_edge": 64 if stage == "dialogue" else 256, "max_edge": 4096,
            "max_pixels": 1048576 if stage in {"preview", "dialogue"} else 4194304,
            "min_frames": 5 if stage == "continuation" else 73, "max_frames": 362}
