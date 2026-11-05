"""H3MD discovery and job-owned resumable segment caches."""
from copy import deepcopy
from pathlib import Path
import shutil
import os
import re

from scripts import h3md_controls as controls


def capabilities(project, components):
    def preset(identity, label, description, rows):
        missing = []
        def select(name, role):
            candidates = [c for c in components if c["role"] == role and c["name"] == name]
            chosen = next((c for c in candidates if "/models/Comfy-Org/" in c.get("path", "")), next(iter(candidates), None))
            if chosen is None:
                missing.append(name)
            return chosen["id"] if chosen else name
        config = {**controls.GENERATION, **{role: select(name, role) for role, name in controls.COMPONENTS.items()},
                  "loras": [{"id": select(name, "lora"), "strength": low} for name, low, high in rows],
                  controls.RECIPE: {**deepcopy(controls.DEFAULTS), "preset": identity,
                                    "high_res_strengths": [high for name, low, high in rows]}}
        if identity == "website_lite":
            config.update(width=1472, height=1088, seed=23793874848978)
            config[controls.RECIPE]["lowres_anchor"] = 1.
        return {"id": identity, "name": label, "description": description,
                "config": config, "ready": not missing, "missing": missing}
    presets = [
        preset("core", "H3MD two-pass", "DMAD + LightX2V. Six steps at half size, latent upscale, two steps at final size.", controls.LORAS[:2]),
        preset("website_lite", "Author’s complete LoRA setup", "The lite example’s eight LoRAs with separate first-pass and final-pass strengths.", controls.LORAS),
    ]
    installed = Path(project) / "runtimes/minimax-h3/h3md/h3md_loop.py"
    return {"version": 1, "source": controls.SOURCE, "revision": controls.REVISION,
            "attribution": controls.ATTRIBUTION, "runtime_ready": installed.is_file(),
            "ready": installed.is_file() and presets[0]["ready"], "defaults": deepcopy(presets[0]["config"]),
            "presets": presets, "samplers": ["euler", "res_multistep"], "schedulers": ["simple", "normal"],
            "latent_upscaling": True, "segment_resume": True, "max_duration_seconds": 600}


def clone_cache(source, destination):
    """Keep reruns independent: no mutable cache files shared with the old job."""
    source, destination = Path(source), Path(destination)
    cache = source / "h3md-cache"
    if not cache.exists():
        raise ValueError("This H3MD job has no saved segments to resume")
    if cache.is_symlink():
        raise ValueError("Invalid H3MD segment cache")
    for path in cache.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Invalid entry in H3MD segment cache")
    def clone(source, target):
        # Frames/latents are immutable between writes. The worker detaches a
        # shared file before replacing a segment; metadata is always copied.
        if re.fullmatch(r"seg_\d+_(?:frames\.npy|audio\.pt|latent\.pt|lowres\.pt)", Path(source).name):
            try:
                os.link(source, target)
                return target
            except OSError:
                pass
        return shutil.copy2(source, target)
    shutil.copytree(cache, destination / "h3md-cache", copy_function=clone)


def guard_edit(original, updated):
    if original.get("recipe") != controls.RECIPE:
        return
    if updated.get("recipe") != controls.RECIPE:
        raise ValueError("Keep the saved H3MD job’s workflow")
    options = updated[controls.RECIPE]
    if options["resume_from_cache"]:
        # Upstream's cache key omits models. Do not reuse segments from changed weights.
        for key in ("model", "encoder", "video_vae", "audio_vae", "latent_upscaler", "loras"):
            if updated.get(key) != original.get(key):
                raise ValueError("Turn off Resume saved segments when changing H3MD models or LoRAs")
        if options["high_res_strengths"] != original[controls.RECIPE]["high_res_strengths"]:
            raise ValueError("Turn off Resume saved segments when changing H3MD final-pass LoRA strengths")
