"""Standalone PinkCherry defaults; no Seed Hunter preview/refinement stages."""
from copy import deepcopy
from scripts.h3_pinkcherry_controls import COMPONENTS, DEFAULTS, SOURCE_REVISION, SOURCE_URL


def capabilities(components):
    defaults, missing = dict(DEFAULTS), []
    for role, name in COMPONENTS.items():
        matches = [c for c in components if c["role"] == role and c["name"] == name]
        selected = next((c for c in matches if "/models/Comfy-Org/" in c.get("path", "")), next(iter(matches), None))
        if not selected:
            missing.append(name)
        identity = selected["id"] if selected else name
        if role == "lora":
            defaults["loras"] = [{"id": identity, "strength": 1}]
        else:
            defaults[role] = identity
    return {"version": 1, "ready": not missing, "missing": missing, "defaults": defaults,
            "source": "PinkCherry_ref2va_turbo_lightx2v_workflow.json",
            "source_url": SOURCE_URL, "source_revision": SOURCE_REVISION,
            "description": "PinkCherry hybrid + FL Turbo v1.0 at strength 1. Heretic Qwen. Direct 12-second render at 672 × 1216; no upscale stage.",
            "presets": [{"id": f"pinkcherry{steps}", "name": f"PinkCherry · {steps} steps",
                         "config": {**deepcopy(defaults), "steps": steps}, "ready": not missing, "missing": missing}
                        for steps in (12, 4)]}
