"""Small shared adapter controls for the API and standalone H3 worker."""
from pathlib import Path


AGE_SLIDER_LORA = "MM-H3 - Ref2VA - Age Slider v2 - EXPERIMENTAL.safetensors"


def lora_strength_control(name):
    if Path(str(name)).name.casefold() != AGE_SLIDER_LORA.casefold():
        return None
    return {"kind": "age", "label": "Age", "min": -5, "max": 5, "step": 0.1,
            "default": 0, "labels": {"min": "Youngest", "zero": "Neutral", "max": "Oldest"}}


def lora_strength_bounds(name):
    control = lora_strength_control(name)
    return (control["min"], control["max"]) if control else (-4, 4)
