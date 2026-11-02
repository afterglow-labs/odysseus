"""Small shared adapter controls for the API and standalone H3 worker."""
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile


AGE_SLIDER_LORA = "MM-H3 - Ref2VA - Age Slider v2 - EXPERIMENTAL.safetensors"


def lora_strength_control(name):
    if Path(str(name)).name.casefold() != AGE_SLIDER_LORA.casefold():
        return None
    return {"kind": "age", "label": "Age", "min": -5, "max": 5, "step": 0.1,
            "default": 0, "labels": {"min": "Youngest", "zero": "Neutral", "max": "Oldest"}}


PROFILE_LIMIT = 65536


def built_in_lora_profile(name):
    """Editable defaults for known adapters; a saved profile always wins."""
    name = Path(str(name)).name.casefold()
    turbo_steps = {
        "minimax_h3_ref2v_turbo_4step_v1.0_768p_comfyui_bf16.safetensors": 4,
        "minimax_h3_ref2v_turbo_8step_v1.1_768p_comfyui_bf16.safetensors": 8,
    }
    if name in turbo_steps:
        steps = turbo_steps[name]
        return validate_lora_profile({
            "display_name": f"LightX2V Ref2VA Turbo · {steps} steps", "variant": "ref2va",
            "notes": "Ref2VA acceleration adapter, strength 1. Use matching reference sizing. Seed Hunter has a complete preset; in Seed Scout, set Preview after step below the full step count. The final latent refinement has its own schedule. Source: https://github.com/ModelTC/Minimax-H3-Turbo",
            "recommended_settings": {"steps": steps, "sampler": "euler", "scheduler": "simple", "shift_video": 6, "shift_audio": 3, "reference_size": "match"},
        })
    if name == "h3_character_swap_v1.5.safetensors":
        return validate_lora_profile({
            "display_name": "Character Swap V1.5", "variant": "ref2va",
            "notes": "Use a Ref2VA base, this adapter at strength 1, and minimax_h3_fl2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors at strength 1. Source performance: Video 1. Replacement identity: Picture 1. The LoRA alone does not supply SAM masks or preserve the source soundtrack. Publisher: https://huggingface.co/akatz-ai/MiniMax-H3-Character-Swap-LoRA",
            "prompt_example": "Replace the selected person in <Video 1> with the character in <Picture 1>. Keep the reference character's face, hair and clothing. Follow the source performance's pose, gaze, expression and movement. Preserve the scene and the other people.",
            "recommended_settings": {"steps": 8, "sampler": "er_sde", "scheduler": "normal", "shift_video": 12, "shift_audio": 3},
        })
    if name == "pika_h3_flex_ref2va_v1_bf16.safetensors":
        return validate_lora_profile({
            "display_name": "Pika Flex Ref2VA", "variant": "ref2va",
            "notes": "Choose the Pika Flex preset in H3 Seed Hunter for the required six-step LCM manual sigma schedule. Ordinary H3 and Scout continuation do not apply this schedule. Source: https://huggingface.co/Pika-Labs/Pika-H3-Flex",
        })
    if name not in {"minimax_h3_vfx_edit_v1.0_r128.safetensors",
                    "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"}:
        return None
    ffp = name.endswith("_ffp.safetensors")
    notes = (
        "Start with LoRA strength 1.0. Use the Ref2VA model and one Source video "
        "as the aligned edit guide. Separate image, video and audio references "
        "are optional. State the requested edit, then what should stay unchanged. "
        "Odysseus adds vfx_edit: once at the beginning when submitting. "
    )
    if ffp:
        notes += ("FFP means first-frame propagation. If using an edited first frame, "
                  "attach it as a reference image and describe <Picture 1>'s role. ")
    notes += "Author guide: https://huggingface.co/Alissonerdx/Minimax-H3-ComfyUI/blob/main/docs/vfx-edit.md"
    return validate_lora_profile({
        "display_name": "VFX Edit FFP · v1.0 r128" if ffp else "VFX Edit · v1.0 r128",
        "variant": "ref2va", "trigger_words": "vfx_edit:", "trigger_position": "prefix",
        "notes": notes,
    })


def _text(value, limit, field):
    if not isinstance(value, str) or len(value) > limit or "\0" in value:
        raise ValueError(f"{field} must be text of at most {limit} characters")
    return value.strip()


def _number(value, low, high, field, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high
            or not math.isfinite(value) or integer and type(value) is not int):
        raise ValueError(f"{field} must be a finite {'integer' if integer else 'number'} from {low} to {high}")
    return value


def validate_lora_profile(raw):
    allowed = {"display_name", "trigger_words", "trigger_position", "notes", "prompt_example", "variant", "strength_control", "recommended_settings"}
    if not isinstance(raw, dict) or raw.keys() - allowed:
        raise ValueError("Unknown LoRA profile field")
    result = {key: _text(raw.get(key, ""), limit, key) for key, limit in (
        ("display_name", 160), ("trigger_words", 4000), ("notes", 8000))}
    if "prompt_example" in raw:
        result["prompt_example"] = _text(raw["prompt_example"], 16000, "Example prompt")
    for key, default, choices in (("trigger_position", "manual", {"prefix", "suffix", "manual"}),
                                  ("variant", "shared", {"ref2va", "fl2va", "shared"})):
        value = raw.get(key, default)
        if not isinstance(value, str) or value not in choices:
            raise ValueError(f"Choose a supported {key}")
        result[key] = value
    control = raw.get("strength_control")
    if control is not None:
        if not isinstance(control, dict) or control.keys() - {"kind", "label", "min", "max", "step", "default", "labels"}:
            raise ValueError("Invalid LoRA strength control")
        low = _number(control.get("min"), -100, 100, "Minimum strength")
        high = _number(control.get("max"), -100, 100, "Maximum strength")
        if low >= high:
            raise ValueError("Minimum strength must be smaller than maximum strength")
        step = _number(control.get("step"), .0001, high - low, "Strength step")
        default = _number(control.get("default"), low, high, "Default strength")
        kind = control.get("kind", "custom")
        if not isinstance(kind, str) or kind not in {"custom", "age"}:
            raise ValueError("Unsupported strength control kind")
        if kind == "age" and (low, high, step, default) != (-5, 5, .1, 0):
            raise ValueError("The Age control uses -5 to 5, step 0.1, and default 0; choose a custom control for other ranges")
        normalized = {"kind": kind, "label": _text(control.get("label", "Strength"), 80, "Strength label"),
                      "min": low, "max": high, "step": step, "default": default}
        labels = control.get("labels", {})
        if not isinstance(labels, dict) or labels.keys() - {"min", "zero", "max"}:
            raise ValueError("Invalid strength labels")
        if labels:
            normalized["labels"] = {key: _text(value, 80, "Strength label") for key, value in labels.items()}
        result["strength_control"] = normalized
    settings = raw.get("recommended_settings", {})
    ranges = {"steps": (1, 100), "width": (256, 1920), "height": (256, 1920),
              "frames": (73, 362), "seed": (0, 2**63 - 1)}
    enums = {"sampler": {"euler", "res_multistep", "er_sde"}, "scheduler": {"simple", "normal"}, "reference_size": {"match", "max"}}
    if not isinstance(settings, dict) or settings.keys() - (ranges.keys() | enums.keys() | {"shift_video", "shift_audio"}):
        raise ValueError("Unsupported recommended workflow setting")
    for key, value in settings.items():
        if key in ranges:
            _number(value, *ranges[key], key, integer=True)
        elif key in enums:
            if not isinstance(value, str) or value not in enums[key]:
                raise ValueError(f"Unsupported recommended {key}")
        else:
            _number(value, .01, 100, key)
        if key in {"width", "height"} and value % 32:
            raise ValueError("Recommended dimensions must be multiples of 32")
        if key == "frames" and (value - 5) % 17:
            raise ValueError("Recommended frames must be 17 × n + 5")
    if settings.get("width", 256) * settings.get("height", 256) > 768 * 1344:
        raise ValueError("Recommended dimensions exceed the H3 canvas limit")
    result["recommended_settings"] = dict(settings)
    return result


def _profile_path(path):
    path = Path(path)
    identity = hashlib.sha256(path.name.encode()).hexdigest()[:16]
    return path.with_name(".odysseus-h3-lora-" + identity + ".json")


def read_profile(path):
    target = _profile_path(path)
    try:
        if target.is_symlink() or target.stat().st_size > PROFILE_LIMIT:
            return None
        document = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or document.get("version") != 1 or document.get("filename") != Path(path).name:
            return None
        return validate_lora_profile(document.get("profile"))
    except (OSError, ValueError, TypeError):
        return None


def write_profile(path, profile, *, overwrite=False):
    """Atomic data-only sidecar, kept beside weights when libraries are moved."""
    from src.model_library import _metadata_lock
    path, profile = Path(path), validate_lora_profile(profile)
    if not path.is_file() or path.suffix.lower() != ".safetensors":
        raise ValueError("Choose an installed safetensors adapter")
    target = _profile_path(path)
    with _metadata_lock(path.parent):
        existing = read_profile(path)
        if existing is not None and not overwrite:
            return existing
        if existing and "prompt_example" not in profile and "prompt_example" in existing:
            # Older clients do not send this optional field. Only an explicit
            # empty value clears an example when another profile field changes.
            profile["prompt_example"] = existing["prompt_example"]
        if target.is_symlink():
            raise ValueError("A LoRA profile cannot be a symbolic link")
        value = json.dumps({"version": 1, "filename": path.name, "profile": profile}, ensure_ascii=False).encode()
        if len(value) > PROFILE_LIMIT:
            raise ValueError("LoRA profile exceeds the metadata limit")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".lora-profile-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return profile


def lora_strength_bounds(name, profile=None):
    profile = validate_lora_profile(profile) if profile is not None else None
    control = profile.get("strength_control") if profile else None
    control = control or lora_strength_control(name)
    return (control["min"], control["max"]) if control else (-4, 4)
