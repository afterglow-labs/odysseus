"""Direct-core BFS MiniMax H3 head-swap recipe, without a ComfyUI server.

The author conditions Ref2VA on the identity image, then anchors the source
video as a guide. Treating that video as an ordinary reference is a different
recipe and does not implement this workflow.
"""
import math

try:
    from .h3_video_worker import H3Runtime
except ImportError:  # Direct invocation by the sibling worker script.
    from h3_video_worker import H3Runtime


WORKFLOW_ID = "h3_head_swap"
SAMPLERS = ("euler", "res_multistep")
SCHEDULERS = ("beta", "simple", "normal")
FPS = 24


def build_prompt(notes=""):
    """Describe input roles explicitly without inventing visual observations."""
    notes = str(notes or "").strip()
    if notes.lower().startswith("head_swap:"):
        return notes
    prompt = """head_swap:

subject_definitions:
<Subject 1> is the complete head identity shown in <Picture 1>: the face, head shape, hair, hairline, skin appearance, and distinctive features.
<Subject 2> is the person in <Video 1>. Retain their body, clothing, pose, performance, and movement.
<Video 1> provides the source footage, camera, setting, lighting, composition, motion, and timing.

summary:
[video editing + reference generation]
Replace only the head of <Subject 2> in <Video 1> with <Subject 1> from <Picture 1>.

retention_analysis:
<Subject 1>: attribute_transfer — transfer the complete reference head identity consistently.
<Subject 2>: partially_preserved — preserve the body and performance; replace only the head.
<Video 1>: partially_preserved — retain the entire source scene and timeline apart from the head replacement.

detailed_description:
Follow the source head's position, rotation, scale, expression, gaze, and occlusions while preserving the identity in <Picture 1>. Match the original lighting, perspective, shadows, and motion blur. Connect the replacement naturally to the original neck. Keep the face and hair stable between frames. Preserve the source body, clothing, hands, actions, background, framing, camera motion, and duration.

overall_soundscape:
Keep the original audiovisual timing; head replacement must not alter the action.

non_diegetic_music:
N/A"""
    if notes:
        prompt += "\n\nAdditional user instructions:\n" + notes
    return prompt


def normalize_config(config):
    """Validate recipe-specific options before weights are allocated."""
    config = dict(config)
    workflow = config.get("workflow_id", WORKFLOW_ID)
    if workflow != WORKFLOW_ID:
        raise ValueError("Unknown BFS MiniMax H3 workflow")
    if config.get("second_pass") or config.get("upscaler"):
        raise ValueError("The optional H3 second-pass latent upscaler is not configured for this recipe")
    if not config.get("lora"):
        raise ValueError("BFS MiniMax H3 needs the head-swap LoRA")
    config.setdefault("sampler", "euler")
    config.setdefault("scheduler", "beta")
    if config["sampler"] not in SAMPLERS or config["scheduler"] not in SCHEDULERS:
        raise ValueError("Choose a supported BFS H3 sampler and scheduler")
    config.setdefault("steps", 8 if config.get("speed_lora") else 20)
    if not isinstance(config["steps"], int) or not 1 <= config["steps"] <= 100:
        raise ValueError("BFS H3 steps must be between 1 and 100")
    config.setdefault("seed", 42)
    config.setdefault("lora_scale", 1.0)
    config.setdefault("speed_lora_scale", 1.0)
    config.setdefault("shift_video", 12.0)
    config.setdefault("shift_audio", 3.0)
    config.setdefault("reference_size", "match")
    for name in ("lora_scale", "speed_lora_scale", "shift_video", "shift_audio"):
        value = config[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"BFS H3 {name} must be a finite number")
        if name.endswith("scale") and not -4 <= value <= 4:
            raise ValueError(f"BFS H3 {name} must be between -4 and 4")
        if name.startswith("shift_") and not 0 < value <= 100:
            raise ValueError(f"BFS H3 {name} must be greater than 0 and at most 100")
    if config["reference_size"] not in ("match", "max"):
        raise ValueError("BFS H3 reference size must be match or max")
    config.setdefault("audio_mode", "source")
    if config["audio_mode"] not in ("source", "silent"):
        raise ValueError("BFS H3 audio mode must be source or silent")
    for axis in ("width", "height"):
        value = config.get(axis)
        if not isinstance(value, int) or value < 32 or value % 32:
            raise ValueError("BFS H3 width and height must be positive multiples of 32")
    if config["width"] * config["height"] > 768 * 1344:
        raise ValueError("BFS H3 canvas must not exceed 1,032,192 pixels")
    config["prompt"] = build_prompt(config.get("prompt", ""))
    if len(config["prompt"]) > 16000:
        raise ValueError("BFS H3 prompt, including input-role instructions, must be at most 16,000 characters")
    return config


def _as_frames(value, name):
    import torch
    if value is None:
        raise ValueError(f"BFS H3 needs {name}")
    value = torch.as_tensor(value, dtype=torch.float32, device="cpu")
    if value.ndim == 3:
        value = value.unsqueeze(0)
    if value.ndim != 4 or value.shape[-1] not in (3, 4) or value.shape[0] < 1:
        raise ValueError(f"Invalid {name}: expected RGB image frames")
    if not torch.isfinite(value).all() or value.min() < 0 or value.max() > 1:
        raise ValueError(f"Invalid {name}: expected finite RGB values from 0 to 1")
    return value[..., :3]


def _identity_canvas(runtime, image, width, height):
    """Match the author’s aspect-preserving black padding of the head photo."""
    import torch.nn.functional as F
    image = image[:1].movedim(-1, 1)
    scale = min(width / image.shape[-1], height / image.shape[-2])
    resized_w = max(1, min(width, round(image.shape[-1] * scale)))
    resized_h = max(1, min(height, round(image.shape[-2] * scale)))
    image = runtime.utils.common_upscale(image, resized_w, resized_h, "lanczos", "disabled")
    pad_w, pad_h = width - resized_w, height - resized_h
    image = F.pad(image, (pad_w // 2, pad_w - pad_w // 2, pad_h // 2, pad_h - pad_h // 2))
    return image.movedim(1, -1)


def _guide_audio(source, mode, frame_count):
    """Keep source channels/timing, or supply explicit silence for both stages."""
    import torch
    duration = frame_count / FPS
    if mode == "silent" or source is None:
        return {"waveform": torch.zeros((1, 2, round(duration * 32000))), "sample_rate": 32000}
    sr = int(source["sample_rate"])
    if sr < 1:
        raise ValueError("Source soundtrack has an invalid sample rate")
    waveform = torch.as_tensor(source["waveform"], dtype=torch.float32, device="cpu")
    if waveform.ndim != 3 or waveform.shape[0] != 1 or waveform.shape[1] not in (1, 2):
        raise ValueError("Source soundtrack must contain one mono or stereo waveform")
    if not torch.isfinite(waveform).all():
        raise ValueError("Source soundtrack contains non-finite audio")
    if waveform.shape[1] == 1:
        waveform = waveform.repeat(1, 2, 1)
    length = round(duration * sr)
    waveform = waveform[..., :length]
    if waveform.shape[-1] < length:
        waveform = torch.nn.functional.pad(waveform, (0, length - waveform.shape[-1]))
    return {"waveform": waveform, "sample_rate": sr}


def _apply_lora(runtime, model, path, strength, progress, phase):
    progress(phase)
    before = sum(len(patches) for patches in model.patches.values())
    state = runtime.utils.load_torch_file(path, safe_load=True)
    updated, _ = runtime.sd.load_lora_for_models(model, None, state, strength, 0)
    after = sum(len(patches) for patches in updated.patches.values())
    if strength != 0 and after <= before:
        raise ValueError(f"The {phase.replace('loading_', '').replace('_', ' ')} has no compatible H3 weights")
    return updated


def generate(config, media, progress, *, runtime=None, runtime_factory=H3Runtime):
    """Return RGB frames and source/silent audio for the shared job supervisor.

    The caller must set CUDA_VISIBLE_DEVICES before invoking this function.
    Inputs are decoded BHWC/THWC RGB floats in [0, 1] at 24 fps, with an optional
    source soundtrack {waveform: [1,channels,samples], sample_rate: int}.
    """
    config = normalize_config(config)
    progress("initializing_runtime", total_steps=config["steps"])
    if runtime is None:
        runtime = runtime_factory(config["runtime_path"])
    source = _as_frames(media.get("source_video"), "a source video")
    identity = _as_frames(media.get("identity_image"), "a head reference image")
    requested = config.get("frames", source.shape[0])
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 5:
        raise ValueError("BFS H3 frame count must be an integer of at least 5")
    frame_count = min(requested, source.shape[0])
    frame_count -= (frame_count - 5) % 17
    if frame_count < 5 or frame_count > 362:
        raise ValueError("BFS H3 needs 5–362 source frames at 24 fps")
    config["frames"] = frame_count
    source = source[:frame_count]
    audio = _guide_audio(media.get("source_audio"), config["audio_mode"], frame_count)
    progress("preparing_media", frames=frame_count, fps=FPS, total_steps=config["steps"])
    import torch
    with torch.inference_mode():
        # Load the base without the generic worker's single-LoRA path, so the
        # speed adapter precedes head-swap exactly as in the author's graph.
        load_config = {**config, "lora": None}
        model, clip, vae, audio_vae = runtime.load(load_config, progress)
        if config.get("speed_lora"):
            model = _apply_lora(runtime, model, config["speed_lora"], config["speed_lora_scale"],
                                progress, "loading_speed_adapter")
        model = _apply_lora(runtime, model, config["lora"], config["lora_scale"],
                            progress, "loading_head_swap_adapter")
        identity = _identity_canvas(runtime, identity, config["width"], config["height"])
        progress("encoding_identity")
        positive, latent = runtime.h3.MiniMaxH3ReferenceToVideo.execute(
            clip=clip, vae=vae, audio_vae=audio_vae, prompt=config["prompt"],
            width=config["width"], height=config["height"], length=frame_count,
            ref_image_size=config["reference_size"], ref_images={"ref_image_0": identity})
        progress("encoding_source_guide")
        positive = runtime.h3.MiniMaxH3AddGuide.execute(
            positive=positive, latent=latent, frame_idx=0, vae=vae,
            audio_vae=audio_vae, image=source, audio=audio)[0]
        progress("sampling", 0)
        samples = runtime.generate(config, model, positive, latent, progress)
        progress("decoding_video")
        frames = runtime.nodes.VAEDecode().decode(vae, samples)[0]
    # The authored workflow muxes the selected original/silent soundtrack.
    # Do not replace it with the model's generated audio stream.
    return frames, audio
