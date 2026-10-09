#!/usr/bin/env python3
"""One-job MiniMax H3 inference worker. No HTTP server or workflow executor.

ML imports deliberately live below manifest validation and GPU selection. The
parent owns authentication, upload paths, job cancellation and output delivery.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time

FPS = 24
AUDIO_RATE = 32000
VFX_EDIT_LORA = "minimax_h3_vfx_edit_v1.0_r128.safetensors"
VFX_EDIT_LORAS = {VFX_EDIT_LORA, "minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors"}
RUNTIME_REVISION = "5c460d8172fe30761ff67c0df3d5643bb74e0d70"
GPU_UUID = re.compile(r"GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def configure_cuda_allocator(environ=None):
    """Apply this server's video-worker allocator setting before importing torch."""
    environ = os.environ if environ is None else environ
    configured = environ.get("ODYSSEUS_H3_PYTORCH_ALLOC_CONF")
    if isinstance(configured, str) and configured.strip():
        # Modern PyTorch reads this before the legacy PYTORCH_CUDA_ALLOC_CONF.
        # Only this video worker changes; the API and other model servers retain
        # their own allocator configuration.
        environ["PYTORCH_ALLOC_CONF"] = configured.strip()
        logging.info("Video worker PyTorch allocator configuration: %s", configured.strip())


def configure_fast_storage(storage, roots=None):
    """Use explicitly trusted storage roots when WSL hides the backing NVMe.

    Keep the runtime's detector for other paths. Resolve symlinks and check the
    device as well as the directory, so a NAS link or nested mount cannot inherit
    the local disk override. Native --disable-fast-disk still takes precedence.
    """
    original = getattr(storage, "_odysseus_original_fast_storage", storage.fast_storage)
    storage._odysseus_original_fast_storage = original
    storage.fast_storage = original
    if roots is None:
        try:
            roots = json.loads(os.environ.get("ODYSSEUS_H3_FAST_DISK_ROOTS", "[]"))
        except (ValueError, TypeError):
            logging.warning("Ignoring invalid ODYSSEUS_H3_FAST_DISK_ROOTS; expected a JSON list of directories")
            return
    if not isinstance(roots, (list, tuple)):
        logging.warning("Ignoring invalid fast storage roots; expected a list of directories")
        return
    trusted = []
    for value in roots:
        try:
            if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
                raise ValueError("Expected a directory path")
            root = Path(value).expanduser().resolve(strict=True)
            if not root.is_dir():
                raise ValueError("Expected a directory")
            trusted.append((root, root.stat().st_dev))
        except (OSError, ValueError, RuntimeError):
            logging.warning("Ignoring unavailable or invalid fast storage directory")
    if not trusted:
        return

    def fast_storage(path):
        try:
            resolved = Path(path).resolve(strict=True)
            if resolved.is_file():
                device = resolved.stat().st_dev
                if any(device == root_device and resolved.is_relative_to(root)
                       for root, root_device in trusted):
                    return True
        except (OSError, ValueError, TypeError, RuntimeError):
            pass
        return original(path)

    storage.fast_storage = fast_storage
    logging.info("Fast disk policy enabled for configured model directories: %s", [str(root) for root, _ in trusted])


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)


class Progress:
    def __init__(self, path, total_steps):
        self.path = path
        self.total_steps = total_steps
        self.step = 0
        self.started = time.time()
        self.details = {}

    def __call__(self, phase, step=None, **extra):
        if step is not None:
            self.step = step
        atomic_json(self.path, {"phase": phase, "step": self.step,
                               "total_steps": self.total_steps,
                               "elapsed_seconds": round(time.time() - self.started, 2),
                               **self.details,
                               **extra})


def is_vfx_edit(config):
    """Both published editing adapters use an aligned source plus optional refs."""
    return any(Path(item["path"]).name.lower() in VFX_EDIT_LORAS and item["strength"] != 0
               for item in lora_stack(config))


def lora_stack(config):
    """Read the ordered manifest stack; an explicit empty list disables LoRAs."""
    if "loras" in config:
        return config["loras"]
    return ([{"path": config["lora"], "strength": config.get("lora_scale", 1)}]
            if config.get("lora") else [])


def validate_loras(config):
    adapters = lora_stack(config)
    if not isinstance(adapters, list) or len(adapters) > 8:
        raise ValueError("loras must be a list of at most 8 adapters")
    result, seen = [], set()
    for index, item in enumerate(adapters, 1):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError(f"LoRA {index} must have an absolute .safetensors path")
        path = Path(item["path"])
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".safetensors":
            raise ValueError(f"LoRA {index} must be an existing absolute .safetensors path")
        identity = path.resolve()
        if identity in seen:
            raise ValueError("Each LoRA may appear only once in the stack")
        seen.add(identity)
        try:
            strength = _number(item, "strength", 1, -4, 4)
        except (TypeError, ValueError) as error:
            raise ValueError(f"LoRA {index} strength must be a finite number between -4 and 4") from error
        result.append({"path": str(path), "strength": strength})
    config["loras"] = result
    config["lora"] = result[0]["path"] if result else None
    config["lora_scale"] = result[0]["strength"] if result else 1.0


def vfx_edit_prompt(prompt):
    body = re.sub(r"^(?:vfx_edit:\s*)+", "", prompt.strip(), flags=re.IGNORECASE)
    if not body:
        raise ValueError("VFX Edit needs an edit instruction after vfx_edit:")
    return "vfx_edit: " + body


def _number(config, name, default, low, high, integral=False):
    value = config.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high or (integral and not value.is_integer()):
        raise ValueError(f"{name} must be {'an integer ' if integral else ''}between {low} and {high}")
    return int(value) if integral else value


def visible_gpu_ids(config):
    """Primary first, optional VAE UUID second; called before importing torch."""
    primary = str(config.get("gpu", "0"))
    if not (primary.isascii() and primary.isdecimal()) and not GPU_UUID.fullmatch(primary):
        raise ValueError("Choose one GPU index or NVIDIA GPU UUID for MiniMax H3")
    vae = config.get("vae_gpu", "")
    if vae is None:
        vae = ""
    if not isinstance(vae, str) or (vae and not GPU_UUID.fullmatch(vae)):
        raise ValueError("Choose an NVIDIA GPU UUID for the separate VAE GPU, or leave it empty")
    if not vae or vae.lower() == primary.lower():
        return (primary,)
    return primary, vae


def validate_job(manifest):
    config = dict(manifest["config"])
    uploads = manifest.get("uploads", manifest)
    if config.get("mode", "t2va") not in {"t2va", "fl2va", "ref2va"}:
        raise ValueError("Unknown MiniMax H3 mode")
    config.setdefault("mode", "t2va")
    if not isinstance(config.get("prompt"), str) or not config["prompt"].strip():
        raise ValueError("A prompt is required")
    if len(config["prompt"]) > 16000:
        raise ValueError("Prompt is too long")
    for name in ("model", "encoder", "video_vae", "audio_vae"):
        value = config.get(name)
        path = Path(value or "")
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".safetensors":
            raise ValueError(f"{name} must be an existing absolute .safetensors path")
        config[name] = str(path)
    validate_loras(config)
    devices = visible_gpu_ids(config)
    config["gpu"] = devices[0]
    config["vae_gpu"] = devices[1] if len(devices) > 1 else ""
    for name, default, low, high in (("width", 960, 32, 2048), ("height", 544, 32, 2048),
                                   ("frames", 124, 5, 362), ("steps", 20, 1, 100)):
        config[name] = _number(config, name, default, low, high, True)
    for name in ("width", "height"):
        if config[name] % 32:
            raise ValueError(f"{name} must be a multiple of 32")
    if config["width"] * config["height"] > 768 * 1344:
        raise ValueError("MiniMax H3 canvas must not exceed 1,032,192 pixels")
    config["frames"] += (5 - config["frames"]) % 17
    seed = config.get("seed", 42)
    if isinstance(seed, bool) or str(seed).strip() != str(int(seed)) or not 0 <= int(seed) <= 2**64 - 1:
        raise ValueError("seed must be an unsigned 64-bit integer")
    config["seed"] = int(seed)
    for name, default, low, high in (("shift_video", 12, 0.01, 100), ("shift_audio", 3, 0.01, 100)):
        config[name] = _number(config, name, default, low, high)
    for name, default, choices in (("sampler", "euler", {"euler", "res_multistep"}),
                                   ("scheduler", "simple", {"simple", "normal"}),
                                   ("reference_size", "match", {"match", "max"})):
        config.setdefault(name, default)
        if config[name] not in choices:
            raise ValueError(f"Unsupported {name}")
    media = {}
    for name, limit in (("reference_images", 9), ("reference_videos", 3), ("reference_audio", 3)):
        values = uploads.get(name, [])
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"At most {limit} {name.replace('_', ' ')} are supported")
        media[name] = values
    for name in ("first_frame", "last_frame", "source_video"):
        media[name] = uploads.get(name) or None
    if media["source_video"] is not None and not isinstance(media["source_video"], str):
        raise ValueError("VFX Edit needs exactly one source video path")
    vfx_edit = is_vfx_edit(config)
    if vfx_edit:
        if config["mode"] != "ref2va":
            raise ValueError("VFX Edit requires reference mode with a Ref2VA base model")
        # Earlier jobs stored the aligned source in their only video-reference
        # slot. Migrate that contract locally without mutating saved manifests.
        # An explicit source field (even empty) always prevents this fallback.
        if "source_video" not in uploads and len(media["reference_videos"]) == 1:
            media["source_video"] = media["reference_videos"][0]
            media["reference_videos"] = []
        if not media["source_video"]:
            raise ValueError("VFX Edit needs exactly one source video")
        if media["first_frame"] or media["last_frame"]:
            raise ValueError("VFX Edit uses native image references, not first/last keyframes")
        config["prompt"] = vfx_edit_prompt(config["prompt"])
        if len(config["prompt"]) > 16000:
            raise ValueError("VFX Edit prompt, including vfx_edit:, must be at most 16,000 characters")
    elif media["source_video"]:
        raise ValueError("A source video requires an active VFX Edit LoRA")
    total = sum(len(media[name]) for name in ("reference_images", "reference_videos", "reference_audio"))
    if total > 12:
        raise ValueError("At most 12 references are supported")
    if config["mode"] == "ref2va":
        if not vfx_edit and not media["reference_images"] and not media["reference_videos"]:
            raise ValueError("Reference mode needs at least one reference image or video; audio alone is unsupported")
        if media["first_frame"] or media["last_frame"]:
            raise ValueError("First/last frames belong to first/last-frame mode")
    elif total:
        raise ValueError("Reference uploads require reference mode")
    if config["mode"] == "fl2va" and not (media["first_frame"] or media["last_frame"]):
        raise ValueError("First/last-frame mode requires a first frame or a last frame")
    if config["mode"] == "t2va" and (media["first_frame"] or media["last_frame"]):
        raise ValueError("Keyframes require first/last-frame mode")
    for values in media.values():
        for value in values if isinstance(values, list) else ([values] if values else []):
            path = Path(value)
            if not path.is_absolute() or not path.is_file():
                raise ValueError("Media files must be existing absolute paths")
    output = Path(manifest["output_path"])
    if not output.is_absolute() or output.suffix.lower() != ".mp4":
        raise ValueError("output_path must be an absolute MP4 path")
    return config, media


def _fit_size(width, height, max_area, max_short_edge=None):
    scale = min(1.0, math.sqrt(max_area / (width * height)))
    if max_short_edge:
        scale = min(scale, max_short_edge / min(width, height))
    return max(1, round(width * scale)), max(1, round(height * scale))


def read_image(path, max_area):
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source).convert("RGB")
        source.thumbnail(_fit_size(*source.size, max_area), Image.Resampling.LANCZOS)
        array = np.asarray(source, dtype=np.float32) / 255.0
    return torch.from_numpy(array.copy()).unsqueeze(0)


def read_audio(path, *, optional=False, check_minimum=True):
    """Decode one bounded track, resample and mix to stereo before tensor creation."""
    import av
    import numpy as np
    import torch

    chunks = []
    count = 0
    with av.open(str(path)) as source:
        if not source.streams.audio:
            if optional:
                return None, 0.0
            raise ValueError("Reference audio contains no audio track")
        stream = source.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout="stereo", rate=AUDIO_RATE)
        for frame in source.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray())
                count += converted.samples
            if count > AUDIO_RATE * 15 + 2048:
                raise ValueError("Reference audio must not exceed 15 seconds")
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray())
            count += converted.samples
    duration = count / AUDIO_RATE
    if duration > 15.05 or (check_minimum and duration < 2):
        raise ValueError("Reference audio must be 2–15 seconds")
    if not chunks:
        raise ValueError("Audio track is empty")
    waveform = np.concatenate(chunks, axis=1)[:, :15 * AUDIO_RATE]
    return {"waveform": torch.from_numpy(waveform.copy()).unsqueeze(0), "sample_rate": AUDIO_RATE}, min(duration, 15.0)


def read_video(path):
    """Resample VFR clips to 24fps and resize during decode, before float tensors."""
    import av
    import numpy as np
    import torch

    arrays = []
    first_time = None
    prior = None
    prior_time = 0.0
    last_time = 0.0
    last_duration = 1 / FPS
    count = 0
    with av.open(str(path)) as source:
        if not source.streams.video:
            raise ValueError("Reference video contains no video track")
        stream = source.streams.video[0]
        source_rate = float(stream.average_rate or FPS)
        if not math.isfinite(source_rate) or source_rate <= 0:
            source_rate = FPS
        for frame in source.decode(stream):
            current_time = float(frame.time) if frame.time is not None else count / source_rate
            count += 1
            if first_time is None:
                first_time = current_time
            current_time = max(last_time, current_time - first_time)
            if current_time > 15.05:
                raise ValueError("Reference video must not exceed 15 seconds")
            width, height = _fit_size(frame.width, frame.height, 768 * 1344)
            current = frame.reformat(width=width, height=height, format="rgb24").to_ndarray()
            rotation = int(frame.rotation)
            if rotation % 90 == 0 and rotation:
                current = np.rot90(current, rotation // 90).copy()
            # Pick the nearer source frame for each target timestamp. No giant
            # decoded full-resolution stack or high-fps intermediates.
            while len(arrays) / FPS <= current_time and len(arrays) < FPS * 15:
                target = len(arrays) / FPS
                arrays.append(prior if prior is not None and target - prior_time < current_time - target else current)
            prior, prior_time, last_time = current, current_time, current_time
            last_duration = float(frame.duration * frame.time_base) if frame.duration and frame.time_base else 1 / source_rate
        duration = last_time + last_duration if count else 0
        if duration < 2 - 1e-3 or duration > 15.05:
            raise ValueError("Reference video must be 2–15 seconds")
        while len(arrays) < min(round(duration * FPS), 15 * FPS):
            arrays.append(prior)
    soundtrack, audio_duration = read_audio(path, optional=True, check_minimum=False)
    if soundtrack is not None:
        length = round(min(duration, 15.0) * AUDIO_RATE)
        soundtrack["waveform"] = soundtrack["waveform"][..., :length]
        audio_duration = min(audio_duration, length / AUDIO_RATE)
    frames = torch.from_numpy(np.stack(arrays)).to(dtype=torch.float32).div_(255)
    return frames, soundtrack, min(duration, 15.0), audio_duration


def prepare_media(media, config):
    # Prepare the guide first so native references use the source-aligned
    # canvas and sampling length. The source never occupies a native ref slot.
    prepared = prepare_vfx_media(media, config) if is_vfx_edit(config) else {}
    area = config["width"] * config["height"]
    for name in ("first_frame", "last_frame"):
        if media[name]:
            prepared[name] = read_image(media[name], max(area, 768 * 1344))
    # Max references can be costly; bound to the upstream 2048-short-edge
    # reference canvas, with an additional 8MP decoder bound for unusual ratios.
    image_area = area if config["reference_size"] == "match" else 8 * 1024 * 1024
    prepared["ref_images"] = {f"ref_image_{i}": read_image(path, image_area)
                              for i, path in enumerate(media["reference_images"], 1)}
    prepared["ref_videos"], prepared["ref_video_audios"] = {}, {}
    video_duration = audio_duration = 0.0
    for i, path in enumerate(media["reference_videos"], 1):
        frames, soundtrack, duration, sound_duration = read_video(path)
        video_duration += duration
        audio_duration += sound_duration
        if video_duration > 15.05 or audio_duration > 15.05:
            raise ValueError("Reference clips must total at most 15 seconds per modality")
        # The core rounds reference clips DOWN to 17k+5. Pad up first so it
        # receives the ending instead of silently dropping up to 16 frames and
        # their soundtrack. The configured output length remains the hard cap;
        # it is already on the same grid, so padding cannot exceed that limit.
        usable = min(len(frames), config["frames"])
        aligned = usable + (5 - usable) % 17
        frames = frames[:usable]
        if aligned > usable:
            import torch

            frames = torch.cat((frames, frames[-1:].expand(aligned - usable, *frames.shape[1:])), dim=0)
        prepared["ref_videos"][f"ref_video_{i}"] = frames
        if soundtrack is not None:
            samples = round(aligned / FPS * soundtrack["sample_rate"])
            waveform = soundtrack["waveform"][..., :samples]
            if waveform.shape[-1] < samples:
                import torch

                silence = waveform.new_zeros((*waveform.shape[:-1], samples - waveform.shape[-1]))
                waveform = torch.cat((waveform, silence), dim=-1)
            prepared["ref_video_audios"][f"ref_video_audio_{i}"] = {**soundtrack, "waveform": waveform}
    prepared["ref_audios"] = {}
    for i, path in enumerate(media["reference_audio"], 1):
        audio, duration = read_audio(path)
        audio_duration += duration
        if audio_duration > 15.05:
            raise ValueError("Reference audio, including video soundtracks, must total at most 15 seconds")
        prepared["ref_audios"][f"ref_audio_{i}"] = audio
    return prepared


def _vfx_canvas(width, height, area):
    """Keep source aspect within the selected pixel budget and H3's grid."""
    scale = min(math.sqrt(area / (width * height)), 2048 / width, 2048 / height)
    out_w, out_h = (max(32, round(axis * scale / 32) * 32) for axis in (width, height))
    ratio = width / height
    while out_w * out_h > area:
        candidates = [(w, h) for w, h in ((out_w - 32, out_h), (out_w, out_h - 32))
                      if w >= 32 and h >= 32]
        out_w, out_h = min(candidates, key=lambda size: abs(math.log(size[0] / size[1] / ratio)))
    return out_w, out_h


def prepare_vfx_media(media, config):
    """Pad the complete source for sampling, retaining its original output length."""
    import torch

    frames, soundtrack, _, _ = read_video(media["source_video"])
    output_frames = len(frames)
    if output_frames < 73:
        raise ValueError("VFX Edit needs at least 73 source frames at 24 fps (about 3.05 seconds)")
    sampling_frames = output_frames + (5 - output_frames) % 17
    if sampling_frames > 362:
        raise ValueError("VFX Edit source video must not exceed 15 seconds")
    config["frames"] = sampling_frames
    config["width"], config["height"] = _vfx_canvas(
        frames.shape[2], frames.shape[1], config["width"] * config["height"])
    # H3 crops non-grid guides down. Repeat only the last frame to fill its
    # grid, then remove those padding frames after decoding, before muxing.
    padding = sampling_frames - output_frames
    if padding:
        frames = torch.cat((frames, frames[-1:].expand(padding, *frames.shape[1:])), dim=0)
    silence = {"waveform": torch.zeros((1, 2, round(sampling_frames / FPS * AUDIO_RATE)),
                                       dtype=torch.float32, device="cpu"),
               "sample_rate": AUDIO_RATE}
    soundtrack = soundtrack if soundtrack is not None else silence
    soundtrack = {**soundtrack, "waveform": soundtrack["waveform"][
        ..., :round(output_frames / FPS * soundtrack["sample_rate"])]}
    logging.info("VFX Edit source guide: %d output frames, %d sampling frames at %d fps, %dx%d; retaining the source soundtrack",
                 output_frames, sampling_frames, FPS, config["width"], config["height"])
    # The author's standard graph guides with EmptyAudio. Preserve the actual
    # source soundtrack separately for muxing; a silent source stays silent.
    return {"guide_video": frames, "guide_audio": silence,
            "source_audio": soundtrack, "output_frames": output_frames}


def mux_mp4(output_path, frames, audio, fps=FPS):
    """Encode RGB frames and their selected audio into one atomic H.264/AAC MP4."""
    import av
    import numpy as np

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.stem + f".{os.getpid()}.tmp.mp4")
    if hasattr(frames, "detach"):
        frames = frames.detach().cpu()
    if len(frames) < 1:
        raise ValueError("Video decoder produced no frames")
    waveform = audio["waveform"]
    if hasattr(waveform, "detach"):
        waveform = waveform.detach().float().cpu().numpy()
    waveform = np.asarray(waveform, dtype=np.float32)
    if waveform.ndim == 3:
        waveform = waveform[0]
    if waveform.ndim != 2 or waveform.shape[0] not in (1, 2):
        raise ValueError("Audio decoder must produce mono or stereo audio")
    if not np.isfinite(waveform).all():
        raise FloatingPointError("Audio decoder produced non-finite samples; check model precision and sampling settings")
    if waveform.shape[0] == 1:
        waveform = np.repeat(waveform, 2, axis=0)
    sample_rate = int(audio["sample_rate"])
    duration_samples = round(len(frames) / fps * sample_rate)
    waveform = waveform[:, :duration_samples]
    if waveform.shape[1] < duration_samples:
        waveform = np.pad(waveform, ((0, 0), (0, duration_samples - waveform.shape[1])))
    waveform = np.clip(waveform, -1.0, 1.0)
    try:
        with av.open(str(temporary), mode="w", options={"movflags": "+faststart"}) as container:
            video = container.add_stream("libx264", rate=fps)
            video.width, video.height = int(frames[0].shape[1]), int(frames[0].shape[0])
            video.pix_fmt = "yuv420p"
            video.options = {"crf": "18", "preset": "medium"}
            sound = container.add_stream("aac", rate=sample_rate)
            sound.layout = "stereo"
            sound.bit_rate = 192000
            for i, pixels in enumerate(frames):
                if hasattr(pixels, "numpy"):
                    pixels = pixels.numpy()
                pixels = np.asarray(pixels)
                if pixels.dtype != np.uint8:
                    if not np.isfinite(pixels).all():
                        raise FloatingPointError("Video decoder produced non-finite pixels; check model precision and sampling settings")
                    pixels = (pixels.clip(0, 1) * 255).round().astype(np.uint8)
                frame = av.VideoFrame.from_ndarray(pixels[..., :3], format="rgb24")
                frame.pts, frame.time_base = i, Fraction(1, fps)
                for packet in video.encode(frame):
                    container.mux(packet)
            for packet in video.encode():
                container.mux(packet)
            for start in range(0, duration_samples, 1024):
                frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(waveform[:, start:start + 1024]),
                                                  format="fltp", layout="stereo")
                frame.sample_rate, frame.pts, frame.time_base = sample_rate, start, Fraction(1, sample_rate)
                for packet in sound.encode(frame):
                    container.mux(packet)
            for packet in sound.encode():
                container.mux(packet)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


class _LoRAInjectionStack:
    """Compose forward wrappers and undo them in reverse nesting order.

    The pinned core replaces the ``bypass_lora`` slot for each adapter, and
    normally ejects injection lists in forward order. Retaining one composite
    avoids dropping earlier adapters or leaving stale wrappers after offload.
    """
    def __init__(self, injections):
        self.injections = tuple(
            child for injection in injections
            for child in (injection.injections if isinstance(injection, _LoRAInjectionStack) else (injection,)))

    def inject(self, patcher):
        applied = []
        try:
            for injection in self.injections:
                applied.append(injection)
                injection.inject(patcher)
        except BaseException:
            for injection in reversed(applied):
                injection.eject(patcher)
            raise

    def eject(self, patcher):
        for injection in reversed(self.injections):
            injection.eject(patcher)


def configure_bounded_lora(bypass_hook_type, lora_type, *, max_residual_bytes=128 * 1024**2):
    """Bound plain inference LoRA residuals without splitting the base forward.

    In particular, NVFP4 activation scales must still see the complete input.
    Only the additive, row-independent LoRA path is evaluated in pieces.
    """
    import torch
    if max_residual_bytes <= 0:
        raise ValueError("LoRA residual memory limit must be positive")
    original = getattr(bypass_hook_type, "_odysseus_original_bypass_forward", bypass_hook_type._bypass_forward)
    bypass_hook_type._odysseus_original_bypass_forward = original

    def bounded_forward(hook, x, *args, **kwargs):
        adapter = hook.adapter
        if (torch.is_grad_enabled() or type(adapter) is not lora_type
                or getattr(adapter, "is_conv", False) or x.ndim != 2):
            return original(hook, x, *args, **kwargs)
        up, down, _, mid, dora, reshape = adapter.weights
        if (up.ndim != 2 or down.ndim != 2 or (mid is not None and mid.ndim != 2)
                or dora is not None or reshape is not None):
            return original(hook, x, *args, **kwargs)
        chunk_rows = max(1, max_residual_bytes // (up.shape[0] * x.element_size()))
        if x.shape[0] <= chunk_rows:
            return original(hook, x, *args, **kwargs)
        base_out = hook.original_forward(x, *args, **kwargs)
        for start in range(0, x.shape[0], chunk_rows):
            stop = start + chunk_rows
            output_slice = base_out[start:stop]
            # Keep native h(): alpha/rank, multiplier, optional mid projection,
            # dtype casts and separate multiply rounding all remain intact.
            output_slice.add_(adapter.h(x[start:stop], output_slice))
        return adapter.g(base_out)

    bypass_hook_type._bypass_forward = bounded_forward
    logging.info("Plain inference LoRA residuals limited to %d MiB per tensor; base forwards retain full inputs",
                 max_residual_bytes // 1024**2)


class H3Runtime:
    """Small adapter over the pinned inference library; imports no server/main."""
    def __init__(self, runtime_path):
        configure_cuda_allocator()
        runtime_path = Path(runtime_path)
        if not (runtime_path / "comfy_extras/nodes_minimax_h3.py").is_file():
            raise ValueError("MiniMax H3 runtime is missing; run scripts/setup_h3_runtime.py")
        sys.path.insert(0, str(runtime_path))
        import comfy.options
        comfy.options.enable_args_parsing(False)
        from comfy.cli_args import args
        import comfy.storage
        configure_fast_storage(comfy.storage)
        import comfy_aimdo.control
        # This is the pinned core's headless equivalent of main.py's allocator
        # initialization, before importing torch/model_management. No server or
        # execution engine is imported. Dynamic offload is essential for H3's
        # large transformer and vision encoder on a single consumer GPU.
        comfy_aimdo.control.init(
            simple_vram_headroom=None if args.reserve_vram is None else int(args.reserve_vram * 1024**3),
            nvml_pressure=not args.disable_nvml_pressure)
        import comfy.model_management
        import comfy.model_patcher
        import comfy.memory_management
        if not comfy.model_management.is_nvidia():
            raise ValueError("This MiniMax H3 runtime requires an NVIDIA CUDA GPU")
        if comfy.model_management.torch_version_numeric < (2, 8):
            raise ValueError("MiniMax H3 dynamic offload requires PyTorch 2.8 or newer")
        initialized = comfy_aimdo.control.init_devices(
            (device.index, int(args.vram_headroom * 1024**3))
            for device in comfy.model_management.get_all_torch_devices())
        if not initialized:
            raise RuntimeError("Could not initialize MiniMax H3 dynamic VRAM offload; check the private runtime dependencies")
        comfy_aimdo.control.set_log_info()
        comfy.model_patcher.CoreModelPatcher = comfy.model_patcher.ModelPatcherDynamic
        comfy.memory_management.aimdo_enabled = True
        import comfy.sd
        import comfy.sample
        import comfy.utils
        import comfy.model_prefetch
        import comfy_aimdo.model_vbar
        from comfy.weight_adapter.bypass import BypassForwardHook
        from comfy.weight_adapter.lora import LoRAAdapter
        configure_bounded_lora(BypassForwardHook, LoRAAdapter)
        import nodes
        from comfy_extras import nodes_audio, nodes_minimax_h3
        self.sd, self.sample, self.utils = comfy.sd, comfy.sample, comfy.utils
        self.nodes, self.audio, self.h3 = nodes, nodes_audio, nodes_minimax_h3
        self.model_management = comfy.model_management
        self.model_prefetch = comfy.model_prefetch
        self.model_vbar = comfy_aimdo.model_vbar

    def load(self, config, progress):
        import torch
        devices = visible_gpu_ids(config)
        if not torch.cuda.is_available() or torch.cuda.device_count() < len(devices):
            raise ValueError("The selected H3 GPU devices are not visible to CUDA; select their GPU UUIDs again")
        primary_device = torch.device("cuda", 0)
        vae_device = torch.device("cuda", 1 if len(devices) > 1 else 0)
        self._gpu_devices = tuple(torch.device("cuda", i) for i in range(len(devices)))
        if any("nvfp4" in Path(config[key]).name.lower() for key in ("model", "encoder")):
            if torch.cuda.get_device_capability(primary_device)[0] < 10:
                raise ValueError("NVFP4 MiniMax H3 transformer/encoder weights require a Blackwell primary GPU; choose the RTX 5090 or use other weights")
        logging.info("Transformer and Qwen encoder: %s (%s)", primary_device, torch.cuda.get_device_name(primary_device))
        logging.info("Video and audio VAEs: %s (%s); cached on that device while memory allows, with CPU offload available",
                     vae_device, torch.cuda.get_device_name(vae_device))
        progress("loading_model")
        model = self.sd.load_diffusion_model(config["model"], model_options={"load_device": primary_device})
        if type(model.model).__name__ != "MiniMaxH3":
            raise ValueError("The selected transformer is not a MiniMax H3 base model")
        if lora_stack(config):
            model = self.load_loras(model, config, progress)
        progress("loading_encoder")
        clip = self.sd.load_clip([config["encoder"]], clip_type=self.sd.CLIPType.MINIMAX,
                                 model_options={"load_device": primary_device})
        self._conditioning_patcher = clip.patcher
        progress("loading_video_vae")
        video_sd, video_metadata = self.utils.load_torch_file(config["video_vae"], safe_load=True, return_metadata=True)
        vae = self.sd.VAE(sd=video_sd, metadata=video_metadata, device=vae_device)
        vae.throw_exception_if_invalid()
        progress("loading_audio_vae")
        audio_sd, audio_metadata = self.utils.load_torch_file(config["audio_vae"], safe_load=True, return_metadata=True)
        audio_vae = self.sd.VAE(sd=audio_sd, metadata=audio_metadata, device=vae_device)
        audio_vae.throw_exception_if_invalid()
        if len(devices) > 1:
            # The pinned VAE adapter enters its own CUDA context and moves
            # inputs to self.device. CPU intermediates give both cards a safe
            # handoff without requiring peer-to-peer transfers. Keep its CPU
            # offload policy so memory pressure can still evict cached weights.
            vae.output_device = audio_vae.output_device = torch.device("cpu")
        return model, clip, vae, audio_vae

    def load_loras(self, model, config, progress):
        adapters = lora_stack(config)
        for index, adapter in enumerate(adapters, 1):
            logging.info("LoRA %d/%d: %s, strength %.3g", index, len(adapters),
                         Path(adapter["path"]).name, adapter["strength"])
            model = self.load_lora(model, {**config, "lora": adapter["path"],
                                          "lora_scale": adapter["strength"]}, progress)
        return model

    def load_lora(self, model, config, progress):
        """Keep LoRA residuals out of the NVFP4 base-weight quantization path."""
        strength = config["lora_scale"]
        if strength == 0:
            logging.info("LoRA disabled: strength is zero")
            return model
        # Layer metadata also covers renamed checkpoints. Inspecting modules
        # does not materialize tensor data or move any weights onto the GPU.
        modules = getattr(model.model, "modules", None)
        nvfp4 = (any(getattr(layer, "quant_format", None) == "nvfp4" for layer in modules())
                 if callable(modules) else False)
        nvfp4 = nvfp4 or "nvfp4" in Path(config["model"]).name.lower()
        if nvfp4:
            loader = getattr(self.sd, "load_bypass_lora_for_models", None)
            if not callable(loader):
                raise RuntimeError("The H3 runtime needs bypass LoRA support for NVFP4; update the private H3 runtime")
        else:
            loader = self.sd.load_lora_for_models
        progress("loading_adapter")
        lora = self.utils.load_torch_file(config["lora"], safe_load=True)
        try:
            from scripts.h3_lora_compat import split_curve_lora, apply_curve_lora
        except ModuleNotFoundError:
            from h3_lora_compat import split_curve_lora, apply_curve_lora
        lora, curve_adapters = split_curve_lora(model, lora)
        previous_patches = {key: len(value) for key, value in getattr(model, "patches", {}).items()}
        previous_injections = list(getattr(model, "injections", {}).get("bypass_lora", [])) if nvfp4 else []
        updated, _ = loader(model, None, lora, strength, 0)
        applied_curve = apply_curve_lora(updated, curve_adapters, strength)
        # Bypass adapters register forward-pass injections rather than weight
        # patches. The pinned core only adds this entry when it finds a hook.
        injections = getattr(updated, "injections", {}).get("bypass_lora", []) if nvfp4 else []
        added_injections = [item for item in injections if not any(item is old for old in previous_injections)]
        added_patches = any(len(value) > previous_patches.get(key, 0)
                            for key, value in getattr(updated, "patches", {}).items())
        if not added_patches and not added_injections and not applied_curve:
            raise ValueError(f"The selected LoRA {Path(config['lora']).name} has no weights compatible with this MiniMax H3 model")
        if previous_injections and added_injections:
            combined = [_LoRAInjectionStack([*previous_injections, *added_injections])]
            updated.set_injections("bypass_lora", combined)
        logging.info("LoRA applied: %s, strength %.3g, %s", Path(config["lora"]).name, strength,
                     "NVFP4 bypass (base weights unchanged)" if nvfp4 else "weight patches")
        return updated

    def log_gpu_memory(self, phase):
        devices = getattr(self, "_gpu_devices", ())
        if not devices:
            return
        import torch
        for device in devices:
            free, total = torch.cuda.mem_get_info(device)
            logging.info("GPU memory %s: %s (%s), allocated %.0f MiB, reserved %.0f MiB, free %.0f / %.0f MiB",
                         phase, device, torch.cuda.get_device_name(device),
                         torch.cuda.memory_allocated(device) / 1024**2,
                         torch.cuda.memory_reserved(device) / 1024**2,
                         free / 1024**2, total / 1024**2)

    def cleanup_model_state(self):
        if getattr(self, "model_management", None) is None:
            return
        # Match the pinned execution.py node boundary: release temporary
        # prefetch/allocator state before the next model begins. The headless
        # adapter does not run that execution engine's finally block.
        self.model_prefetch.cleanup_prefetch_queues()
        self.model_management.reset_cast_buffers()
        self.model_vbar.vbars_reset_watermark_limits()

    def release_conditioning(self):
        H3Runtime.cleanup_model_state(self)
        patcher = getattr(self, "_conditioning_patcher", None)
        if patcher is None:
            return
        # Dynamic-to-dynamic loading deliberately keeps the previous model.
        # Conditioning is complete, so unload its encoder explicitly while
        # retaining the video/audio VAEs for decode (including on cuda:1).
        self.model_management.unload_model_and_clones(patcher, unload_additional_models=False)
        self.model_management.soft_empty_cache()
        self._conditioning_patcher = None
        logging.info("Released Qwen encoder and conditioning allocation caches before sampling")

    def condition(self, config, prepared, clip, vae, audio_vae):
        common = dict(clip=clip, vae=vae, prompt=config["prompt"], width=config["width"],
                      height=config["height"], length=config["frames"])
        if is_vfx_edit(config):
            # The author's FFP branch conditions native image references before
            # adding the aligned source. The core retains minimax_refs alongside
            # minimax_keyframes; native videos/audio follow the same ref channel.
            refs = {key: prepared.get(key, {}) for key in (
                "ref_images", "ref_videos", "ref_video_audios", "ref_audios")}
            if any(refs.values()):
                positive, latent = self.h3.MiniMaxH3ReferenceToVideo.execute(
                    **common, audio_vae=audio_vae, ref_image_size=config["reference_size"], **refs)
            else:
                # No-reference branch: text conditioning and an empty AV latent.
                positive, latent = self.h3.MiniMaxH3ImageToVideo.execute(**common)
            positive = self.h3.MiniMaxH3AddGuide.execute(
                positive=positive, latent=latent, frame_idx=0, vae=vae,
                audio_vae=audio_vae, image=prepared["guide_video"], audio=prepared["guide_audio"])[0]
            return positive, latent
        if config["mode"] == "ref2va":
            return self.h3.MiniMaxH3ReferenceToVideo.execute(
                **common, audio_vae=audio_vae, ref_image_size=config["reference_size"],
                **{key: prepared[key] for key in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios")})
        return self.h3.MiniMaxH3ImageToVideo.execute(
            **common, first_frame=prepared.get("first_frame"), last_frame=prepared.get("last_frame"))

    def generate(self, config, model, positive, latent, progress):
        # Also covers BFS H3, whose dedicated guide conditioning bypasses
        # H3Runtime.condition but enters this same sampler boundary.
        H3Runtime.log_gpu_memory(self, "after conditioning")
        H3Runtime.release_conditioning(self)
        H3Runtime.log_gpu_memory(self, "after conditioning cleanup")
        model = self.h3.MiniMaxH3SigmaShift.execute(model, config["shift_video"], config["shift_audio"])[0]
        latent_image = latent["samples"]
        noise = self.sample.prepare_noise(latent_image, config["seed"])
        def callback(step, x0, x, total_steps):
            progress("sampling", step + 1)
        samples = self.sample.sample(model, noise, config["steps"], 1.0, config["sampler"],
                                     config["scheduler"], positive, [], latent_image,
                                     callback=callback, disable_pbar=False, seed=config["seed"])
        H3Runtime.cleanup_model_state(self)
        return {**latent, "samples": samples}

    def decode(self, samples, vae, audio_vae, progress, *, source_audio=None):
        progress("decoding_video")
        frames = self.nodes.VAEDecode().decode(vae, samples)[0]
        if source_audio is None:
            progress("decoding_audio")
            audio = self.audio.VAEDecodeAudio.execute(audio_vae, samples)[0]
        else:
            audio = source_audio
        self.log_gpu_memory("after video/audio decode")
        return frames, audio


def run_job(manifest, *, runtime_factory=H3Runtime, media_loader=prepare_media, muxer=mux_mp4):
    config, media = validate_job(manifest)
    os.environ["CUDA_VISIBLE_DEVICES"] = ",".join(visible_gpu_ids(config))
    progress = Progress(manifest["status_path"], config["steps"])
    progress("initializing_runtime")
    runtime = runtime_factory(manifest["runtime_path"])
    progress("preparing_media", frames=config["frames"], fps=FPS)
    prepared = media_loader(media, config)
    if is_vfx_edit(config):
        progress.details.update(frames=prepared["output_frames"], sampling_frames=config["frames"],
                                fps=FPS, width=config["width"],
                                height=config["height"], audio_mode="source")
        progress("preparing_media")
    import torch
    with torch.inference_mode():
        model, clip, vae, audio_vae = runtime.load(config, progress)
        progress("encoding_prompt")
        conditioned = runtime.condition(config, prepared, clip, vae, audio_vae)
        progress("sampling")
        samples = runtime.generate(config, model, conditioned[0], conditioned[1], progress)
        if is_vfx_edit(config):
            frames, audio = runtime.decode(samples, vae, audio_vae, progress,
                                           source_audio=prepared["source_audio"])
            if len(frames) < prepared["output_frames"]:
                raise RuntimeError("VFX Edit decoder returned fewer frames than the source video")
            frames = frames[:prepared["output_frames"]]
        else:
            frames, audio = runtime.decode(samples, vae, audio_vae, progress)
    progress("encoding_mp4")
    muxer(manifest["output_path"], frames, audio)
    if not Path(manifest["output_path"]).is_file() or Path(manifest["output_path"]).stat().st_size == 0:
        raise RuntimeError("Video encoder did not produce an MP4")
    progress("completed", config["steps"], frames=len(frames), fps=FPS, duration_seconds=len(frames) / FPS)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="[h3] %(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    args = parser.parse_args(argv)
    manifest = None
    try:
        manifest = json.loads(args.job.read_text(encoding="utf-8"))
        run_job(manifest)
        return 0
    except Exception as exc:
        # No manifest dumps: parent paths/state may contain private user data.
        error = f"{type(exc).__name__}: {exc}"
        logging.exception("H3 render failed")
        if isinstance(manifest, dict) and manifest.get("status_path"):
            atomic_json(manifest["status_path"], {"phase": "failed", "step": 0,
                                                "total_steps": manifest.get("config", {}).get("steps", 0),
                                                "error": error})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
