"""Resolve named Seed Hunter setups against the server's actual inventory."""
from scripts.h3_pinkcherry_controls import LORA as PINKCHERRY_TURBO
from copy import deepcopy
from scripts.h3_seed_hunter_controls import LATENT_UPSCALER
from scripts.h3md_controls import UPSCALER as H3MD_UPSCALER

from scripts.h3_seed_hunter_presets import (LIGHTVAE, PIKA, TURBO4, TURBO8,
                                          CHARACTER_SWAP, CHARACTER_TURBO, PRISM8, PINKCHERRY, refinement_settings)


def preferred_upscaler(components):
    """Keep the authored Hunter upscaler independent of other recipe installs."""
    available = [c for c in components if c.get("role") == "latent_upscaler"]
    return next((c for c in available if c.get("name", "").lower() == LATENT_UPSCALER.lower()),
                next((c for c in available if c.get("name", "").lower() != H3MD_UPSCALER.lower()), None))


def catalog(components):
    def named(name, role):
        return next((c for c in components if c.get("role") == role and c.get("name", "").lower() == name.lower()), None)

    quality = next((c for c in components if c.get("role") == "model" and c.get("variant") == "ref2va"
                    and "singularity" in c.get("name", "").lower() and "v2" in c.get("name", "").lower()
                    and "pruned" in c.get("name", "").lower()), None)
    base = next((c for c in components if c.get("role") == "model" and c.get("variant") == "ref2va"
                 and "pruned_int8" in c.get("name", "").lower() and "singularity" not in c.get("name", "").lower()), None)
    vae = named("minimax_h3_video_vae_fp16.safetensors", "video_vae")
    upscaler = preferred_upscaler(components)
    definitions = [
        ("quality", "Singularity quality · 20 / 2 steps", "Short seed previews followed by a longer, higher-resolution final clip.", quality, None, vae),
        ("turbo4", "LightX2V fast · 4 / 2 steps", "Four-step Ref2VA previews with the v1.0 Turbo adapter.", base, named(TURBO4, "lora"), vae),
        ("turbo8", "LightX2V balanced · 8 / 2 steps", "Eight-step Ref2VA previews with the v1.1 Turbo adapter.", base, named(TURBO8, "lora"), vae),
        ("pika", "Pika Flex · 6 / 2 steps", "Pika's six-step reference-guided generation, then H3 refinement.", base, named(PIKA, "lora"), vae),
        ("lightvae", "Singularity + LightVAE · 20 / 2 steps", "The quality setup with a smaller video encoder and decoder.", quality, None, named(LIGHTVAE, "video_vae")),
        ("prism8", "Singularity × 10Eros · 8 steps", "Beta 7 INT8 comparison from the PRISM publisher. Turbo is already merged into the checkpoint.", named(PRISM8, "model"), None, vae),
        ("character_swap", "Character Swap V1.5 · 8 steps", "Use source performance as Video 1 and the replacement character as Picture 1. Includes the publisher's Turbo pairing; no automatic masks.", base, named(CHARACTER_SWAP, "lora"), vae),
        ("pinkcherry12", "PinkCherry v1.8 · 12 / 2 steps", "The supplied workflow's active 12-step Euler/simple branch and FL2V Turbo v1.0 pairing, adapted to short seed previews.", named(PINKCHERRY, "model"), named(PINKCHERRY_TURBO, "lora"), vae),
        ("pinkcherry4", "PinkCherry v1.8 · 4 / 2 steps", "The supplied workflow's alternate four-step branch, with the same model and Turbo adapter.", named(PINKCHERRY, "model"), named(PINKCHERRY_TURBO, "lora"), vae),
    ]
    rows = []
    for key, label, description, model, adapter, video_vae in definitions:
        required = {"Ref2VA model": model, "Video VAE": video_vae, "Latent upscaler": upscaler}
        if key in ("turbo4", "turbo8", "pika", "character_swap", "pinkcherry12", "pinkcherry4"):
            required["Preset adapter"] = adapter
        if key == "character_swap":
            required["Character Swap Turbo adapter"] = named(CHARACTER_TURBO, "lora")
        config = {"mode": "ref2va", "steps": 20, "sampler": "er_sde", "scheduler": "beta",
                  "shift_video": 12, "shift_audio": 6, "loras": [],
                  "seed_hunter": {"preset": key, "sampling_schedule": "standard", "refinement_method": "latent",
                                  "shift_enabled": False, "denoise": refinement_settings(key)["denoise"]}}
        for field, value in (("model", model), ("video_vae", video_vae), ("latent_upscaler", upscaler)):
            if value:
                config[field] = value["id"]
        if adapter:
            config["loras"] = [{"id": adapter["id"], "strength": 1.}]
        if key in ("turbo4", "turbo8"):
            config.update(steps=4 if key == "turbo4" else 8, sampler="euler", scheduler="simple", shift_video=6, shift_audio=3, reference_size="match")
            config["seed_hunter"]["shift_enabled"] = True
        elif key == "pika":
            config.update(steps=6, sampler="lcm", scheduler="simple")
            config["seed_hunter"]["sampling_schedule"] = "pika"
        elif key == "director":
            config.update(steps=16, sampler="euler", shift_audio=3)
            config["seed_hunter"].update(preview_count=4, denoise=.2, refinement_method="pixel", shift_enabled=True)
        elif key == "prism8":
            config.update(steps=8, sampler="euler", scheduler="simple", shift_audio=3)
            config["seed_hunter"]["shift_enabled"] = True
        elif key == "character_swap":
            config.update(steps=8, sampler="er_sde", scheduler="normal", shift_audio=3)
            config["seed_hunter"]["shift_enabled"] = True
            turbo = required["Character Swap Turbo adapter"]
            if turbo:
                config["loras"].append({"id": turbo["id"], "strength": 1.})
        elif key in ("pinkcherry12", "pinkcherry4"):
            config.update(steps=12 if key == "pinkcherry12" else 4, sampler="euler", scheduler="simple",
                          shift_video=1, shift_audio=1, reference_size="match")
        missing = [label for label, component in required.items() if component is None]
        rows.append({"id": key, "name": label, "description": description, "ready": not missing,
                     "missing": missing, "config": config, "refinement": refinement_settings(key)})
    return rows


def final_defaults(config, components):
    key = config.get("seed_hunter", {}).get("preset", "custom")
    if key == "custom":
        return {}
    result = deepcopy(refinement_settings(key))
    result["seed_hunter"]["preset"] = key
    if key in ("turbo4", "turbo8", "pika", "pinkcherry12", "pinkcherry4"):
        accelerators = {TURBO4, TURBO8, PIKA, CHARACTER_TURBO, PINKCHERRY_TURBO}
        from src.video_job_edit import _draft
        draft = _draft(config, {"components": components}, "h3")
        by_id = {c["id"]: c for c in components}
        result["loras"] = [row for row in draft.get("loras", [])
                           if by_id.get(row["id"], {}).get("name") not in accelerators]
    return result
