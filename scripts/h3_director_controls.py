"""Director's independent timeline and seed-hunt contract; no ML imports."""
from copy import deepcopy
import json
import math

RECIPE = "director"
SAMPLERS = ("res_multistep", "euler", "euler_ancestral", "dpmpp_2m")
SCHEDULERS = ("simple", "normal", "beta", "sgm_uniform")
SEED_STRIDE = 1_000_003
# Active saved example, with Corey's explicit Heretic Qwen encoder override.
WORKFLOW_SOURCE = {
    "url": "https://github.com/muse-collective-26/MiniMaxH3-Director-Seed-Hunt/blob/d07e8408f3809af7d1406a653da3fb44152b0af6/workflows/muse_minimax_h3_director_scout_v1.json",
    "revision": "d07e8408f3809af7d1406a653da3fb44152b0af6",
    "sha256": "14804582ba07888e8eda1616a06e5ff229caf7bb29df70bf013a2e35c1fa7d73",
}
WORKFLOW_COMPONENTS = {
    "model": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    "model_fl2va": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    "encoder": "qwen3vl_32b_heretic_minimax_h3_nvfp4.safetensors",
    "video_vae": "minimax_h3_video_vae_fp16.safetensors",
    "audio_vae": "minimax_h3_audio_vae_fp32.safetensors",
    "lora": "minimax_h3_turbo_4step_ema_ckpt500_pruned_comfyui.safetensors",
}
GENERATION_DEFAULTS = {
    "recipe": RECIPE, "mode": "ref2va", "width": 608, "height": 352,
    "frames": 158, "steps": 16, "seed": 50, "sampler": "euler", "scheduler": "beta",
    "shift_video": 12, "shift_audio": 3, "reference_size": "match",
}
REFINEMENT_DEFAULTS = {
    "width": 768, "height": 1376, "megapixels": 1, "steps": 20,
    "sampler": "euler", "scheduler": "beta", "denoise": .2,
}
DEFAULTS = {
    "stage": "generation", "duration_seconds": 6.0, "chunk_duration_seconds": 15.0,
    "resize_method": "crop", "hybrid_continuation": False,
    "candidate_2": False, "candidate_3": False, "candidate_4": False,
    "segments": [{"id": "cut-1", "prompt": "", "weight": 1.0, "speakerCharIdxs": []}],
    "characters": [], "background": None, "refVideos": [], "refAudios": [],
    "dialogue_language": "English", "overall_soundscape": "", "non_diegetic_music": "",
    "denoise": 0.2,
}
IMAGE_RETENTION = ("fully_preserved", "partially_preserved", "attribute_transfer", "weak_reference")
VIDEO_RETENTION = IMAGE_RETENTION
AUDIO_RETENTION = ("reference", "fully_copy", "partially_copy", "weak_reference")


def number(value, name, low, high, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high
            or integer and value != int(value)):
        raise ValueError(f"Director {name} must be {'an integer' if integer else 'a number'} from {low} to {high}")
    return int(value) if integer else float(value)


def text(value, name, limit=16000):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"Director {name} must be text of at most {limit} characters")
    return value


def _object(value, allowed, name):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"Invalid Director {name}")
    return value


def _reference(value, kind, index):
    image = kind == "image"
    allowed = {"image_index" if image else "input_index", "name", "description", "retention"}
    if not image:
        allowed |= {"trimStartSec", "trimEndSec"}
    if kind == "video":
        allowed |= {"includeAudio", "role"}
    _object(value, allowed, f"{kind} reference")
    result = dict(value)
    key = "image_index" if image else "input_index"
    result[key] = number(value.get(key, index), key, 0, 8 if image else 2, integer=True)
    for name in ("name", "description"):
        result[name] = text(value.get(name, ""), name, 2000)
    choices = IMAGE_RETENTION if image else VIDEO_RETENTION if kind == "video" else AUDIO_RETENTION
    default_retention = "fully_preserved" if image or kind == "video" and result["description"] else "weak_reference" if kind == "video" else "reference"
    result["retention"] = value.get("retention", default_retention)
    if result["retention"] not in choices:
        raise ValueError(f"Choose a supported Director {kind} retention")
    if not image:
        result["trimStartSec"] = number(value.get("trimStartSec", 0), "reference start", 0, 86400)
        end = value.get("trimEndSec")
        result["trimEndSec"] = None if end is None else number(end, "reference end", 0, 86400)
        if end is not None and end <= result["trimStartSec"]:
            raise ValueError("Director reference end must follow its start")
    if kind == "video":
        result["includeAudio"] = value.get("includeAudio", False)
        if type(result["includeAudio"]) is not bool:
            raise ValueError("Director reference-video audio must be on or off")
        result["role"] = value.get("role", "reference")
        if result["role"] not in ("reference", "editing_source", "continuation_source"):
            raise ValueError("Choose a supported Director video role")
    return result


def validate_options(raw):
    _object(raw, DEFAULTS, "settings")
    result = {**deepcopy(DEFAULTS), **deepcopy(raw)}
    if not isinstance(result["stage"], str) or result["stage"] not in {"generation", "refine"}:
        raise ValueError("Choose Director generation or refinement")
    for key in ("hybrid_continuation", "candidate_2", "candidate_3", "candidate_4"):
        if type(result[key]) is not bool:
            raise ValueError(f"Director {key.replace('_', ' ')} must be on or off")
    for key, low, high in (("duration_seconds", 1, 120), ("chunk_duration_seconds", 3, 15), ("denoise", .05, 1)):
        result[key] = number(result[key], key.replace("_", " "), low, high)
    if not isinstance(result["resize_method"], str) or result["resize_method"] not in {"crop", "pad", "stretch"}:
        raise ValueError("Choose crop, pad or stretch for Director references")
    for key in ("dialogue_language", "overall_soundscape", "non_diegetic_music"):
        result[key] = text(result[key], key.replace("_", " "))
    segments = result["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 32:
        raise ValueError("Director needs 1–32 timeline cuts")
    ids = set()
    for index, segment in enumerate(segments):
        _object(segment, {"id", "prompt", "weight", "speakerCharIdxs"}, "timeline cut")
        segment["id"] = text(segment.get("id", f"cut-{index + 1}"), "cut ID", 64)
        if not segment["id"] or segment["id"] in ids:
            raise ValueError("Director cuts need distinct IDs")
        ids.add(segment["id"])
        segment["prompt"] = text(segment.get("prompt", ""), "cut prompt")
        segment["weight"] = number(segment.get("weight", 1), "cut weight", .01, 1000)
        speakers = segment.get("speakerCharIdxs", [])
        if not isinstance(speakers, list) or len(speakers) > 9:
            raise ValueError("Choose up to nine speaking references per cut")
        segment["speakerCharIdxs"] = list(dict.fromkeys(number(i, "speaker reference", 0, 8, integer=True) for i in speakers))
    for key, kind, maximum in (("characters", "image", 9), ("refVideos", "video", 3), ("refAudios", "audio", 3)):
        values = result[key]
        if not isinstance(values, list) or len(values) > maximum:
            raise ValueError(f"Director supports up to {maximum} {key}")
        result[key] = [_reference(value, kind, i) for i, value in enumerate(values)]
        indices = [value["image_index" if kind == "image" else "input_index"] for value in result[key]]
        if len(indices) != len(set(indices)):
            raise ValueError(f"Director {key} cannot repeat an input")
    if result["background"] is not None:
        result["background"] = _reference(result["background"], "image", 0)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 80 * 1024:
        raise ValueError("Director's timeline and reference descriptions exceed 80 KiB")
    return result


def candidate_indices(options):
    return [1] + [i for i in (2, 3, 4) if options.get(f"candidate_{i}")]


def chunk_lengths(options):
    duration, size = options["duration_seconds"], options["chunk_duration_seconds"]
    result = []
    while duration > size + 1e-8:
        result.append(size)
        duration -= size
    result.append(duration)
    if len(result) > 1 and result[-1] < 4:
        total = result[-2] + result[-1]
        # Avoid upstream's short-tail merge producing a call beyond H3's 15s limit.
        result[-2:] = [total] if total <= 15 else [total - 4, 4.0]
    return result


def validate_inputs(config, counts):
    options = config[RECIPE]
    if not config.get("prompt", "").strip() and not any(s["prompt"].strip() for s in options["segments"]):
        raise ValueError("Write a Director style prompt or at least one timeline cut")
    if config["seed"] + (max(candidate_indices(options)) - 1) * SEED_STRIDE + len(chunk_lengths(options)) - 1 > 2**53 - 1:
        raise ValueError("Director candidate and chunk seeds must fit a 53-bit integer")
    if config["mode"] == "fl2va":
        if options["hybrid_continuation"]:
            raise ValueError("Hybrid continuation belongs to Director Reference mode")
        if any(counts.get(k, 0) for k in ("reference_images", "reference_videos", "reference_audio")):
            raise ValueError("Director First/Last Frame mode uses its first and last frame inputs")
        return
    elif config["mode"] != "ref2va":
        raise ValueError("Choose Director Reference or First/Last Frame mode")
    if options["hybrid_continuation"] and not config.get("model_fl2va"):
        raise ValueError("Choose a First/Last Frame checkpoint for hybrid continuation")
    for key, field in (("characters", "reference_images"), ("refVideos", "reference_videos"), ("refAudios", "reference_audio")):
        for row in options[key]:
            if row["image_index" if key == "characters" else "input_index"] >= counts.get(field, 0):
                raise ValueError(f"Attach the Director {field.replace('_', ' ')} used by its settings")
    background = options["background"]
    if background and background["image_index"] >= counts.get("reference_images", 0):
        raise ValueError("Attach the Director location reference")
