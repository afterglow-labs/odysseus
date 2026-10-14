"""Headless Seed Hunter v2.5.1 stages over the pinned MiniMax inference core.

Preview x0 latents and their original reference conditioning are persisted as
safetensors plus a strictly typed JSON tree. No pickle, graph execution, or
dynamic module name comes from a request or an imported workflow.
"""
from __future__ import annotations

import importlib.util
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys


def _worker():
    try:
        from scripts import h3_video_worker
    except ModuleNotFoundError:
        import h3_video_worker
    return h3_video_worker


def validate_inputs(manifest, config, media, uploads):
    options = config["seed_hunter"]
    if config["mode"] != "ref2va" and options["stage"] != "scout":
        raise ValueError("Seed Hunter requires a Ref2VA base model")
    if _worker().is_vfx_edit(config):
        raise ValueError("Use the standard H3 VFX workflow for source-aligned VFX adapters")
    if config["seed"] + 1000 * (options["preview_count"] - 1) > 2**53 - 1:
        raise ValueError("Seed Hunter seeds, including candidate offsets, must fit a 53-bit integer")
    mods = uploads.get("reference_mods", [])
    if not isinstance(mods, list) or len(mods) > 8 or any(
            not isinstance(path, str) or Path(path).suffix.lower() != ".safetensors" for path in mods):
        raise ValueError("Seed Hunter accepts at most eight safetensors RefMods")
    media["reference_mods"] = mods
    if len(options["refmod_strengths"]) not in (0, len(mods)):
        raise ValueError("Each RefMod must have one strength")
    for flag, key, collection, index in (("first_frame_guide", "first_frame", "reference_images", 0),
                                          ("last_frame_guide", "last_frame", "reference_images", 1)):
        if options[flag] and not media[key] and len(media[collection]) <= index:
            raise ValueError(f"{flag.replace('_', ' ')} needs an image")
    if options["force_audio"] and not media["reference_audio"]:
        raise ValueError("Forced audio needs the first audio attachment")
    if options["stage"] == "continuation":
        if not media["source_video"]:
            raise ValueError("Continuation needs a source video")
        if options["continuation_overlap"] >= config["frames"]:
            raise ValueError("Continuation length must exceed the overlap")
    if options["stage"] == "final":
        value = config.get("latent_upscaler")
        path = Path(value or "")
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".safetensors":
            raise ValueError("Final refinement needs an installed latent upscaler")
        snapshot = manifest.get("seed_hunter_input")
        if not isinstance(snapshot, dict) or not all(isinstance(snapshot.get(key), str) and
                Path(snapshot[key]).is_absolute() and Path(snapshot[key]).is_file()
                for key in ("latent", "metadata")):
            raise ValueError("Final refinement needs an owned candidate latent snapshot")
        if Path(snapshot["latent"]).suffix != ".safetensors" or Path(snapshot["metadata"]).suffix != ".json":
            raise ValueError("Invalid candidate snapshot format")


def _encode_tree(value, tensors, depth=0):
    import torch
    if depth > 64:
        raise ValueError("Candidate conditioning is too deeply nested")
    if getattr(value, "is_nested", False):
        return {"type": "nested", "items": [_encode_tree(v, tensors, depth + 1) for v in value.unbind()]}
    if isinstance(value, torch.Tensor):
        key = f"tensor_{len(tensors)}"
        # clone removes shared storage between reference and guide tensors.
        tensors[key] = value.detach().cpu().contiguous().clone()
        return {"type": "tensor", "key": key}
    if value is None or isinstance(value, (bool, str, int)) or isinstance(value, float) and math.isfinite(value):
        return {"type": "scalar", "value": value}
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {"type": "dict", "items": {key: _encode_tree(v, tensors, depth + 1) for key, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"type": "tuple" if isinstance(value, tuple) else "list",
                "items": [_encode_tree(v, tensors, depth + 1) for v in value]}
    raise ValueError(f"Unsupported candidate conditioning value: {type(value).__name__}")


def _decode_tree(tree, tensors, nested_factory=None, depth=0):
    if depth > 64 or not isinstance(tree, dict):
        raise ValueError("Invalid candidate metadata tree")
    kind = tree.get("type")
    if kind == "tensor" and isinstance(tree.get("key"), str) and tree["key"] in tensors:
        return tensors[tree["key"]]
    if kind == "scalar":
        value = tree.get("value")
        if value is None or isinstance(value, (str, bool, int)) or isinstance(value, float) and math.isfinite(value):
            return value
    if kind == "dict" and isinstance(tree.get("items"), dict):
        return {key: _decode_tree(v, tensors, nested_factory, depth + 1) for key, v in tree["items"].items()}
    if kind in {"list", "tuple", "nested"} and isinstance(tree.get("items"), list):
        items = [_decode_tree(v, tensors, nested_factory, depth + 1) for v in tree["items"]]
        if kind == "nested":
            if nested_factory is None:
                from comfy.nested_tensor import NestedTensor
                nested_factory = NestedTensor
            return nested_factory(items)
        return tuple(items) if kind == "tuple" else items
    raise ValueError("Invalid candidate metadata tree")


def save_snapshot(path, *, latent, positive, config, seed):
    from safetensors.torch import save_file
    path = Path(path)
    tensors = {}
    tree = _encode_tree({"latent": latent, "positive": positive}, tensors)
    document = {"version": 1, "recipe": "seed_hunter", "seed": seed, "config": config, "tree": tree}
    temporary = path.with_suffix(".tmp.safetensors")
    save_file(tensors, str(temporary), metadata={"odysseus_recipe": "seed_hunter", "version": "1"})
    os.replace(temporary, path)
    _worker().atomic_json(path.with_suffix(".json"), document)


def load_snapshot(spec, config, *, nested_factory=None):
    from safetensors.torch import load_file
    metadata = Path(spec["metadata"])
    if metadata.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Candidate metadata exceeds the safe size limit")
    document = json.loads(metadata.read_text(encoding="utf-8"))
    if document.get("version") != 1 or document.get("recipe") != "seed_hunter":
        raise ValueError("Unsupported candidate snapshot")
    original = document.get("config", {})
    if config["seed_hunter"]["stage"] == "final" and original.get("seed_hunter", {}).get("stage") != "preview":
        raise ValueError("Only preview candidates can be refined")
    # LoRAs, seed and final sampling controls may change; text, reference
    # conditioning, transformer and codecs remain those of the selected result.
    for key in ("model", "encoder", "video_vae", "audio_vae", "prompt", "frames"):
        if original.get(key) != config.get(key):
            raise ValueError(f"Candidate {key} differs from the refinement job")
    if config["width"] < original["width"] or config["height"] < original["height"]:
        raise ValueError("The learned latent upscaler cannot reduce candidate dimensions")
    tensors = load_file(str(spec["latent"]), device="cpu")
    result = _decode_tree(document["tree"], tensors, nested_factory)
    if not isinstance(result, dict) or not isinstance(result.get("latent"), dict) or "positive" not in result:
        raise ValueError("Candidate snapshot is missing its latent or conditioning")
    return result["positive"], result["latent"]


def _load_module(path, name):
    path = Path(path)
    if not path.is_file():
        raise ValueError(f"Seed Hunter dependency is not installed: {path.parent.name}")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def patch_model(runtime, config, model, runtime_path=None):
    from comfy_extras.nodes_model_advanced import ModelAttentionBackend
    options = config["seed_hunter"]
    if options["ff_chunk_size"]:
        # A positive count is an explicit user choice, including 1 (no
        # splitting). KJ's object patch below owns that behavior; keep the
        # shared automatic INT8 guard from changing it underneath the patch.
        for block in model.get_model_object("diffusion_model").blocks:
            block.mlp._odysseus_manual_ff_chunks = options["ff_chunk_size"]
    if options["ff_chunk_size"] or options["low_vram_chunks"]:
        import types
        path = Path(runtime_path).parent / "seed-hunter/kjnodes/nodes/minimax_nodes.py"
        # Optional KJ progress reporting imports PromptServer; headless workers
        # deliberately provide no server and import only the mathematical nodes.
        absent = "server" not in sys.modules
        if absent:
            stub = types.ModuleType("server")
            stub.PromptServer = None
            sys.modules["server"] = stub
        try:
            optimizations = _load_module(path, "odysseus_h3_kj_optimizations")
        finally:
            if absent:
                sys.modules.pop("server", None)
        if options["ff_chunk_size"]:
            model = optimizations.MiniMaxChunkFeedForward.execute(model, options["ff_chunk_size"], options["ff_chunk_threshold"])[0]
        if options["low_vram_chunks"]:
            model = optimizations.MiniMaxLowVRAMAttention.execute(model, options["low_vram_chunks"])[0]
    backend = options["attention_backend"]
    model = ModelAttentionBackend.execute(model, "pytorch attention" if backend == "pytorch" else backend)[0]
    if options["shift_enabled"]:
        model = runtime.h3.MiniMaxH3SigmaShift.execute(model, config["shift_video"], config["shift_audio"])[0]
    if options["sparse_method"] != "none":
        from comfy_extras.nodes_sparse_attention import BlockSparseAttention
        selection = {"selection": options["sparse_method"].replace("_", "-"),
                     "keep_percent": options["sparse_keep_percent"]}
        model = BlockSparseAttention.execute(model, selection, options["sparse_start"], options["sparse_end"],
            min_tokens=options["sparse_min_tokens"], extra_tokens=options["sparse_extra_tokens"],
            sink_conditioning="off" if options["sparse_sink_conditioning"] == "none"
            else options["sparse_sink_conditioning"])[0]
    return model


def sample_candidate(runtime, config, model, positive, latent, progress):
    """Return both sampler output and processed x0, exactly as the graph does."""
    import comfy.samplers
    import comfy.nested_tensor
    from comfy_extras.nodes_custom_sampler import BasicGuider, BasicScheduler
    options = config["seed_hunter"]
    runtime.release_conditioning()
    denoise = options["denoise"] if options["stage"] == "final" else 1.
    if denoise == 0:
        return latent, latent
    sigmas = BasicScheduler.execute(model, config["scheduler"], config["steps"], denoise)[0]
    guider = BasicGuider.execute(model, positive)[0]
    x0 = []

    def callback(step, denoised, _noisy, _total):
        x0[:] = [denoised.detach().cpu() if hasattr(denoised, "detach") else denoised.cpu()]
        progress("sampling", step + 1)

    samples = guider.sample(runtime.sample.prepare_noise(latent["samples"], config["seed"]),
        latent["samples"], comfy.samplers.sampler_object(config["sampler"]), sigmas,
        denoise_mask=latent.get("noise_mask"), callback=callback, disable_pbar=False, seed=config["seed"])
    samples = samples.to(runtime.model_management.intermediate_device())
    output = {key: value for key, value in latent.items() if key not in {"downscale_ratio_spacial", "downscale_ratio_temporal"}}
    output["samples"] = samples
    denoised = output
    if x0:
        value = x0[0]
        if samples.is_nested and not value.is_nested:
            value = comfy.nested_tensor.NestedTensor(runtime.utils.unpack_latents(value, [v.shape for v in samples.unbind()]))
        denoised = {**latent, "samples": guider.model_patcher.model.process_latent_out(value.cpu())}
    runtime.cleanup_model_state()
    return output, denoised


def upscale_latent(manifest, config, latent, progress):
    import torch
    import folder_paths
    from comfy_extras.nodes_lt import LTXVSeparateAVLatent, LTXVConcatAVLatent
    source = Path(manifest["runtime_path"]).parent / "seed-hunter/latent-upscaler/nodes/minimax_h3_latent_upscaler_3d.py"
    module = _load_module(source, "odysseus_h3_latent_upscaler_3d")
    path = Path(config["latent_upscaler"])
    # A worker owns this registry. A single exact directory prevents same-name
    # upscalers in other cache roots from winning lookup order.
    folder_paths.folder_names_and_paths["latent_upscale_models"] = ([str(path.parent)], {".safetensors"})
    video, audio = LTXVSeparateAVLatent.execute(latent)
    progress("upscaling_latent")
    options = config["seed_hunter"]
    with torch.cuda.device(0):
        result = module.MinimaxH3LatentUpscaler3D.execute(video, path.name,
            mode={"mode": "target dimensions", "width": config["width"], "height": config["height"]},
            align=32, enable_temporal_chunking=options["upscaler_chunking"],
            force_unload=options["upscaler_force_unload"], device="cuda", precision="fp16")[0]
    updated = {**video, **result}
    if "noise_mask" in video and video["noise_mask"] is not None:
        mask = video["noise_mask"]
        updated["noise_mask"] = torch.nn.functional.interpolate(mask.float(), size=result["samples"].shape[-3:], mode="nearest")
    # The source graph preserves audio through the upscale, then jointly
    # denoises video and audio. A forced-audio mask must survive this boundary.
    return LTXVConcatAVLatent.execute(updated, audio)[0]


def condition(runtime, config, prepared, clip, vae, audio_vae):
    options = config["seed_hunter"]
    if options["stage"] == "scout" and config["mode"] != "ref2va":
        positive, latent = runtime.condition(config, prepared, clip, vae, audio_vae)
        return positive, latent, positive
    positive, latent = runtime.h3.MiniMaxH3ReferenceToVideo.execute(clip=clip, vae=vae, audio_vae=audio_vae,
        prompt=config["prompt"], width=config["width"], height=config["height"], length=config["frames"],
        ref_image_size=config["reference_size"],
        **{key: prepared.get(key, {}) for key in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios")})
    if prepared.get("refmod_blocks"):
        positive = [[embedding, {**metadata, "minimax_refs": [*metadata.get("minimax_refs", []), *prepared["refmod_blocks"]]}]
                    for embedding, metadata in positive]
    base_positive = positive
    for flag, key, fallback, frame in (("first_frame_guide", "first_frame", "ref_image_1", 0),
                                        ("last_frame_guide", "last_frame", "ref_image_2", -1)):
        if options[flag]:
            image = prepared.get(key)
            if image is None:
                image = prepared.get("ref_images", {}).get(fallback)
            positive = runtime.h3.MiniMaxH3AddGuide.execute(positive=positive, latent=latent,
                frame_idx=frame, vae=vae, audio_vae=audio_vae, image=image)[0]
    if "continuation_guide" in prepared:
        positive = runtime.h3.MiniMaxH3AddGuide.execute(positive=positive, latent=latent,
            frame_idx=0, vae=vae, audio_vae=audio_vae, image=prepared["continuation_guide"],
            audio=prepared.get("continuation_audio"))[0]
    if options["force_audio"]:
        import torch
        from comfy_extras.nodes_lt import LTXVConcatAVLatent
        audio = runtime.audio.VAEEncodeAudio.execute(audio_vae, prepared["ref_audios"]["ref_audio_1"])[0]
        audio = {**audio, "noise_mask": torch.zeros_like(audio["samples"])}
        latent = LTXVConcatAVLatent.execute(latent, audio)[0]
    return positive, latent, base_positive


def _check_optional(manifest, config, media):
    options = config["seed_hunter"]
    if options["stage"] in {"continuation", "dialogue"} or options["output_fps"] == 48 or options["audio_start_seconds"] or options["audio_end_seconds"] is not None:
        ffmpeg_binary()
    dependencies = Path(manifest["runtime_path"]).parent / "seed-hunter"
    if (options["ff_chunk_size"] or options["low_vram_chunks"]) and not (dependencies / "kjnodes/nodes/minimax_nodes.py").is_file():
        raise ValueError("Seed Hunter feed-forward/low-VRAM optimization nodes are not installed")
    if options["output_fps"] != 24 and not rife_path(manifest).is_file():
        raise ValueError("48 fps requires the optional RIFE interpolation runtime")
    if media["reference_mods"] and not (dependencies / "refmods/core.py").is_file():
        raise ValueError("Encoded RefMods require the optional MiniMaxH3Mod runtime")


def _publish_candidates(path, candidates):
    _worker().atomic_json(path, {"version": 1, "candidates": candidates})


def run(manifest, config, media, *, runtime_factory, media_loader, muxer):
    worker = _worker()
    options = config["seed_hunter"]
    _check_optional(manifest, config, media)
    folder = Path(manifest["output_path"]).parent
    folder.mkdir(parents=True, exist_ok=True)
    progress = worker.Progress(manifest["status_path"], config["steps"])
    progress.details.update(recipe="seed_hunter", stage=options["stage"], candidate_count=options["preview_count"])
    progress("initializing_runtime")
    runtime = runtime_factory(manifest["runtime_path"])
    progress("preparing_media")
    prepared = prepare_media(manifest, config, media, media_loader, folder)
    import torch
    candidates = []
    with torch.inference_mode():
        model, clip, vae, audio_vae = runtime.load(config, progress)
        model = patch_model(runtime, config, model, manifest["runtime_path"])
        if options["stage"] == "final":
            positive, latent = load_snapshot(manifest["seed_hunter_input"], config)
            runtime.release_conditioning()
            latent = upscale_latent(manifest, config, latent, progress)
            base_positive = positive
        else:
            progress("encoding_prompt")
            positive, latent, base_positive = condition(runtime, config, prepared, clip, vae, audio_vae)
        for index in range(1, options["preview_count"] + 1):
            candidate_config = {**config, "seed": config["seed"] + (index - 1) * 1000}
            progress.details.update(candidate=index, seed=candidate_config["seed"])
            progress("sampling", 0)
            row = {"id": index, "seed": candidate_config["seed"], "status": "running", "latent_ready": False}
            candidates.append(row)
            _publish_candidates(folder / "candidates.json", candidates)
            try:
                if options["stage"] == "scout":
                    from scripts.h3_seed_scout import sample as scout_sample
                    output, denoised, selected_seed = scout_sample(runtime, manifest, candidate_config,
                        model, positive, latent, vae, progress)
                    row["seed"] = candidate_config["seed"] = selected_seed
                else:
                    output, denoised = sample_candidate(runtime, candidate_config, model, positive, latent, progress)
                latent_name = f"candidate-{index}.safetensors"
                save_snapshot(folder / latent_name, latent=denoised, positive=base_positive,
                              config=config, seed=candidate_config["seed"])
                row.update(latent=latent_name, metadata=f"candidate-{index}.json", latent_ready=True)
                # Preview uses sampler output, but single pass and refinement
                # decode x0, as the selected-latent branch in the source graph.
                decode_samples = output if options["stage"] in {"preview", "dialogue"} else denoised
                if options["stage"] == "dialogue":
                    progress("decoding_audio")
                    audio = runtime.audio.VAEDecodeAudio.execute(audio_vae, decode_samples)[0]
                    # Audio-only output has a small blank compatibility video;
                    # FLAC is the primary downloadable candidate artifact.
                    frames = torch.zeros((config["frames"], 64, 64, 3), dtype=torch.float32)
                else:
                    frames, audio = runtime.decode(decode_samples, vae, audio_vae, progress)
                frames = frames[:config["frames"]]
                if options["stage"] == "continuation":
                    frames = blend_continuation(prepared["continuation_guide"], frames,
                                                options["continuation_overlap"])
                progress("encoding_mp4")
                video_name = f"candidate-{index}.mp4"
                muxer(str(folder / video_name), frames, audio)
                if options["stage"] == "continuation":
                    join_continuation(prepared, folder / video_name)
                output_frames = len(frames) + prepared.get("continuation_keep", 0)
                if options["output_fps"] == 48:
                    interpolate_file(manifest, folder / video_name, progress)
                output_fps = options["output_fps"]
                output_frames *= output_fps // 24
                row.update(video=video_name, status="completed", frames=output_frames, fps=output_fps,
                           duration_seconds=output_frames / output_fps)
                if options["stage"] == "dialogue":
                    audio_name = f"candidate-{index}.flac"
                    save_flac(folder / audio_name, audio)
                    row["audio"] = audio_name
                _publish_candidates(folder / "candidates.json", candidates)
                if index == 1:
                    destination = Path(manifest["output_path"])
                    source = folder / video_name
                    if destination.exists():
                        destination.unlink()
                    try:
                        os.link(source, destination)
                    except OSError:
                        shutil.copyfile(source, destination)
                del output, denoised, decode_samples, frames, audio
                runtime.cleanup_model_state()
            except Exception as error:
                row.update(status="failed", error=str(error)[:2000])
                _publish_candidates(folder / "candidates.json", candidates)
                raise
    progress("completed", config["steps"], frames=output_frames, fps=output_fps,
             duration_seconds=output_frames / output_fps, candidates=len(candidates))


def prepare_media(manifest, config, media, media_loader, folder):
    options = config["seed_hunter"]
    if options["stage"] == "final":
        return {}
    # The dialogue branch uses only its voice reference, independent of the
    # image/video references in the visual draft. The first audio can be trimmed
    # before decoding; retaining numbering avoids moving Audio 2 into slot 1.
    custom_audio = options["stage"] == "dialogue" or options["audio_start_seconds"] != 0 or options["audio_end_seconds"] is not None
    load_media = media
    if custom_audio:
        load_media = {**media, "reference_audio": []}
    if options["stage"] == "dialogue":
        load_media = {**load_media, "reference_images": [], "reference_videos": [], "first_frame": None, "last_frame": None}
    prepared = media_loader(load_media, config)
    if custom_audio and media["reference_audio"]:
        end = options["audio_end_seconds"]
        maximum = 45 if options["stage"] == "dialogue" else 15
        duration = maximum if end is None else end - options["audio_start_seconds"]
        if duration > maximum:
            raise ValueError(f"This audio reference must be at most {maximum} seconds")
        prepared["ref_audios"] = {"ref_audio_1": read_trimmed_audio(media["reference_audio"][0],
                options["audio_start_seconds"], duration)}
        if options["stage"] != "dialogue":
            for index, path in enumerate(media["reference_audio"][1:], 2):
                prepared["ref_audios"][f"ref_audio_{index}"] = _worker().read_audio(path)[0]
    if options["stage"] == "continuation":
        prepared.update(prepare_continuation(media["source_video"], config, folder))
    if media["reference_mods"]:
        prepared["refmod_blocks"] = load_refmods(manifest["runtime_path"], media["reference_mods"], options["refmod_strengths"])
    return prepared


def load_refmods(runtime_path, paths, strengths):
    """Use the pinned RefMod latent transform without importing its HTTP UI."""
    from safetensors import safe_open
    core = _load_module(Path(runtime_path).parent / "seed-hunter/refmods/core.py", "odysseus_h3_refmod_core")
    blocks = []
    for index, path in enumerate(paths):
        strength = min(1., max(0., strengths[index] if strengths else 1.))
        if not strength:
            continue
        with safe_open(path, framework="pt", device="cpu") as source:
            headers = source.metadata() or {}
            metadata = json.loads(headers.get(core.META_KEY, headers.get("audio_refmod_meta", "null")))
            if not isinstance(metadata, dict):
                raise ValueError("This safetensors file is not an encoded MiniMax H3 RefMod")
            if metadata.get("kind") == "bundle":
                members = metadata.get("members")
                if metadata.get("_format_version") != 5 or not isinstance(members, list) or not 1 <= len(members) <= 256:
                    raise ValueError("Invalid RefMod bundle")
                entries = [(f"ref_{i}", meta) for i, meta in enumerate(members)]
            else:
                entries = [("latent", metadata)]
            for key, meta in entries:
                if not isinstance(meta, dict) or key not in source.keys():
                    raise ValueError("Invalid RefMod member")
                shape = source.get_slice(key).get_shape()
                audio = meta.get("kind") == "audio"
                valid = (len(shape) == 4 and shape[:3] == [1, 32, 2] and shape[3] > 0) if audio else (
                    meta.get("kind") in {"image", "video"} and len(shape) == 5 and shape[:2] == [1, 24]
                    and all(n > 0 for n in shape[2:]) and all(n % 2 == 0 for n in shape[3:])
                    and (meta["kind"] != "image" or shape[2] == 1))
                if not valid or math.prod(shape) > 128 * 1024 * 1024:
                    raise ValueError("Invalid or oversized RefMod latent layout")
                latent = source.get_tensor(key).clone()
                normalized = {**meta, "latent_h": 0 if audio else shape[3],
                              "latent_w": 0 if audio else shape[4], "latent_t": shape[-1] if audio else shape[2]}
                mod = core.H3RefMod.from_metadata(normalized, latent)
                block = mod.ref_block(strength, curve=("constant", "linear", 1.))
                if block is not None:
                    blocks.append({**block, "refmod": True})
    return blocks


def read_trimmed_audio(path, start, duration):
    import numpy as np
    import torch
    result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(start),
        "-i", str(path), "-t", str(duration), "-vn", "-ar", "32000", "-ac", "2", "-f", "f32le", "pipe:1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode or not result.stdout:
        raise ValueError("Could not decode the selected audio reference range")
    samples = np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2).T.copy()
    return {"waveform": torch.from_numpy(samples).unsqueeze(0), "sample_rate": 32000}


def ffmpeg_binary():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as error:
        raise ValueError("Seed Hunter media processing needs imageio-ffmpeg in the private Python environment") from error


def _ffmpeg(args):
    result = subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ValueError("Video processing failed: " + result.stderr.decode("utf-8", errors="replace")[-2000:])


def prepare_continuation(path, config, folder):
    """Transcode to disk; decode only the tail, not an unbounded source tensor."""
    import av
    import numpy as np
    import torch
    options = config["seed_hunter"]
    normalized = folder / "continuation-source.mp4"
    args = ["-ss", str(options["source_start_seconds"]), "-i", str(path)]
    if options["source_end_seconds"] is not None:
        args += ["-t", str(options["source_end_seconds"] - options["source_start_seconds"])]
    w, h = config["width"], config["height"]
    args += ["-vf", f"fps=24,scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
             "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             "-c:a", "aac", "-ar", "32000", "-ac", "2", str(normalized)]
    _ffmpeg(args)
    with av.open(str(normalized)) as source:
        count = source.streams.video[0].frames
        has_audio = bool(source.streams.audio)
    overlap = options["continuation_overlap"]
    if count < overlap:
        raise ValueError("Continuation source is shorter than its overlap")
    tail_path = folder / "continuation-tail.mp4"
    keep = count - overlap
    _ffmpeg(["-i", str(normalized), "-ss", str(keep / 24), "-frames:v", str(overlap),
             "-c:v", "libx264", "-c:a", "aac", str(tail_path)])
    with av.open(str(tail_path)) as source:
        arrays = [frame.to_ndarray(format="rgb24") for frame in source.decode(video=0)]
    if len(arrays) != overlap:
        raise ValueError("Could not decode the complete continuation overlap")
    guide = torch.from_numpy(np.stack(arrays)).float().div_(255)
    audio, _ = _worker().read_audio(tail_path, optional=True, check_minimum=False)
    return {"continuation_guide": guide, "continuation_audio": audio,
            "continuation_source": str(normalized), "continuation_keep": keep,
            "continuation_has_audio": has_audio}


def blend_continuation(source_tail, generated, overlap):
    import torch
    if len(generated) < overlap or len(source_tail) != overlap:
        raise ValueError("Continuation output does not contain its complete overlap")
    # The supplied graph's ImageBlend uses a constant 0.5, not a temporal fade.
    return torch.cat((source_tail.to(generated) * .5 + generated[:overlap] * .5, generated[overlap:]), dim=0)


def join_continuation(prepared, video):
    keep = prepared["continuation_keep"]
    if keep == 0:
        return
    temporary = video.with_name(video.stem + "-joined.mp4")
    inputs = ["-i", prepared["continuation_source"], "-i", str(video)]
    if prepared["continuation_has_audio"]:
        audio = f"[0:a]atrim=end={keep / 24},asetpts=PTS-STARTPTS[a];"
    else:
        inputs += ["-f", "lavfi", "-t", str(keep / 24), "-i", "anullsrc=r=32000:cl=stereo"]
        audio = "[2:a]asetpts=PTS-STARTPTS[a];"
    graph = (f"[0:v]trim=end_frame={keep},setpts=PTS-STARTPTS[v];" + audio +
             "[v][a][1:v][1:a]concat=n=2:v=1:a=1[outv][outa]")
    _ffmpeg([*inputs, "-filter_complex", graph, "-map", "[outv]", "-map", "[outa]", "-r", "24",
             "-c:v", "libx264", "-crf", "18", "-c:a", "aac", "-movflags", "+faststart", str(temporary)])
    os.replace(temporary, video)


def save_flac(path, audio):
    import av
    import numpy as np
    waveform = audio["waveform"].detach().float().cpu().numpy()[0]
    rate = int(audio["sample_rate"])
    if not np.isfinite(waveform).all():
        raise FloatingPointError("Audio decoder produced non-finite samples")
    with av.open(str(path), "w") as output:
        stream = output.add_stream("flac", rate=rate)
        stream.layout = "stereo" if waveform.shape[0] == 2 else "mono"
        resampler = av.AudioResampler(format="s16", layout=stream.layout.name, rate=rate)
        frame = av.AudioFrame.from_ndarray(waveform, format="fltp", layout=stream.layout.name)
        frame.sample_rate = rate
        for converted in resampler.resample(frame) + resampler.resample(None):
            for packet in stream.encode(converted):
                output.mux(packet)
        for packet in stream.encode(None):
            output.mux(packet)


def rife_path(manifest):
    return Path(manifest["runtime_path"]).parents[2] / "models/hzwer/RIFE/rife-v4.26.safetensors"


def interpolate_file(manifest, path, progress):
    """Learned RIFE interpolation in bounded chunks, including continuation prefixes."""
    import av
    import torch
    import numpy as np
    from fractions import Fraction
    import folder_paths
    from comfy_extras.nodes_frame_interpolation import FrameInterpolationModelLoader, FrameInterpolate
    weights = rife_path(manifest)
    folder_paths.folder_names_and_paths["frame_interpolation"] = ([str(weights.parent)], {".safetensors"})
    model = FrameInterpolationModelLoader.execute(weights.name)[0]
    temporary = path.with_name(path.stem + "-48fps-video.mp4")
    remuxed = path.with_name(path.stem + "-48fps.mp4")
    progress("interpolating_frames")
    with av.open(str(path)) as source, av.open(str(temporary), "w") as output:
        stream = output.add_stream("libx264", rate=48)
        stream.width, stream.height = source.streams.video[0].width, source.streams.video[0].height
        stream.pix_fmt = "yuv420p"
        stream.options = {"crf": "18", "preset": "medium"}
        emitted = 0
        buffered = []

        def emit(images):
            nonlocal emitted
            for image in images:
                pixels = image.detach().float().cpu().numpy()
                pixels = np.rint(np.clip(pixels, 0, 1) * 255).astype(np.uint8)
                frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
                frame.pts, frame.time_base = emitted, Fraction(1, 48)
                emitted += 1
                for packet in stream.encode(frame):
                    output.mux(packet)

        def flush():
            images = torch.stack(buffered)
            result = FrameInterpolate.execute(model, images, 2)[0]
            emit(result[:-1])
            buffered[:] = buffered[-1:]

        for frame in source.decode(video=0):
            buffered.append(torch.from_numpy(frame.to_ndarray(format="rgb24")).float().div_(255))
            if len(buffered) == 8:
                flush()
                progress("interpolating_frames", interpolated_frames=emitted)
        if len(buffered) > 1:
            flush()
        if buffered:
            # Interpolation defines midpoints through the final source frame;
            # one repeated terminal frame preserves the original N/24 duration.
            emit([buffered[0], buffered[0]])
        for packet in stream.encode(None):
            output.mux(packet)
    _ffmpeg(["-i", str(temporary), "-i", str(path), "-map", "0:v:0", "-map", "1:a:0?",
             "-c", "copy", "-t", str(emitted / 48), "-movflags", "+faststart", str(remuxed)])
    os.replace(remuxed, path)
    temporary.unlink(missing_ok=True)
