"""Run pinned H3MD nodes headlessly with job-owned media and segment caches."""
from copy import deepcopy
import hashlib
import importlib
import json
from pathlib import Path
import sys
import types

try:
    from scripts import h3md_controls as controls, h3_video_worker as worker
except ModuleNotFoundError:
    import h3md_controls as controls
    import h3_video_worker as worker


def load_modules(runtime_path):
    root = Path(runtime_path).parent / "h3md"
    if not (root / "h3md_loop.py").is_file():
        raise ValueError("H3MD runtime is missing; run scripts/setup_h3md.py")
    # Load inference modules only, never upstream's HTTP routes or editor.
    package = types.ModuleType("odysseus_h3md")
    package.__path__ = [str(root)]
    sys.modules[package.__name__] = package
    return tuple(importlib.import_module(package.__name__ + "." + name) for name in
                 ("h3md_director", "h3md_loop", "h3md_stream_save", "h3md_lora", "h3md_cache"))


def make_plan(config, media):
    """Stable media keys survive owned-file cloning without trusting supplied paths."""
    plan = {"version": 1, "fps": 24, "width": config["width"], "height": config["height"],
            "globalPrompt": config["prompt"], "refVideoHeight": "auto", "media": {}, "segments": []}
    paths = {}
    for kind, field in (("images", "reference_images"), ("videos", "reference_videos"), ("audios", "reference_audio")):
        plan["media"][kind] = []
        for index, path in enumerate(media.get(field, [])):
            identity = f"{kind}-{index}"
            paths[identity] = path
            plan["media"][kind].append({"id": identity, "file": identity})
    for shot in config[controls.RECIPE]["shots"]:
        # An empty selection means all attached references of that type.
        images = shot.get("images") or list(range(len(media.get("reference_images", []))))
        videos = shot.get("videos") or [{"index": i} for i in range(len(media.get("reference_videos", [])))]
        audios = shot.get("audios") or [{"index": i} for i in range(len(media.get("reference_audio", [])))]
        plan["segments"].append({"length": shot["frames"], "overlapIn": shot["overlap"],
            "prompt": shot.get("prompt", ""), "images": [f"images-{i}" for i in images],
            "videos": [{"id": f"videos-{v['index']}", "trimStart": v.get("offset", 0),
                        "useSoundtrack": v.get("soundtrack", False)} for v in videos],
            "audios": [{"id": f"audios-{a['index']}", "offset": a.get("offset", 0),
                        "mode": a.get("mode", "reference")} for a in audios], "audioMode": "reference"})
    return plan, paths


def detach_segment_files(cache, index):
    """Copy-on-write for reruns: upstream writers may truncate an old segment."""
    for suffix in ("_frames.npy", "_audio.pt", "_latent.pt", "_lowres.pt"):
        path = Path(cache._p(index, suffix))
        if path.exists() and path.stat().st_nlink > 1:
            path.unlink()


def run(manifest, config, media, *, runtime_factory, muxer=None):
    import torch
    progress = worker.Progress(manifest["status_path"], config["steps"])
    progress("initializing_h3md")
    runtime = runtime_factory(manifest["runtime_path"])
    director, loop, save, lora, cache = load_modules(manifest["runtime_path"])
    import folder_paths
    from comfy_extras.nodes_custom_sampler import BasicScheduler, KSamplerSelect
    options = config[controls.RECIPE]
    directory = Path(manifest["output_path"]).parent
    raw_plan, paths = make_plan(config, media)
    allowed = {key: str(Path(path).resolve()) for key, path in paths.items()}
    def resolve_media(key):
        path = paths.get(key)
        if not path or Path(path).is_symlink() or str(Path(path).resolve()) != allowed[key]:
            raise ValueError("H3MD reference is outside this job's owned inputs")
        return path
    director.resolve_media_path = resolve_media
    # The upstream cache uses filenames/mtime. Hash owned input contents instead,
    # so a safe copy into a rerun remains reusable, and changed media invalidates it.
    signatures = {}
    def signature(key):
        if key not in signatures:
            with Path(resolve_media(key)).open("rb") as stream:
                signatures[key] = [key, hashlib.file_digest(stream, "sha256").hexdigest()]
        return signatures[key]
    cache._file_sig = signature
    original_write = cache.SegmentCache.write
    def write_segment(instance, index, *args, **kwargs):
        detach_segment_files(instance, index)
        return original_write(instance, index, *args, **kwargs)
    cache.SegmentCache.write = write_segment
    # Upstream does not include model identity in its cache key.
    split_settings = cache.split_settings
    identity = {key: config.get(key) for key in ("model", "encoder", "video_vae", "audio_vae", "latent_upscaler", "loras")}
    identity["high_res_strengths"] = options["high_res_strengths"]
    cache.split_settings = lambda settings: split_settings({**settings, "odysseus_models": identity,
                                                           "odysseus_h3md_revision": controls.REVISION})
    upscaler = Path(config["latent_upscaler"])
    folder_paths.folder_names_and_paths["latent_upscale_models"] = ([str(upscaler.parent)], {".safetensors"})
    phase = {"segment": 0, "count": 0, "stage": "sampling"}
    original_timed = loop.S.timed_sampler
    def timed_sampler(sampler, stage, total, log=None):
        result = original_timed(sampler, stage, total, log)
        original = result.sampler_function
        def execute(*args, **kwargs):
            phase["stage"] = "first pass · low resolution" if stage == "low_res" else "final pass · full resolution"
            progress(f"H3MD segment {phase['segment']}/{phase['count']} · {phase['stage']}", 0, total_steps=total)
            return original(*args, **kwargs)
        result.sampler_function = execute
        return result
    loop.S.timed_sampler = timed_sampler
    original_encode = director.H3MDSegmentEncode.encode
    def encode(instance, *args, **kwargs):
        phase["segment"] = kwargs.get("segment_index", args[1] if len(args) > 1 else 0) + 1
        progress(f"H3MD segment {phase['segment']}/{phase['count']} · encoding", 0)
        return original_encode(instance, *args, **kwargs)
    director.H3MDSegmentEncode.encode = encode
    original_sample = loop.S.H3MDSampler.sample
    def sample(instance, *args, **kwargs):
        runtime.release_conditioning()
        progress(f"H3MD segment {phase['segment']}/{phase['count']} · two-pass sampling", 0)
        return original_sample(instance, *args, **kwargs)
    loop.S.H3MDSampler.sample = sample
    runtime.utils.set_progress_bar_global_hook(lambda step, total, *args, **kwargs:
        progress(f"H3MD segment {phase['segment']}/{phase['count']} · {phase['stage']}", step, total_steps=total))
    with torch.inference_mode():
        # Both stages share one checkpoint; adapters remain independent clones.
        model, clip, vae, audio_vae = runtime.load({**config, "loras": [], "lora": None, "lora_scale": 1}, progress)
        low, high = model, model
        for index, adapter in enumerate(config["loras"]):
            path = Path(adapter["path"])
            state = runtime.utils.load_torch_file(str(path), safe_load=True)
            low_strength, high_strength = adapter["strength"], options["high_res_strengths"][index]
            # The source uses its bounded bypass loader for DMAD, LightX2V and
            # Dasiwa; ordinary adapters use the existing bounded native loader.
            if path.name in {controls.LORAS[i][0] for i in (0, 1, 3)}:
                if low_strength:
                    low = lora.apply_lowvram_bypass(low, state, low_strength, "replace_like_comfy", 8192)[0]
                if high_strength:
                    high = lora.apply_lowvram_bypass(high, state, high_strength, "replace_like_comfy", 8192)[0]
            else:
                if low_strength:
                    low = runtime.load_lora(low, {**config, "lora": str(path), "lora_scale": low_strength}, progress)
                if high_strength:
                    high = runtime.load_lora(high, {**config, "lora": str(path), "lora_scale": high_strength}, progress)
            del state
        plan, count, total_frames, summary, _ = director.H3MDDirector().build(raw_plan)
        phase["count"] = count
        worker.atomic_json(directory / "h3md-plan.json", {"segments": count, "frames": total_frames, "summary": summary})
        sigmas = BasicScheduler().get_sigmas(low, config["scheduler"], config["steps"], 1.)[0]
        sampler = KSamplerSelect().get_sampler(config["sampler"])[0]
        # Each job owns its cache root. Always use stable upstream resume names;
        # the API copies an earlier cache only for explicit Resume/Edit & rerun.
        result = loop.H3MDLoopSampler().run(plan=plan, clip=clip, vae=vae, audio_vae=audio_vae,
            low_res_model=low, high_res_model=high, sampler=sampler, sigmas=sigmas, seed=config["seed"],
            cfg=options["cfg"], transition_step=options["transition_step"], lowres_scale=options["lowres_scale"],
            rho=options["rho"], w_min=options["w_min"], w_max=options["w_max"], upscaler_model=upscaler.name,
            highres_tiling=options["highres_tiling"], ff_chunk_rows=4096,
            ref_image_size=config["reference_size"], continue_audio=options["continue_audio"],
            drift_strength=options["drift_strength"], drift_lock_tokens=options["drift_lock_tokens"],
            seed_mode=options["seed_mode"], audio_crossfade_ms=options["audio_crossfade_ms"],
            rerun_segments=options["rerun_segments"], stop_after=options["stop_after"],
            rerun_new_seed=options["rerun_new_seed"], cache_mode="disk", cache_dtype="fp16",
            cache_dir=str(directory / "h3md-cache"), resume_from_cache=True,
            continue_lowres_prefix="native_lowres_carry", lowres_anchor=options["lowres_anchor"],
            seam_decode_context=2, vram_release="deep", slow_step_guard="stop")
        progress("encoding_h3md_video")
        save.stream_save(result[5], manifest["output_path"], interpolate=False, target_fps=24, crf=options["crf"])
        worker.atomic_json(directory / "h3md-result.json", {"frames": result[3], "summary": result[4], "segments": count})
        progress("completed", config["steps"], frames=result[3], fps=24, segments=count)
