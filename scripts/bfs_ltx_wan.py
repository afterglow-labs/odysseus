"""Curated BFS LTX and Bernini recipes using the private Comfy inference core.

This executes model operations directly: no workflow JSON execution, ComfyUI
server, arbitrary custom-node imports, automatic downloads, or global installs.
The LTX-2.5 overlap helper and LTX-2.3 panel composer are the author's pinned
BFSNodes 1.21.2 implementation; see runtimes/minimax-h3/bfs_nodes/UPSTREAM.json.
Optional workflow captioners, first-frame editors and upscaling passes are not
part of these generation recipes. The output is generated at the chosen size.
"""
from __future__ import annotations

import importlib
import math
from pathlib import Path
import sys

from scripts.h3_video_worker import H3Runtime

WORKFLOWS = {
    "ltx2_v1": "ltx2", "ltx2_v2": "ltx2", "ltx23_v3": "ltx23",
    "ltx25_v1": "ltx25", "ltx25_v11": "ltx25", "wan22_head_swap": "wan22",
}
DISTILLED_SIGMAS = (1.0, .99375, .9875, .98125, .975, .909375, .725, .421875, 0.0)


def normalize_config(config):
    config = dict(config)
    family = WORKFLOWS.get(config.get("workflow_id"))
    if family is None or config.get("family", family) != family:
        raise ValueError("Choose a supported BFS LTX or Wan workflow")
    config["family"] = family
    for key in ("model", "encoder", "video_vae", "lora"):
        if not config.get(key):
            raise ValueError("Missing BFS component: " + key)
    for key in (("model_low", "lora_low") if family == "wan22" else ("audio_vae",)):
        if not config.get(key):
            raise ValueError("Missing BFS component: " + key)
    if family in ("ltx2", "ltx23") and not config.get("connector"):
        raise ValueError("This LTX encoder needs its matching text projection")
    for axis in ("width", "height"):
        value = config.get(axis)
        if isinstance(value, bool) or not isinstance(value, int) or not 32 <= value <= 1920 or value % 32:
            raise ValueError("Output width and height must be multiples of 32")
    if config["width"] * config["height"] > 768 * 1344:
        raise ValueError("Output dimensions exceed 1,032,192 pixels")
    for key, default, low, high in (("steps", 40 if family == "wan22" else 20 if family == "ltx2" else 8, 1, 100),
                                   ("seed", 42, 0, 2**32 - 1), ("frames", 73 if family == "wan22" else 121, 1, 241)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError("Invalid BFS " + key)
    for key, default, low, high in (("cfg", 5 if family == "wan22" else 4 if family == "ltx2" else 1, 1, 20),
                                   ("lora_scale", .8 if config["workflow_id"] == "ltx25_v11" else 1, .01, 4)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("Invalid BFS " + key)
    config.setdefault("fps", 24)
    config.setdefault("audio_mode", "source")
    if config["fps"] != 24 or config["audio_mode"] not in ("source", "silent"):
        raise ValueError("BFS recipes use 24 fps with source or silent audio")
    if family == "ltx25" and "distilled" not in Path(config["model"]).name.lower() and not config.get("distill_lora"):
        raise ValueError("LTX-2.5 BFS needs a distilled base or its matching distillation LoRA")
    if family == "wan22" and config["steps"] < 2:
        raise ValueError("Wan needs at least two steps for its high- and low-noise models")
    prompt = config.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 16000:
        raise ValueError("BFS prompt must be at most 16,000 characters")
    config["prompt"] = build_prompt(config["workflow_id"], prompt)
    if len(config["prompt"]) > 16000:
        raise ValueError("BFS prompt, including recipe instructions, exceeds 16,000 characters")
    return config


def build_prompt(workflow_id, text):
    """Keep the author's trigger and distinguish identity from scene/action."""
    text = text.strip()
    trigger = "head swap" if workflow_id.startswith("ltx2_v") else "head_swap:"
    if text.lower().startswith(trigger):
        return text
    return (trigger + "\nFACE: Use the reference head identity consistently.\n"
            "ACTION: Follow the source person's performance, pose, body, clothing, "
            "camera, setting and lighting; replace their head with the reference identity.\n" + text).strip()


def as_frames(value, label):
    import torch
    if value is None:
        raise ValueError("Supply " + label)
    frames = torch.as_tensor(value, dtype=torch.float32, device="cpu")
    if frames.ndim == 3:
        frames = frames.unsqueeze(0)
    if frames.ndim != 4 or frames.shape[0] < 1 or frames.shape[-1] not in (3, 4):
        raise ValueError(label + " must contain RGB image frames")
    if not torch.isfinite(frames).all() or frames.min() < 0 or frames.max() > 1:
        raise ValueError(label + " contains invalid pixels")
    return frames[..., :3]


def magenta_guide(source, mask):
    """BFS V2 trains on a magenta hole covering the *whole* original head."""
    import torch
    mask = as_frames(mask, "a head mask video")
    if mask.shape[:3] != source.shape[:3]:
        raise ValueError("The mask must match the selected source frames and dimensions")
    foreground = mask.mean(dim=-1, keepdim=True) >= .5
    if not foreground.any():
        raise ValueError("The head mask is empty; white must cover the original head")
    return torch.where(foreground, source.new_tensor([1., 0., 1.]), source)


def _helpers(runtime_path, module):
    root = Path(runtime_path).resolve().parent
    package = root / "bfs_nodes"
    if not (package / "UPSTREAM.json").is_file():
        raise RuntimeError("BFS helper runtime is missing; run the BFS runtime setup")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module("bfs_nodes." + module)


def compose_panel(source, identity, runtime_path):
    # Exact author's all-frames green-panel composition, with its white face
    # padding and aspect-preserving fit. Scale the 256/1024 panel for our canvas.
    composer = _helpers(runtime_path, "nodes").ReservedRegionFrameComposer()
    width = source.shape[2]
    return composer.process(
        frames=source, face_images=identity[:1], region_position="left",
        region_size_px=max(32, round(width / 4)), face_distribution="all_faces_every_frame",
        interval_frames=12, overflow_mode="loop", stack_direction="auto", face_scale_pct=100,
        face_padding_px=12, face_gap_px=12, face_align_main="center", face_align_cross="center",
        chroma_r=0, chroma_g=255, chroma_b=0)[0]


class CoreBackend:
    """Small direct-core boundary, injectable for CPU recipe contract tests."""
    def __init__(self, runtime, config):
        import torch
        import comfy.samplers
        import comfy.model_management
        import node_helpers
        from comfy_extras import nodes_lt, nodes_lt_audio
        self.runtime, self.config, self.torch = runtime, config, torch
        self.lt, self.audio = nodes_lt, nodes_lt_audio
        self.samplers, self.management, self.helpers = comfy.samplers, comfy.model_management, node_helpers

    def apply_lora(self, model, path, scale):
        before = sum(len(v) for v in model.patches.values())
        state = self.runtime.utils.load_torch_file(path, safe_load=True)
        updated, _ = self.runtime.sd.load_lora_for_models(model, None, state, scale, 0)
        if sum(len(v) for v in updated.patches.values()) <= before:
            raise ValueError("The selected adapter has no weights compatible with this base model")
        return updated

    def load(self, progress):
        c, runtime = self.config, self.runtime
        def load_model(key, adapter):
            progress("loading_" + key)
            model = runtime.sd.load_diffusion_model(c[key])
            kind = type(model.model).__name__.lower()
            if (c["family"] == "wan22" and "wan" not in kind) or (c["family"] != "wan22" and "ltx" not in kind):
                raise ValueError("The selected transformer does not match this workflow family")
            if c.get("distill_lora"):
                model = self.apply_lora(model, c["distill_lora"], 1.)
            progress("loading_" + adapter)
            return self.apply_lora(model, c[adapter], c["lora_scale"])
        model = load_model("model", "lora")
        low = load_model("model_low", "lora_low") if c["family"] == "wan22" else None
        progress("loading_encoder")
        clip_type = runtime.sd.CLIPType.WAN if c["family"] == "wan22" else runtime.sd.CLIPType.LTXV
        paths = [c["encoder"]] + ([c["connector"]] if c.get("connector") else [])
        clip = runtime.sd.load_clip(paths, clip_type=clip_type)
        def load_vae(key):
            progress("loading_" + key)
            state, metadata = runtime.utils.load_torch_file(c[key], safe_load=True, return_metadata=True)
            vae = runtime.sd.VAE(sd=state, metadata=metadata)
            vae.throw_exception_if_invalid()
            return vae
        self.vae = load_vae("video_vae")
        self.audio_vae = load_vae("audio_vae") if c["family"] != "wan22" else None
        progress("encoding_prompt")
        positive = runtime.nodes.CLIPTextEncode().encode(clip, c["prompt"])[0]
        negative = runtime.nodes.CLIPTextEncode().encode(clip, "")[0]
        if c["family"] != "wan22":
            positive, negative = self.lt.LTXVConditioning.execute(positive, negative, c["fps"])
        return model, low, positive, negative

    def empty_video(self):
        c = self.config
        return self.lt.EmptyLTXVLatentVideo.execute(c["width"], c["height"], c["frames"], 1)[0]

    def keyframe(self, latent, image, last=False):
        # Inplace I2V pins the supplied prepared keyframe. Last-frame mode uses
        # the same VAE encoding and mask at the last latent, not another guide.
        if not last:
            return self.lt.LTXVImgToVideoInplace.execute(self.vae, image[:1], latent, 1.)[0]
        height, width = self.config["height"], self.config["width"]
        image = image[:1]
        if image.shape[1:3] != (height, width):
            image = self.runtime.utils.common_upscale(image.movedim(-1, 1), width, height, "bilinear", "center").movedim(1, -1)
        encoded = self.vae.encode(image)
        result = dict(latent)
        result["samples"] = latent["samples"].clone()
        result["samples"][:, :, -1:] = encoded[:, :, :1]
        mask = latent.get("noise_mask", self.torch.ones_like(latent["samples"][:, :1, :, :1, :1])).clone()
        mask[:, :, -1:] = 0
        result["noise_mask"] = mask
        return result

    def add_guide(self, positive, negative, latent, image):
        return tuple(self.lt.LTXVAddGuide.execute(positive, negative, self.vae, latent, image, 0, 1.))

    def overlap(self, model, positive, negative, latent, source, identity):
        helper = _helpers(self.config["runtime_path"], "ltx_multiple_controls").LTXMultipleControls()
        # The helper defaults now target a different model. Every relevant BFS
        # head-swap coordinate/resize setting is therefore supplied explicitly.
        result = helper.apply(
            model=model, positive=positive, negative=negative, vae=self.vae, latent=latent,
            guide_video=source, guide_source_id=1., guide_phase_scale=1., guide_layout="overlap",
            guide_ref_resize_mode="native_resolution", guide_downscale_factor=1,
            identity_image=identity[:1], identity_source_id=2., identity_phase_scale=1.,
            identity_layout="overlap", identity_ref_resize_mode="native_resolution", identity_downscale_factor=1,
            mask_video=None, identity_mask_image=None, auto_mask_guide=False,
            crop_anchor="center", reference_guidance_scale=1.3 if self.config["workflow_id"] == "ltx25_v11" else 1.,
            debug_log=False)
        return result[:4]

    def bernini(self, positive, negative, source, identity):
        # Core already consumes Bernini context_latents in source order, so no
        # third-party global Wan patch is needed. source1=guide, source2=head.
        def encode(image):
            height, width = image.shape[1:3]
            width, height = max(16, round(width / 16) * 16), max(16, round(height / 16) * 16)
            if image.shape[1:3] != (height, width):
                image = self.runtime.utils.common_upscale(image.movedim(-1, 1), width, height, "area", "disabled").movedim(1, -1)
            return self.vae.encode(image)
        guide, head = encode(source), encode(identity[:1])
        positive = self.helpers.conditioning_set_values(positive, {"context_latents": [guide, head]})
        negative = self.helpers.conditioning_set_values(negative, {"context_latents": [guide]})
        samples = self.torch.zeros((1, 16, (len(source) - 1) // 4 + 1, source.shape[1] // 8, source.shape[2] // 8),
                                   device=self.management.intermediate_device())
        return positive, negative, {"samples": samples}

    def av_latent(self, video, source_audio):
        c = self.config
        if c["family"] != "ltx25" and source_audio is not None and c["audio_mode"] == "source":
            audio = self.audio.LTXVAudioVAEEncode.execute(source_audio, self.audio_vae)[0]
            audio["noise_mask"] = self.torch.zeros_like(audio["samples"])
        else:
            audio = self.audio.LTXVEmptyLatentAudio.execute(c["frames"], c["fps"], 1, self.audio_vae)[0]
        return self.lt.LTXVConcatAVLatent.execute(video, audio)[0]

    def sample(self, model, positive, negative, latent, progress, *, low=None):
        c, torch = self.config, self.torch
        distilled = c["family"] in ("ltx23", "ltx25") and ("distilled" in Path(c["model"]).name.lower() or c.get("distill_lora"))
        if distilled and c["steps"] == 8:
            sigmas = torch.tensor(DISTILLED_SIGMAS, dtype=torch.float32)
        else:
            sigmas = self.samplers.calculate_sigmas(model.get_model_object("model_sampling"), "simple", c["steps"])
        sampler = self.samplers.sampler_object("er_sde" if low is not None else "euler_ancestral")
        noise = self.runtime.sample.prepare_noise(latent["samples"], c["seed"])
        def one(active, data, curve, add_noise, offset):
            def callback(step, x0, x, total):
                progress("sampling", offset + step + 1)
            return self.runtime.sample.sample_custom(
                active, noise if add_noise else self.runtime.sample.prepare_empty_noise(data), c["cfg"], sampler,
                curve, positive, negative, data, noise_mask=latent.get("noise_mask"),
                callback=callback, disable_pbar=False, seed=c["seed"])
        if low is None:
            result = one(model, latent["samples"], sigmas, True, 0)
        else:
            split = max(1, min(c["steps"] - 1, c["steps"] // 2))
            result = one(model, latent["samples"], sigmas[:split + 1], True, 0)
            result = one(low, result, sigmas[split:], False, split)
        return {**latent, "samples": result}

    def decode(self, samples, positive, negative):
        if self.config["family"] != "wan22":
            samples = self.lt.LTXVSeparateAVLatent.execute(samples)[0]
            _, _, samples = self.lt.LTXVCropGuides.execute(positive, negative, samples)
        return self.runtime.nodes.VAEDecode().decode(self.vae, samples)[0]


def generate(config, media, progress, *, runtime=None, backend_factory=CoreBackend):
    config = normalize_config(config)
    source = as_frames(media.get("source_video"), "a source video")
    identity = as_frames(media.get("identity_image"), "a head reference image")
    frame_step = 4 if config["family"] == "wan22" else 8
    count = min(config["frames"], len(source))
    count -= (count - 1) % frame_step
    config["frames"] = count
    source = source[:count]
    if source.shape[1:3] != (config["height"], config["width"]):
        raise ValueError("Source frames must be fitted to the output canvas before inference")
    workflow = config["workflow_id"]
    last = media.get("last_frame")
    if workflow == "ltx2_v1":
        two_keyframes = "8750" in Path(config["lora"]).name
        if two_keyframes != (last is not None):
            raise ValueError("Choose the first-and-last-frame adapter when supplying a last frame; it requires both keyframes")
        if last is not None:
            last = as_frames(last, "a last frame")
    elif last is not None:
        raise ValueError("A last frame is supported only by the LTX-2 first-and-last-frame recipe")
    if workflow == "ltx2_v2":
        mask = as_frames(media.get("mask_video"), "a head mask video")
        if len(mask) < count:
            raise ValueError("The mask video is shorter than the selected source segment")
        source_guide = magenta_guide(source, mask[:count])
    else:
        if media.get("mask_video") is not None:
            raise ValueError("The mask video belongs to the LTX-2 masked recipe")
        source_guide = source
    progress("initializing_runtime", total_steps=config["steps"], frames=count, fps=config["fps"])
    if runtime is None:
        runtime = H3Runtime(config["runtime_path"])
    backend = backend_factory(runtime, config)
    import torch
    with torch.inference_mode():
        model, low, positive, negative = backend.load(progress)
        progress("encoding_source_and_identity")
        if config["family"] == "wan22":
            positive, negative, latent = backend.bernini(positive, negative, source, identity)
        else:
            latent = backend.empty_video()
            if config["family"] == "ltx25":
                model, positive, negative, latent = backend.overlap(model, positive, negative, latent, source, identity)
            else:
                if workflow == "ltx23_v3":
                    source_guide = compose_panel(source, identity, config["runtime_path"])
                else:
                    latent = backend.keyframe(latent, identity)
                    if last is not None:
                        latent = backend.keyframe(latent, last, last=True)
                positive, negative, latent = backend.add_guide(positive, negative, latent, source_guide)
            latent = backend.av_latent(latent, media.get("source_audio"))
        progress("sampling", 0)
        samples = backend.sample(model, positive, negative, latent, progress, low=low)
        progress("decoding_video")
        frames = backend.decode(samples, positive, negative)
    return frames[:count], media.get("source_audio") if config["audio_mode"] == "source" else None
