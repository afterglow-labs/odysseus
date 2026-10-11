"""Shared validation for ordered H3 adapter selections and saved manifests."""
import math

from scripts.h3_lora_controls import lora_strength_bounds

MAX_LORAS = 8


def lora_entries(config, key="id", *, components=None):
    """An explicit stack wins over legacy fields, including an empty stack."""
    rows = config.get("loras") if "loras" in config else (
        [{key: config["lora"], "strength": config.get("lora_scale", 1)}] if config.get("lora") else [])
    if not isinstance(rows, list) or len(rows) > MAX_LORAS:
        raise ValueError(f"Choose at most {MAX_LORAS} LoRAs")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {key, "strength"}:
            raise ValueError("Each LoRA needs a component selection and strength")
        identity, strength = row[key], row["strength"]
        if not isinstance(identity, str) or not identity or len(identity) > 4096:
            raise ValueError("Choose a cached LoRA for each adapter")
        name = (components or {}).get(identity, {}).get("name", identity)
        low, high = lora_strength_bounds(name)
        if (isinstance(strength, bool) or not isinstance(strength, (int, float))
                or not math.isfinite(strength) or not low <= strength <= high):
            raise ValueError(f"Each LoRA strength must be a finite number from {low} to {high}")
        if identity in seen:
            raise ValueError("The same LoRA cannot be selected more than once")
        seen.add(identity)
        result.append({key: identity, "strength": strength})
    return result
