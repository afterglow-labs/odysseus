#!/usr/bin/env python3
"""One image recipe per private process, using the installed inference library.

No Comfy server, custom-node scan, or graph executor is started. Qwen reference
conditioning follows Phr00t's fixed TextEncodeQwenImageEditPlus v2, Apache-2.0,
at revision 691024f438640508f8aa86414863fc15edfb8a84. Source and model provenance
are documented in docs/image-workflows.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
from pathlib import Path
import signal
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.h3_video import _read, _write, _locked, _redact
from src.image_workflows import validate_config, validate_inputs


def runtime(path):
    sys.path.insert(0, str(path))
    import comfy.options
    comfy.options.enable_args_parsing(False)
    from comfy.cli_args import args
    import comfy_aimdo.control
    comfy_aimdo.control.init(simple_vram_headroom=None, nvml_pressure=not args.disable_nvml_pressure)
    import comfy.model_management
    import comfy.model_patcher
    import comfy.memory_management
    if not comfy.model_management.is_nvidia():
        raise RuntimeError("Image workflows require the selected NVIDIA GPU")
    if not comfy_aimdo.control.init_devices((d.index, int(args.vram_headroom * 1024**3)) for d in comfy.model_management.get_all_torch_devices()):
        raise RuntimeError("Could not initialize model offloading on the selected GPU")
    comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
    comfy.memory_management.aimdo_enabled = True
    import comfy.sd
    import comfy.utils
    import nodes
    return nodes


def qwen_condition(clip, vae, prompt, images, width, height):
    """Use the publisher's four-image, target-size-aware v2 conditioning."""
    import comfy.utils
    import node_helpers
    image_prompt, vision, references = "", [], []
    for index, image in enumerate(images):
        samples = image.movedim(-1, 1)
        scale = math.sqrt((384 * 384) / (samples.shape[3] * samples.shape[2]))
        vw, vh = round(samples.shape[3] * scale), round(samples.shape[2] * scale)
        vision.append(comfy.utils.common_upscale(samples, vw, vh, "lanczos", "center").movedim(1, -1))
        sized = comfy.utils.common_upscale(samples, width, height, "lanczos", "center")
        references.append(vae.encode(sized.movedim(1, -1)[:, :, :, :3]))
        image_prompt += f"Picture {index + 1}: <|vision_start|><|image_pad|><|vision_end|>"
    template = ("<|im_start|>system\nDescribe key details of the input image (including any objects, characters, poses, facial features, clothing, setting, textures and style), then explain how the user's text instruction should alter, modify or recreate the image. Generate a new image that meets the user's requirements, which can vary from a small change to a completely new image using inputs as a guide.<|im_end|>\n<|im_start|>user\n{}<|im_end|>\n<|im_start|>assistant\n")
    tokens = clip.tokenize(image_prompt + prompt, images=vision, llama_template=template)
    conditioning = clip.encode_from_tokens_scheduled(tokens)
    if references:
        conditioning = node_helpers.conditioning_set_values(conditioning, {"reference_latents": references}, append=True)
    return conditioning


def render(manifest, directory, progress):
    config, parts = manifest["config"], manifest["components"]
    os.environ["CUDA_VISIBLE_DEVICES"] = config["gpu"]
    nodes = runtime(manifest["runtime"])
    import numpy as np
    import torch
    from PIL import Image, ImageOps, PngImagePlugin
    import comfy.sd
    import comfy.utils

    progress("Loading model")
    if config["recipe"] == "snofs_krea":
        model = comfy.sd.load_diffusion_model(parts["model"])
        clip = comfy.sd.load_clip(ckpt_paths=[parts["encoder"]], clip_type=comfy.sd.CLIPType.KREA2)
        vae = comfy.sd.VAE(sd=comfy.utils.load_torch_file(parts["vae"], safe_load=True))
    else:
        model, clip, vae, _ = comfy.sd.load_checkpoint_guess_config(parts["checkpoint"], output_vae=True, output_clip=True)
    if model is None or clip is None or vae is None:
        raise RuntimeError("The selected checkpoint did not provide all required components")
    width, height = config["width"], config["height"]
    originals = []
    for path in manifest["images"]:
        with Image.open(path) as image:
            originals.append(ImageOps.exif_transpose(image).convert("RGB"))
    images = [torch.from_numpy(np.asarray(image, dtype=np.float32) / 255.).unsqueeze(0) for image in originals]
    progress("Encoding prompt")
    base = mask_image = None
    with torch.inference_mode():
        if config["recipe"] == "qwen_rapid":
            positive = qwen_condition(clip, vae, config["prompt"], images, width, height)
            negative = qwen_condition(clip, vae, config["negative_prompt"], images, width, height)
            latent = nodes.EmptyLatentImage().generate(width, height, 1)[0]
        else:
            positive = nodes.CLIPTextEncode().encode(clip, config["prompt"])[0]
            negative = nodes.CLIPTextEncode().encode(clip, config["negative_prompt"])[0]
            if config["recipe"] == "lustify_inpaint":
                if config["mode"] == "inpaint":
                    base = originals[0].resize((width, height), Image.Resampling.LANCZOS)
                    with Image.open(manifest["mask"]) as mask:
                        mask_image = mask.convert("L").resize((width, height), Image.Resampling.NEAREST)
                    if not mask_image.getbbox():
                        raise ValueError("Paint an area or supply a nonempty white mask")
                else:
                    base = Image.new("RGB", (width, height), (128, 128, 128))
                    mask_image = Image.new("L", (width, height), 255)
                pixels = torch.from_numpy(np.asarray(base, dtype=np.float32) / 255.).unsqueeze(0)
                mask_tensor = torch.from_numpy(np.asarray(mask_image, dtype=np.float32) / 255.).unsqueeze(0)
                positive, negative, latent = nodes.InpaintModelConditioning().encode(positive=positive, negative=negative, vae=vae, pixels=pixels, mask=mask_tensor, noise_mask=True)
            else:
                negative = nodes.ConditioningZeroOut().zero_out(positive)[0]
                latent = nodes.EmptyLatentImage().generate(width, height, 1)[0]

        progress("Generating", 0, config["steps"])
        def tick(value, total, preview=None, **kwargs):
            if (directory / "cancel").exists():
                raise InterruptedError("Image generation stopped")
            progress("Generating", value, total)
        comfy.utils.set_progress_bar_global_hook(tick)
        latent = nodes.common_ksampler(model, config["seed"], config["steps"], config["cfg"], config["sampler"], config["scheduler"], positive, negative, latent, denoise=config["denoise"])[0]
        progress("Decoding image")
        pixels = nodes.VAEDecode().decode(vae, latent)[0]
        output = Image.fromarray((pixels[0].clamp(0, 1).cpu().numpy() * 255).round().astype(np.uint8))
        if config["mode"] == "inpaint":
            output = Image.composite(output, base, mask_image)
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Odysseus workflow", json.dumps(config))
        output.save(directory / "output.png", pnginfo=metadata)
        (directory / "output.png").chmod(0o600)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    directory = Path(args.job).resolve().parent
    manifest = _read(args.job)
    state = _read(directory / "state.json")
    started = time.time()
    def progress(phase, step=0, total=0):
        _write(directory / "progress.json", {"phase": phase, "step": step, "total_steps": total, "elapsed_seconds": round(time.time() - started, 1)})
    def stopped(*_):
        raise InterruptedError("Image generation stopped")
    signal.signal(signal.SIGTERM, stopped)
    try:
        manifest["config"] = validate_config(manifest["config"])
        validate_inputs(manifest["config"], manifest["images"], manifest["mask"])
        lease = Path(manifest["execution_lock"])
        lease.parent.mkdir(parents=True, exist_ok=True)
        progress("Waiting for GPU")
        with _locked(lease):
            if (directory / "cancel").exists():
                raise InterruptedError("Image generation stopped")
            state.update(status="running", phase="Loading model", started_at=time.time())
            _write(directory / "state.json", state)
            render(manifest, directory, progress)
            if (directory / "cancel").exists():
                raise InterruptedError("Image generation stopped")
            state.update(status="completed", phase="Completed", finished_at=time.time())
            _write(directory / "state.json", state)
    except (InterruptedError, KeyboardInterrupt):
        state.update(status="stopped", phase="Stopped", finished_at=time.time())
        _write(directory / "state.json", state)
    except Exception as exc:
        traceback.print_exc()
        state.update(status="failed", phase="Failed", error=_redact(exc), finished_at=time.time())
        _write(directory / "state.json", state)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
