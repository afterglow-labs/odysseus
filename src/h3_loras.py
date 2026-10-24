"""Shared validation for ordered H3 adapter selections and saved manifests."""
import hashlib
import math
from pathlib import Path

from scripts.h3_lora_controls import lora_strength_bounds, lora_strength_control, read_profile, validate_lora_profile

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
        component = (components or {}).get(identity, {})
        name = component.get("name", identity)
        profile = component.get("lora_profile")
        if not component and Path(identity).is_absolute():
            profiles = config.get("lora_profiles", {}) if key == "path" else {}
            if not isinstance(profiles, dict) or len(profiles) > MAX_LORAS:
                raise ValueError("Invalid saved LoRA profiles")
            profile = profiles.get(identity) or read_profile(identity)
        low, high = lora_strength_bounds(name, profile)
        if (isinstance(strength, bool) or not isinstance(strength, (int, float))
                or not low <= strength <= high or not math.isfinite(strength)):
            raise ValueError(f"Each LoRA strength must be a finite number from {low} to {high}")
        if identity in seen:
            raise ValueError("The same LoRA cannot be selected more than once")
        seen.add(identity)
        result.append({key: identity, "strength": strength})
    return result


def _resolved_path(value, cache):
    key = str(value)
    if key not in cache:
        cache[key] = Path(value).resolve()
    return str(cache[key])


def frozen_inventory(config, inventory, *, resolved_paths=None):
    """Use server-saved profiles when editing a job, never caller metadata."""
    profiles = config.get("lora_profiles", {})
    if not isinstance(profiles, dict) or len(profiles) > MAX_LORAS:
        raise ValueError("Invalid saved LoRA profiles")
    resolved_paths = {} if resolved_paths is None else resolved_paths
    profiles = {_resolved_path(path, resolved_paths): validate_lora_profile(profile) for path, profile in profiles.items()}
    components = []
    for component in inventory["components"]:
        profile = (profiles.get(_resolved_path(component["path"], resolved_paths))
                   if profiles and component.get("role") == "lora" else None)
        if profile is not None:
            component = {**component, "lora_profile": profile, "variant": profile["variant"],
                         "display_name": profile["display_name"] or component["name"]}
            component.pop("strength_control", None)
            control = profile.get("strength_control") or lora_strength_control(component["name"])
            if control:
                component["strength_control"] = control
        components.append(component)
    if config.get("seed_hunter", {}).get("stage") == "scout" and config.get("model"):
        # Exact partial-schedule resumes need their original weights, even when
        # a newer version has replaced that model in the new-job picker.
        path = Path(_resolved_path(config["model"], resolved_paths))
        identity = hashlib.sha256(str(path).encode()).hexdigest()[:32]
        replacement = next((row for row in components if row.get("role") == "model"
                            and identity in row.get("aliases", []) and row["id"] != identity), None)
        if replacement and path.is_file():
            components.insert(0, {**replacement, "id": identity, "aliases": [], "path": str(path),
                                  "name": path.name, "display_name": path.name,
                                  "nvfp4": "nvfp4" in path.name.lower()})
    return {**inventory, "components": components}


def frozen_components(config, inventory, *, resolved_paths=None):
    resolved_paths = {} if resolved_paths is None else resolved_paths
    selected = {_resolved_path(row["path"], resolved_paths) for row in lora_entries(config, "path")}
    if not selected:
        return []
    return [{key: value for key, value in component.items() if key != "path"}
            for component in frozen_inventory(config, inventory, resolved_paths=resolved_paths)["components"]
            if component.get("role") == "lora" and _resolved_path(component["path"], resolved_paths) in selected]
