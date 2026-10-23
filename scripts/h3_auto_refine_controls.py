"""Data-only settings for unattended candidate refinement."""
import math
import re

DEFAULTS = {"candidates": [], "project": "", "existing": "skip", "refinement": {}}
REFINEMENT_FIELDS = {"prompt", "width", "height", "frames", "steps", "sampler", "scheduler",
                     "gpu", "vae_gpu", "loras", "lora", "lora_scale", "latent_upscaler",
                     "denoise", "seed_hunter", "shift_video", "shift_audio"}
REFINEMENT_OPTIONS = {"denoise", "output_fps", "final_frames", "upscaler_chunking", "upscaler_force_unload", "shift_enabled",
                     "attention_backend", "sparse_method", "sparse_keep_percent", "sparse_start", "sparse_end",
                     "sparse_min_tokens", "sparse_extra_tokens", "sparse_sink_conditioning", "ff_chunk_size",
                     "ff_chunk_threshold", "low_vram_chunks"}


def enabled(options):
    return bool(options.get("auto_refine", {}).get("candidates"))


def slots(options):
    count = options["scout_count"] if options["stage"] == "scout" else options["preview_count"]
    chosen = options.get("auto_refine", {}).get("candidates", [])
    return list(range(1, count + 1)) if chosen == "all" else list(chosen)


def validate(value, options):
    if not isinstance(value, dict) or value.keys() - DEFAULTS.keys():
        raise ValueError("Unknown automatic refinement setting")
    result = {**DEFAULTS, **value}
    if result["existing"] not in ("skip", "rerender"):
        raise ValueError("Choose Skip existing refinements or Reprocess all selected candidates")
    chosen = result["candidates"]
    if chosen != "all":
        if (not isinstance(chosen, list) or len(chosen) > 32
                or any(type(v) is not int or not 1 <= v <= 32 for v in chosen)
                or len(set(chosen)) != len(chosen)):
            raise ValueError("Choose candidate numbers or All candidates for automatic refinement")
        result["candidates"] = sorted(chosen)
    project = result["project"]
    if (not isinstance(project, str) or len(project) > 80 or any(ord(c) < 32 for c in project)
            or re.search(r'[/\\:*?"<>|]', project) or project.strip() in {".", ".."}):
        raise ValueError("Project name must be at most 80 characters without folder separators")
    result["project"] = project.strip()
    patch = result["refinement"]
    if not isinstance(patch, dict) or patch.keys() - REFINEMENT_FIELDS:
        raise ValueError("Unsupported shared refinement setting; each candidate keeps its own seed")
    for key in ("width", "height"):
        if key in patch and (type(patch[key]) is not int or not 256 <= patch[key] <= 4096 or patch[key] % 32):
            raise ValueError("Final dimensions must be multiples of 32 from 256 to 4096")
    if "steps" in patch and (type(patch['steps']) is not int or not 1 <= patch['steps'] <= 100):
        raise ValueError("Refinement steps must be from 1 to 100")
    if "frames" in patch and (type(patch['frames']) is not int or not 73 <= patch['frames'] <= 362 or (patch['frames'] - 5) % 17):
        raise ValueError("Choose an available final video length")
    for key, choices in (("sampler", ("euler", "res_multistep", "er_sde")), ("scheduler", ("simple", "normal", "beta", "linear_quadratic"))):
        if key in patch and patch[key] not in choices:
            raise ValueError("Choose a supported refinement sampler and scheduler")
    for key in ("denoise", "lora_scale", "shift_video", "shift_audio"):
        if key in patch and (isinstance(patch[key], bool) or not isinstance(patch[key], (int, float)) or not math.isfinite(patch[key])):
            raise ValueError(f"Invalid refinement {key}")
    if 'denoise' in patch and not 0 <= patch['denoise'] <= 1:
        raise ValueError("Refinement denoise must be from 0 to 1")
    if not isinstance(patch.get('seed_hunter', {}), dict) or patch.get('seed_hunter', {}).keys() - REFINEMENT_OPTIONS:
        raise ValueError("Unsupported shared refinement option")
    for key in ("gpu", "vae_gpu", "latent_upscaler", "lora", "prompt"):
        if key in patch and (not isinstance(patch[key], str) or len(patch[key]) > (16000 if key == 'prompt' else 1024)):
            raise ValueError(f"Invalid refinement {key}")
    if chosen:
        if options["stage"] not in {"preview", "scout"} or options["stage"] == "scout" and options["scout_mode"] == "final":
            raise ValueError("Automatic refinement requires preview candidates or Seed Scout candidates")
        if not result["project"]:
            raise ValueError("Name the batch/project for automatic refinement outputs")
        count = options["scout_count"] if options["stage"] == "scout" else options["preview_count"]
        if chosen != "all" and max(chosen) > count:
            raise ValueError("An automatic refinement candidate exceeds the configured candidate count")
    return result


def final_size(config):
    settings = config["seed_hunter"]["auto_refine"]["refinement"]
    return tuple(settings.get(axis) or config[axis] * 2 for axis in ("width", "height"))


def validate_dimensions(config):
    options = config["seed_hunter"]
    if not enabled(options):
        return
    width, height = final_size(config)
    if width < config["width"] or height < config["height"] or max(width, height) > 4096 or width * height > 4194304:
        raise ValueError("Automatic final size must be at least the preview size, within 4096 per side and 4,194,304 pixels")
