"""Portable H3MD Director settings. No inference imports or server paths."""
from copy import deepcopy
import math
import re

RECIPE = "h3md"
REVISION = "5c2170620abd3baf739b4be7cb72960be99d713a"
SOURCE = "https://github.com/1113800942/comfy-h3md-director"
ATTRIBUTION = "Based on H3MD Director (ComfyUI-H3MD-Director) by YJ"
MODEL = "minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors"
UPSCALER = "h3_upscaler_sharpness_2000steps_v0.1_fp32.safetensors"
COMPONENTS = {
    "model": MODEL, "encoder": "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors", "latent_upscaler": UPSCALER,
}
# Ordered as in the author's lite graph. Zero means disabled in that pass.
LORAS = (
    ("minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors", 1., 1.),
    ("minimax_h3_fl2v_turbo_4step_v1.3_768p_comfyui_bf16.safetensors", 1., 1.),
    ("H3_Combat_V2.safetensors", .7, .49),
    ("minimax_h3_turbo_v4_step600_ema_DasiwaREF2VAHybridV1_0_curveproj1025_compat_v001.safetensors", .9, .5),
    ("minimax_h3_turbo_4step_10ErosMax_test4_pruned_curveproj1025_exp_v001-T8.safetensors", .5, .7),
    ("MiniMaxH3_Enhancer_0.5-1.0_3432130_epoch_10.safetensors", .5, 0.),
    ("h3-realism-people-t2v-i2v-r2v.safetensors", 0., .5),
    ("minimax_h3_lms_v1.0_r64.safetensors", 0., .2),
)
GENERATION = {"recipe": RECIPE, "mode": "ref2va", "width": 704, "height": 1280,
              "frames": 158, "steps": 8, "seed": 42, "sampler": "euler", "scheduler": "simple",
              "shift_video": 1, "shift_audio": 1, "reference_size": "match"}
DEFAULTS = {
    "preset": "core", "transition_step": 6, "lowres_scale": .5,
    "rho": 0., "w_min": .5, "w_max": 1., "cfg": 1., "highres_tiling": True,
    "high_res_strengths": [1., 1.], "seed_mode": "increment", "lowres_anchor": 0.,
    "stop_after": 0, "rerun_segments": "", "rerun_new_seed": True, "resume_from_cache": False,
    "continue_audio": True, "drift_strength": .6, "drift_lock_tokens": 2,
    "audio_crossfade_ms": 0, "crf": 19,
    "shots": [{"id": "shot-1", "prompt": "", "frames": 158, "overlap": 0,
               "images": [], "videos": [], "audios": []}],
}


def number(value, field, low, high, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not low <= value <= high or integer and type(value) is not int):
        raise ValueError(f"H3MD {field} must be {'an integer' if integer else 'a number'} from {low} to {high}")
    return value


def text(value, field, limit=16000):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"H3MD {field} must be text of at most {limit} characters")
    return value


def validate_options(raw):
    if not isinstance(raw, dict) or raw.keys() - DEFAULTS.keys():
        raise ValueError("Unknown H3MD Director setting")
    value = deepcopy(DEFAULTS)
    value.update(deepcopy(raw))
    for key, low, high, integer in (("transition_step", 1, 99, True), ("lowres_scale", .25, .75, False),
            ("rho", 0, 1, False), ("w_min", 0, 1, False), ("w_max", 0, 1, False), ("cfg", 1, 10, False),
            ("lowres_anchor", 0, 1, False), ("stop_after", 0, 255, True), ("drift_strength", 0, 1, False),
            ("drift_lock_tokens", 0, 8, True), ("audio_crossfade_ms", 0, 2000, True), ("crf", 0, 51, True)):
        number(value[key], key.replace('_', ' '), low, high, integer=integer)
    if value["w_min"] > value["w_max"]:
        raise ValueError("H3MD minimum upscale weight cannot exceed its maximum")
    for key in ("highres_tiling", "rerun_new_seed", "resume_from_cache", "continue_audio"):
        if type(value[key]) is not bool:
            raise ValueError(f"H3MD {key} must be a switch")
    if (not isinstance(value["preset"], str) or value["preset"] not in {"core", "website_lite", "custom"}
            or not isinstance(value["seed_mode"], str) or value["seed_mode"] not in {"increment", "same"}):
        raise ValueError("Choose a supported H3MD preset and seed mode")
    rerun = text(value["rerun_segments"], "segments to redo", 200).strip()
    if rerun and not re.fullmatch(r"[1-9]\d*(?:\s*-\s*[1-9]\d*)?(?:\s*,\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?)*", rerun):
        raise ValueError("List segments to redo as 3, 4-6, or 8-10, 13-14")
    if rerun and not value["resume_from_cache"]:
        raise ValueError("Redoing selected segments requires a saved H3MD run")
    value["rerun_segments"] = rerun
    strengths = value["high_res_strengths"]
    if not isinstance(strengths, list) or len(strengths) > 16:
        raise ValueError("Invalid H3MD final-pass LoRA strengths")
    for strength in strengths:
        number(strength, "final-pass LoRA strength", -100, 100)
    shots = value["shots"]
    if not isinstance(shots, list) or not 1 <= len(shots) <= 64:
        raise ValueError("H3MD needs 1–64 shots")
    seen, total = set(), 0
    for i, shot in enumerate(shots):
        if not isinstance(shot, dict) or shot.keys() - {"id", "prompt", "frames", "overlap", "images", "videos", "audios"}:
            raise ValueError("Invalid H3MD shot")
        identity = text(shot.get("id"), "shot identifier", 100)
        if not identity or identity in seen:
            raise ValueError("H3MD shots need distinct identifiers")
        seen.add(identity)
        text(shot.get("prompt", ""), "shot prompt")
        frames = number(shot.get("frames"), "shot frames", 5, 14405, integer=True)
        overlap = shot.get("overlap", 0)
        if type(overlap) is not int or overlap not in {0, 22, 39} or i == 0 and overlap or frames <= overlap or i > 0 and shots[i - 1]["frames"] <= overlap:
            raise ValueError("First shots start with a cut; later shots use a cut or 22/39-frame overlap")
        if (frames - 5) % 17:
            raise ValueError("H3MD shot lengths must be 5 + 17 × n frames")
        total += frames - overlap
        for key, maximum in (("images", 9), ("videos", 3), ("audios", 3)):
            refs = shot.get(key, [])
            if not isinstance(refs, list) or len(refs) > maximum:
                raise ValueError(f"Too many H3MD shot {key}")
            indices = []
            for ref in refs:
                if key == "images":
                    index = ref
                else:
                    allowed = {"index", "offset", "soundtrack"} if key == "videos" else {"index", "offset", "mode"}
                    if not isinstance(ref, dict) or ref.keys() - allowed:
                        raise ValueError(f"Invalid H3MD shot {key} reference")
                    index = ref.get("index")
                    number(ref.get("offset", 0), "reference start", 0, 86400)
                    if key == "videos" and type(ref.get("soundtrack", False)) is not bool:
                        raise ValueError("Video soundtrack must be a switch")
                    if key == "audios" and (not isinstance(ref.get("mode", "reference"), str) or ref.get("mode", "reference") not in {"reference", "locked"}):
                        raise ValueError("Audio must be a reference or a locked soundtrack")
                number(index, "reference index", 0, maximum - 1, integer=True)
                indices.append(index)
            if len(set(indices)) != len(indices):
                raise ValueError("An H3MD shot cannot use the same reference twice")
    if total > 24 * 600:
        raise ValueError("An H3MD plan may contain at most ten minutes of video")
    return value


def validate(config, counts):
    options = validate_options(config.get(RECIPE, {}))
    if config.get("mode") != "ref2va":
        raise ValueError("H3MD uses the Reference model path")
    if config["width"] % 64 or config["height"] % 64:
        raise ValueError("H3MD dimensions must be multiples of 64 for its half-size first pass")
    if options["transition_step"] >= config["steps"]:
        raise ValueError("Leave at least one sampling step after H3MD latent upscaling")
    if len(options["high_res_strengths"]) != len(config.get("loras", [])):
        raise ValueError("Each selected H3MD LoRA needs a first-pass and final-pass strength")
    if not config.get("prompt", "").strip() and not any(s.get("prompt", "").strip() for s in options["shots"]):
        raise ValueError("Write an overall prompt or a shot prompt")
    for shot in options["shots"]:
        for kind, field in (("images", "reference_images"), ("videos", "reference_videos"), ("audios", "reference_audio")):
            for ref in shot.get(kind, []):
                index = ref if kind == "images" else ref["index"]
                if index >= counts.get(field, 0):
                    raise ValueError(f"Attach the {kind} selected by this H3MD shot")
    return options
