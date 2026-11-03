"""Shared, data-only contracts for Seed Hunter sampling presets.

Pika's six-step schedule comes from Comfy-Org's video_pika_h3_flex_ref2va
template. Named presets carry separate preview and refinement settings.
"""
import math

PRESET_IDS = ("custom", "quality", "turbo4", "turbo8", "pika", "lightvae", "director", "prism8", "character_swap", "pinkcherry12", "pinkcherry4")
SAMPLING_SCHEDULES = ("standard", "pika")
PIKA_SIGMAS = (1., .972973, .923077, .8, .571429, .333333, 0.)
TURBO4 = "minimax_h3_ref2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors"
TURBO8 = "minimax_h3_ref2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors"
PIKA = "pika_h3_flex_ref2va_v1_bf16.safetensors"
LIGHTVAE = "minimax_h3_lightvae_lite_vae_int8_convrot.safetensors"
CHARACTER_SWAP = "h3_character_swap_v1.5.safetensors"
CHARACTER_TURBO = "minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
PRISM8 = "10Eros_Max_h3_8stepTURBO_beta7_SingularityV2_int8HQ.safetensors"
PINKCHERRY = "PinkCherry_hybrid_v1.8_int8_minimax_h3_pruned.safetensors"


def final_size(config, megapixels=2):
    """Match the clients' aspect-preserving 32-pixel sizing without shrinking."""
    width, height = config["width"], config["height"]
    scale = max(1., min(math.sqrt(megapixels * 1024 * 1024 / (width * height)),
                        4096 / max(width, height)))
    return {key: max(value, min(4096, math.floor(value * scale / 32 + .5) * 32))
            for key, value in (("width", width), ("height", height))}


def compatible_adapter(name, variant, wanted, active_names, base_name=""):
    # The Character Swap publisher explicitly tests this one FL Turbo adapter
    # with Ref2VA. Other FL adapters still require their matching checkpoint.
    return variant in {"shared", wanted} or (wanted == "ref2va" and (
        name == CHARACTER_TURBO and (CHARACTER_SWAP in active_names or base_name == PINKCHERRY)
        or name == "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors" and base_name == PINKCHERRY))



def candidate_limit(options):
    return 4 if options.get("preset") == "director" else 3


def candidate_stride(options):
    return 1_000_003 if options.get("preset") == "director" else 1000


def validate_sampling(config):
    options = config.get("seed_hunter", {})
    schedule = options.get("sampling_schedule", "standard")
    if schedule == "standard":
        return
    if schedule != "pika":
        raise ValueError("Unsupported sampling schedule")
    if options.get("stage") == "scout":
        raise ValueError("Pika's manual schedule is available in Seed Hunter, not Seed Scout")
    expected_steps, expected_sampler = 6, "lcm"
    if config.get("steps") != expected_steps or config.get("sampler") != expected_sampler:
        raise ValueError(f"The {schedule} schedule requires {expected_steps} steps and {expected_sampler} sampling")
    if options.get("shift_enabled"):
        raise ValueError(f"The {schedule} schedule already includes its sigma shifts")


def refinement_settings(preset):
    result = {"megapixels": 2, "steps": 2, "sampler": "er_sde",
              "scheduler": "linear_quadratic", "denoise": .4,
              "seed_hunter": {"sampling_schedule": "standard", "shift_enabled": False}}
    if preset == "director":
        result.update(steps=20, sampler="euler", scheduler="beta", denoise=.2,
                      shift_video=12, shift_audio=3)
        result["seed_hunter"].update(refinement_method="pixel", shift_enabled=True)
    elif preset == "character_swap":
        result.update(steps=8, sampler="er_sde", scheduler="normal")
        result["seed_hunter"]["shift_enabled"] = True
    elif preset == "prism8":
        result.update(steps=8, sampler="euler", scheduler="simple", shift_video=12, shift_audio=3)
        result["seed_hunter"]["shift_enabled"] = True
    return result
