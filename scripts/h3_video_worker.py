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
RUNTIME_REVISION = "5c460d8172fe30761ff67c0df3d5643bb74e0d70"


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

    def __call__(self, phase, step=None, **extra):
        if step is not None:
            self.step = step
        atomic_json(self.path, {"phase": phase, "step": self.step,
                               "total_steps": self.total_steps,
                               "elapsed_seconds": round(time.time() - self.started, 2),
                               **extra})


def _number(config, name, default, low, high, integral=False):
    value = config.get(name, default)
    if isinstance(value, bool):
        raise ValueError(f"Invalid {name}")
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high or (integral and not value.is_integer()):
        raise ValueError(f"{name} must be {'an integer ' if integral else ''}between {low} and {high}")
    return int(value) if integral else value


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
    for name in ("model", "encoder", "video_vae", "audio_vae", "lora"):
        value = config.get(name)
        if name == "lora" and not value:
            continue
        path = Path(value or "")
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".safetensors":
            raise ValueError(f"{name} must be an existing absolute .safetensors path")
        config[name] = str(path)
    gpu = str(config.get("gpu", "0"))
    if not re.fullmatch(r"(?:\d+|GPU-[0-9a-fA-F-]{36})", gpu):
        raise ValueError("Choose one GPU index or NVIDIA GPU UUID for MiniMax H3")
    config["gpu"] = gpu
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
    for name, default, low, high in (("shift_video", 12, 0.01, 100), ("shift_audio", 3, 0.01, 100),
                                   ("lora_scale", 1, -4, 4)):
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
    for name in ("first_frame", "last_frame"):
        media[name] = uploads.get(name) or None
    total = sum(len(media[name]) for name in ("reference_images", "reference_videos", "reference_audio"))
    if total > 12:
        raise ValueError("At most 12 references are supported")
    if config["mode"] == "ref2va":
        if not media["reference_images"] and not media["reference_videos"]:
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
    prepared = {}
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
        # The core uses at most the target length, rounded down to 17k+5.
        # Apply that trim to BOTH streams so sound remains paired to the clip.
        usable = min(len(frames), config["frames"])
        usable -= (usable - 5) % 17
        prepared["ref_videos"][f"ref_video_{i}"] = frames[:usable]
        if soundtrack is not None:
            soundtrack["waveform"] = soundtrack["waveform"][..., :round(usable / FPS * AUDIO_RATE)]
            prepared["ref_video_audios"][f"ref_video_audio_{i}"] = soundtrack
    prepared["ref_audios"] = {}
    for i, path in enumerate(media["reference_audio"], 1):
        audio, duration = read_audio(path)
        audio_duration += duration
        if audio_duration > 15.05:
            raise ValueError("Reference audio, including video soundtracks, must total at most 15 seconds")
        prepared["ref_audios"][f"ref_audio_{i}"] = audio
    return prepared


def mux_mp4(output_path, frames, audio, fps=FPS):
    """Encode RGB frames and generated audio into one atomic H.264/AAC MP4."""
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


class H3Runtime:
    """Small adapter over the pinned inference library; imports no server/main."""
    def __init__(self, runtime_path):
        runtime_path = Path(runtime_path)
        if not (runtime_path / "comfy_extras/nodes_minimax_h3.py").is_file():
            raise ValueError("MiniMax H3 runtime is missing; run scripts/setup_h3_runtime.py")
        sys.path.insert(0, str(runtime_path))
        import comfy.options
        comfy.options.enable_args_parsing(False)
        from comfy.cli_args import args
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
        import nodes
        from comfy_extras import nodes_audio, nodes_minimax_h3
        self.sd, self.sample, self.utils = comfy.sd, comfy.sample, comfy.utils
        self.nodes, self.audio, self.h3 = nodes, nodes_audio, nodes_minimax_h3

    def load(self, config, progress):
        if "nvfp4" in Path(config["model"]).name.lower():
            import torch
            if not torch.cuda.is_available() or torch.cuda.get_device_capability(0)[0] < 10:
                raise ValueError("NVFP4 MiniMax H3 weights require a Blackwell GPU; choose the RTX 5090 or use other weights")
        progress("loading_model")
        model = self.sd.load_diffusion_model(config["model"])
        if type(model.model).__name__ != "MiniMaxH3":
            raise ValueError("The selected transformer is not a MiniMax H3 base model")
        if config.get("lora"):
            progress("loading_adapter")
            lora = self.utils.load_torch_file(config["lora"], safe_load=True)
            model, _ = self.sd.load_lora_for_models(model, None, lora, config["lora_scale"], 0)
            if config["lora_scale"] != 0 and not model.patches:
                raise ValueError("The selected LoRA has no weights compatible with this MiniMax H3 model")
        progress("loading_encoder")
        clip = self.sd.load_clip([config["encoder"]], clip_type=self.sd.CLIPType.MINIMAX)
        progress("loading_video_vae")
        video_sd, video_metadata = self.utils.load_torch_file(config["video_vae"], safe_load=True, return_metadata=True)
        vae = self.sd.VAE(sd=video_sd, metadata=video_metadata)
        vae.throw_exception_if_invalid()
        progress("loading_audio_vae")
        audio_sd, audio_metadata = self.utils.load_torch_file(config["audio_vae"], safe_load=True, return_metadata=True)
        audio_vae = self.sd.VAE(sd=audio_sd, metadata=audio_metadata)
        audio_vae.throw_exception_if_invalid()
        return model, clip, vae, audio_vae

    def condition(self, config, prepared, clip, vae, audio_vae):
        common = dict(clip=clip, vae=vae, prompt=config["prompt"], width=config["width"],
                      height=config["height"], length=config["frames"])
        if config["mode"] == "ref2va":
            return self.h3.MiniMaxH3ReferenceToVideo.execute(
                **common, audio_vae=audio_vae, ref_image_size=config["reference_size"],
                **{key: prepared[key] for key in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios")})
        return self.h3.MiniMaxH3ImageToVideo.execute(
            **common, first_frame=prepared.get("first_frame"), last_frame=prepared.get("last_frame"))

    def generate(self, config, model, positive, latent, progress):
        model = self.h3.MiniMaxH3SigmaShift.execute(model, config["shift_video"], config["shift_audio"])[0]
        latent_image = latent["samples"]
        noise = self.sample.prepare_noise(latent_image, config["seed"])
        def callback(step, x0, x, total_steps):
            progress("sampling", step + 1)
        samples = self.sample.sample(model, noise, config["steps"], 1.0, config["sampler"],
                                     config["scheduler"], positive, [], latent_image,
                                     callback=callback, disable_pbar=False, seed=config["seed"])
        return {**latent, "samples": samples}

    def decode(self, samples, vae, audio_vae, progress):
        progress("decoding_video")
        frames = self.nodes.VAEDecode().decode(vae, samples)[0]
        progress("decoding_audio")
        audio = self.audio.VAEDecodeAudio.execute(audio_vae, samples)[0]
        return frames, audio


def run_job(manifest, *, runtime_factory=H3Runtime, media_loader=prepare_media, muxer=mux_mp4):
    config, media = validate_job(manifest)
    os.environ["CUDA_VISIBLE_DEVICES"] = config["gpu"]
    progress = Progress(manifest["status_path"], config["steps"])
    progress("initializing_runtime")
    runtime = runtime_factory(manifest["runtime_path"])
    progress("preparing_media", frames=config["frames"], fps=FPS)
    prepared = media_loader(media, config)
    import torch
    with torch.inference_mode():
        model, clip, vae, audio_vae = runtime.load(config, progress)
        progress("encoding_prompt")
        conditioned = runtime.condition(config, prepared, clip, vae, audio_vae)
        progress("sampling")
        samples = runtime.generate(config, model, conditioned[0], conditioned[1], progress)
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
        print(error, file=sys.stderr, flush=True)
        if isinstance(manifest, dict) and manifest.get("status_path"):
            atomic_json(manifest["status_path"], {"phase": "failed", "step": 0,
                                                "total_steps": manifest.get("config", {}).get("steps", 0),
                                                "error": error})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
