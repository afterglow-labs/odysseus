# Muse Collective, MIT; see licenses/Muse-Director-MIT-LICENSE.txt and licenses/Muse-Director-NOTICE.txt.
# Pinned upstream e46f0aed164f037bd76dc3b28a77df04df4440b3.
"""
Muse Minimax Refine — a standalone second-pass "hi-res fix" node for MiniMax H3.

Companion to MuseMinimaxDirector (github.com/muse-collective-26/MiniMaxH3-Director),
not a replacement or a modification of it — this package has no dependency on it and
works standalone with any four IMAGE/AUDIO pairs. It takes up to four already-generated
candidate clips (e.g. four cheap low-res scouting passes, or just four manual re-runs
of a director-style node), lets you pick one via the button selector, and re-samples
it at a new (typically higher) resolution using a genuine img2img-style partial-denoise
pass. H3 has no dedicated spatial-upscaler model of its own (unlike LTX), so this works
in pixel space instead, the same technique as an SD hi-res-fix:

  1. Pick one of the four candidate image/audio pairs (button selector in the UI).
  2. Resize its frames to the new target resolution (resize_method).
  3. Re-encode those pixels through the real vae/audio_vae to build a genuine,
     non-empty starting AV latent — this is the img2img step; H3's own reference
     nodes only ever build this from real pixels for reference *videos*
     (MiniMaxH3ReferenceToVideo._encode_ref_audio and its ref_videos loop), so this
     mirrors that exact encode path rather than guessing at one.
  4. Run H3's own sampler chain (RandomNoise -> BasicGuider -> KSamplerSelect ->
     BasicScheduler -> SamplerCustomAdvanced) — but with BasicScheduler's denoise
     below 1.0, so only the tail of the sigma schedule runs and the model refines
     the existing content instead of generating from scratch (denoise=1.0 would
     just regenerate, ignoring the candidate entirely).
  5. Decode video only. Audio is passed straight through from the selected
     candidate, untouched — it was already real, decoded audio; round-tripping it
     through a partial-noise pass would only risk drift for no benefit, since this
     node doesn't change the prompt or the performance, only the pixels.

Single-clip only, same ~15s ceiling as one real H3 call — candidates are expected
to be single H3-call-length clips (e.g. one MuseMinimaxDirector chunk), not
multi-chunk stitched output. A concatenated multi-chunk clip's total frame count
generally won't satisfy H3's own 17k+5 latent grid as one block, so it isn't a
valid input here.
"""

import logging
import math

import torch

from comfy_extras.nodes_minimax_h3 import MiniMaxH3SigmaShift, MiniMaxH3ReferenceToVideo
from comfy_extras.nodes_resolution import AspectRatio, ASPECT_RATIOS
from comfy_execution.graph import ExecutionBlocker

log = logging.getLogger(__name__)

ASPECT_RATIO_OPTIONS = [a.value for a in AspectRatio]


# ── Shared helpers ───────────────────────────────────────────────────────────
# Deliberately duplicated here rather than imported from MuseMinimaxDirector —
# this package has to work standalone for anyone who installs just this node via
# the Registry, without also needing MuseMinimaxDirector installed. Same lesson
# already learned once this project: MuseMinimaxDirector's Analyze route used to
# hard-depend on a sibling package for exactly this reason, and broke for anyone
# who installed it alone.

def _execute_comfy_node(node_class, **kwargs):
    """Invoke a ComfyUI node's main entrypoint, whether it is a comfy_api io.ComfyNode
    (classmethod 'execute') or a legacy node (instance method named by FUNCTION)."""
    if hasattr(node_class, "execute"):
        return node_class.execute(**kwargs)
    fn_name = getattr(node_class, "FUNCTION", None)
    instance = node_class()
    if fn_name and hasattr(instance, fn_name):
        return getattr(instance, fn_name)(**kwargs)
    raise RuntimeError(f"Could not determine how to execute node {node_class!r}")


def _unpack_node_result(out):
    """Normalise a node return (io.NodeOutput, tuple, list or dict) into a tuple of outputs."""
    if out is None:
        return ()
    for attr in ("result", "args", "values", "outputs"):
        if hasattr(out, attr):
            val = getattr(out, attr)
            if callable(val):
                try:
                    val = val()
                except Exception:
                    continue
            if isinstance(val, (tuple, list)):
                return tuple(val)
    if isinstance(out, (tuple, list)):
        return tuple(out)
    if isinstance(out, dict) and isinstance(out.get("result"), (tuple, list)):
        return tuple(out["result"])
    return (out,)


def _resolve_resolution(aspect_ratio: str, megapixels: float, multiple: int):
    """Exact port of the stock ResolutionSelector node's own formula."""
    w_ratio, h_ratio = ASPECT_RATIOS[AspectRatio(aspect_ratio)]
    total_pixels = megapixels * 1024 * 1024
    scale = math.sqrt(total_pixels / (w_ratio * h_ratio))
    width = round(w_ratio * scale / multiple) * multiple
    height = round(h_ratio * scale / multiple) * multiple
    return width, height


def _fit_image_to_target(tensor: torch.Tensor, target_w: int, target_h: int, method: str) -> torch.Tensor:
    """Resizes an [N,H,W,C] IMAGE tensor to exactly (target_h, target_w) ourselves,
    using one of three standard fit strategies, rather than leaving an aspect-ratio
    mismatch to whatever H3's own internal preprocessing happens to do (observed
    directly: it silently stretches/distorts rather than cropping).
      - crop: scale up to fully cover the target, then center-crop the excess — no
        distortion, no bars, but can crop off the edges of a person/scene on a big
        aspect-ratio swing
      - pad: scale down to fully fit within the target, then pad with black bars —
        never crops anything, but the bars themselves become visible reference content
      - stretch: resize straight to the target dimensions, ignoring aspect ratio"""
    if tensor is None:
        return None
    n, h, w, c = tensor.shape
    if h == target_h and w == target_w:
        return tensor
    chw = tensor.permute(0, 3, 1, 2)
    if method == "stretch":
        resized = torch.nn.functional.interpolate(chw, size=(target_h, target_w), mode="bilinear", align_corners=False)
        return resized.permute(0, 2, 3, 1).clamp(0, 1)
    scale = max(target_w / w, target_h / h) if method == "crop" else min(target_w / w, target_h / h)
    new_w = max(1, round(w * scale))
    new_h = max(1, round(h * scale))
    resized = torch.nn.functional.interpolate(chw, size=(new_h, new_w), mode="bilinear", align_corners=False)
    resized = resized.permute(0, 2, 3, 1).clamp(0, 1)
    if method == "crop":
        top = max(0, (new_h - target_h) // 2)
        left = max(0, (new_w - target_w) // 2)
        return resized[:, top:top + target_h, left:left + target_w, :]
    canvas = torch.zeros((n, target_h, target_w, c), dtype=resized.dtype)
    top = max(0, (target_h - new_h) // 2)
    left = max(0, (target_w - new_w) // 2)
    canvas[:, top:top + new_h, left:left + new_w, :] = resized
    return canvas


def _trim_to_grid(n: int) -> int:
    """H3's video latent only supports frame counts on the 17k+5 grid. A candidate
    that came out of a real single H3 call should already satisfy this, but trim
    defensively the same way MiniMaxH3ReferenceToVideo trims its own ref_videos
    input, rather than erroring on an off-by-a-few count."""
    while n > 5 and n % 17 != 5:
        n -= 1
    return max(5, n)


def _encode_av_latent(vae, audio_vae, frames, audio):
    """Encode real pixels + real waveform into H3's joint NestedTensor AV latent —
    the img2img counterpart to nodes_minimax_h3._empty_av_latent. The audio side
    mirrors MiniMaxH3ReferenceToVideo._encode_ref_audio's own resample-then-encode
    exactly, since that's the only place in H3's own source that encodes real
    (non-empty) audio."""
    import comfy.nested_tensor
    import torchaudio

    n = _trim_to_grid(frames.shape[0])
    video_latent = vae.encode(frames[:n])

    waveform = audio["waveform"]
    sr = audio["sample_rate"]
    vae_sr = getattr(audio_vae, "audio_sample_rate", 32000)
    if sr != vae_sr:
        waveform = torchaudio.functional.resample(waveform, sr, vae_sr)
    audio_latent = audio_vae.encode(waveform[:1].movedim(1, -1))

    return {"samples": comfy.nested_tensor.NestedTensor((video_latent, audio_latent))}, n


class MuseMinimaxRefine:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "model": ("MODEL",),
                "clip": ("CLIP",),
                "vae": ("VAE",),
                "audio_vae": ("VAE",),
                "prompt": ("STRING", {"default": "", "multiline": True,
                    "tooltip": "Wire this from your director-style node's compiled_prompt output — the "
                               "refine pass reuses the exact prompt the candidate was generated from."}),
                "candidate": ("INT", {"default": 0, "min": 0, "max": 4,
                    "tooltip": "Which of the four candidate slots to refine. Set by the button selector in "
                               "the node's UI. Defaults to 0 (none picked yet) — the node deliberately refuses "
                               "to run at 0, rather than silently falling back to candidate 1, so a graph "
                               "can't get queued before you've actually chosen one."}),
                "aspect_ratio": (ASPECT_RATIO_OPTIONS, {"default": AspectRatio.WIDESCREEN_H.value}),
                "megapixels": ("FLOAT", {"default": 1.96, "min": 0.1, "max": 4.0, "step": 0.02,
                    "tooltip": "Target resolution for the refined output — normally higher than whatever "
                               "resolution the candidates were scouted at."}),
                "multiple": ("INT", {"default": 32, "min": 8, "max": 128, "step": 4, "advanced": True}),
                "resize_method": (["crop", "pad", "stretch"], {"default": "crop", "tooltip":
                    "How the selected candidate's frames get fit to the new target resolution when its "
                    "aspect ratio doesn't match."}),
                "ref_image_size": (["match", "max"], {"default": "match", "tooltip":
                    "Only used when ref_images is connected. 'match' scales references down to the output's "
                    "pixel area (faster). 'max' keeps up to a 2048px short edge for stronger identity "
                    "fidelity, but reference tokens ride every sampling step so it's several times slower."}),
                "denoise": ("FLOAT", {"default": 0.4, "min": 0.05, "max": 1.0, "step": 0.01,
                    "tooltip": "How much of the candidate's own content survives vs. gets redrawn. Low "
                               "values (~0.2-0.3) stay close to the candidate and mostly add resolution/detail. "
                               "High values (~0.6+) let the model redraw more, which can drift from the "
                               "candidate's content — 1.0 regenerates from scratch and ignores it entirely."}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
                "steps": ("INT", {"default": 20, "min": 1, "max": 100}),
                "sampler_name": (["res_multistep", "euler", "euler_ancestral", "dpmpp_2m"], {"default": "res_multistep"}),
                "scheduler": (["simple", "normal", "beta", "sgm_uniform"], {"default": "simple"}),
                "shift_video": ("FLOAT", {"default": 12.0, "min": 0.01, "max": 100.0, "step": 0.01, "advanced": True}),
                "shift_audio": ("FLOAT", {"default": 3.0, "min": 0.01, "max": 100.0, "step": 0.01, "advanced": True}),
            },
            "optional": {
                "candidate_1_images": ("IMAGE",),
                "candidate_1_audio": ("AUDIO",),
                "candidate_2_images": ("IMAGE",),
                "candidate_2_audio": ("AUDIO",),
                "candidate_3_images": ("IMAGE",),
                "candidate_3_audio": ("AUDIO",),
                "candidate_4_images": ("IMAGE",),
                "candidate_4_audio": ("AUDIO",),
                "ref_images": ("IMAGE", {"tooltip":
                    "The same character/product reference photos that anchored identity and fine detail "
                    "(exact prop shape, skin, likeness) in the original candidate — e.g. wired from "
                    "MuseMinimaxDirector's ref_images_used output. Without this, the refine pass only has "
                    "the text prompt to go on, and any detail the prompt doesn't spell out explicitly is "
                    "free to drift during the partial-denoise redraw — this is what locks it back down."}),
            },
        }

    RETURN_TYPES = ("IMAGE", "AUDIO")
    RETURN_NAMES = ("images", "audio")
    FUNCTION = "execute"
    CATEGORY = "Muse Collective"

    def execute(self, model, clip, vae, audio_vae, prompt, candidate, aspect_ratio, megapixels, multiple,
                resize_method, ref_image_size, denoise, seed, steps, sampler_name, scheduler, shift_video, shift_audio,
                candidate_1_images=None, candidate_1_audio=None,
                candidate_2_images=None, candidate_2_audio=None,
                candidate_3_images=None, candidate_3_audio=None,
                candidate_4_images=None, candidate_4_audio=None,
                ref_images=None):
        candidates = {
            1: (candidate_1_images, candidate_1_audio),
            2: (candidate_2_images, candidate_2_audio),
            3: (candidate_3_images, candidate_3_audio),
            4: (candidate_4_images, candidate_4_audio),
        }
        # Not-ready-yet states (no candidate picked, or the picked slot isn't wired)
        # block downstream execution via ExecutionBlocker rather than raising. Passing
        # a message to ExecutionBlocker still fires ComfyUI's own "execution_error"
        # event (a visible red error toast) — confirmed directly from execution.py's
        # execution_block_cb. ExecutionBlocker(None) blocks silently in the UI instead;
        # the log.warning below is the only visible trace, in the console, not a popup.
        if candidate == 0:
            log.warning("[MuseMinimaxRefine] No candidate selected (candidate=0) — click one of the four "
                        "buttons in the node's UI to pick which candidate to refine. Blocking, not running.")
            blocker = ExecutionBlocker(None)
            return (blocker, blocker)
        images, audio = candidates.get(candidate, (None, None))
        if images is None or audio is None:
            log.warning("[MuseMinimaxRefine] Candidate slot %d has no images/audio connected — wire "
                        "candidate_%d_images and candidate_%d_audio, or pick a filled slot. Blocking, not running.",
                        candidate, candidate, candidate)
            blocker = ExecutionBlocker(None)
            return (blocker, blocker)

        width, height = _resolve_resolution(aspect_ratio, megapixels, multiple)
        resized_frames = _fit_image_to_target(images, width, height, resize_method)

        from nodes import NODE_CLASS_MAPPINGS
        CLIPTextEncode = NODE_CLASS_MAPPINGS["CLIPTextEncode"]
        RandomNoise = NODE_CLASS_MAPPINGS["RandomNoise"]
        BasicGuider = NODE_CLASS_MAPPINGS["BasicGuider"]
        KSamplerSelect = NODE_CLASS_MAPPINGS["KSamplerSelect"]
        BasicScheduler = NODE_CLASS_MAPPINGS["BasicScheduler"]
        SamplerCustomAdvanced = NODE_CLASS_MAPPINGS["SamplerCustomAdvanced"]
        VAEDecode = NODE_CLASS_MAPPINGS["VAEDecode"]

        latent, trimmed_n = _encode_av_latent(vae, audio_vae, resized_frames, audio)

        log.info("[MuseMinimaxRefine] candidate=%d, %dx%d, seed=%d, denoise=%.2f, %d frame(s)",
                  candidate, width, height, seed, denoise, trimmed_n)

        shifted_model = _unpack_node_result(_execute_comfy_node(
            MiniMaxH3SigmaShift, model=model, shift_video=shift_video, shift_audio=shift_audio,
        ))[0]
        sampler = _unpack_node_result(_execute_comfy_node(KSamplerSelect, sampler_name=sampler_name))[0]

        # Same identity/prop-detail lock the candidate was originally generated with —
        # without this, only the text prompt constrains what the partial-denoise redraw
        # can change, and anything the prompt doesn't spell out (exact prop shape, skin
        # detail, likeness) is free to drift toward the model's own generic defaults.
        # Reuses MiniMaxH3ReferenceToVideo's own ref_items/<Picture N> assembly directly
        # (its own conditioning output only — the AV latent it also returns is discarded,
        # since the real starting latent here comes from the candidate's own pixels).
        ref_images_dict = None
        if ref_images is not None and ref_images.shape[0] > 0:
            ref_images_dict = {f"ref_image_{i}": ref_images[i:i + 1] for i in range(ref_images.shape[0])}
        else:
            log.warning("[MuseMinimaxRefine] No ref_images connected — refining from text only. Fine detail "
                        "that was only ever anchored by the original reference photos (exact props, skin, "
                        "likeness) may drift. Wire in the same reference photos the candidate used to lock it.")

        if ref_images_dict:
            positive = _unpack_node_result(_execute_comfy_node(
                MiniMaxH3ReferenceToVideo, clip=clip, vae=vae, audio_vae=audio_vae, prompt=prompt,
                width=width, height=height, length=trimmed_n, ref_image_size=ref_image_size,
                ref_images=ref_images_dict,
            ))[0]
        else:
            positive = _unpack_node_result(_execute_comfy_node(CLIPTextEncode, clip=clip, text=prompt))[0]

        noise = _unpack_node_result(_execute_comfy_node(RandomNoise, noise_seed=seed))[0]
        guider = _unpack_node_result(_execute_comfy_node(BasicGuider, model=shifted_model, conditioning=positive))[0]
        sigmas = _unpack_node_result(_execute_comfy_node(
            BasicScheduler, model=shifted_model, scheduler=scheduler, steps=steps, denoise=denoise,
        ))[0]
        sampled = _unpack_node_result(_execute_comfy_node(
            SamplerCustomAdvanced, noise=noise, guider=guider, sampler=sampler, sigmas=sigmas, latent_image=latent,
        ))[0]

        refined_images = _unpack_node_result(_execute_comfy_node(VAEDecode, samples=sampled, vae=vae))[0]

        return (refined_images, audio)


NODE_CLASS_MAPPINGS = {
    "MuseMinimaxRefine": MuseMinimaxRefine,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MuseMinimaxRefine": "Muse Minimax Refine",
}
