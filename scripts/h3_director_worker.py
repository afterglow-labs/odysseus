"""Owned-file adapter for the pinned Muse Director and Refine nodes.

Candidates run sequentially and are published independently. No Comfy HTTP
server, upstream provider route, remote media URL or user-selected code runs.
"""
from copy import deepcopy
import gc
import json
from pathlib import Path
import shutil

try:
    from scripts import h3_director_controls as controls
    from scripts import h3_video_worker as worker
except ModuleNotFoundError:
    import h3_director_controls as controls
    import h3_video_worker as worker


def timeline(config, media):
    options = config["director"]
    data = {key: deepcopy(options[key]) for key in
            ("segments", "dialogue_language", "overall_soundscape", "non_diegetic_music")}
    data["style_line"] = config["prompt"]
    for key, field in (("characters", "reference_images"), ("refVideos", "reference_videos"), ("refAudios", "reference_audio")):
        index_key = "image_index" if key == "characters" else "input_index"
        metadata = {row[index_key]: row for row in options[key]}
        background_index = options["background"]["image_index"] if options["background"] else None
        data[key] = []
        for i, path in enumerate(media[field]):
            if key == "characters" and i == background_index:
                # Keep UI character indices stable for speaker and voice pairing.
                data[key].append({})
                continue
            row = deepcopy(metadata.get(i, {}))
            row.pop(index_key, None)
            row["file"] = path
            data[key].append(row)
    if options["background"]:
        row = deepcopy(options["background"])
        row["file"] = media["reference_images"][row.pop("image_index")]
        data["background"] = row
    if config["mode"] == "fl2va":
        data["characters"] = [{"file": media[key]} if media.get(key) else {} for key in ("first_frame", "last_frame")]
        data["background"] = None
    return data


def _nodes(runtime):
    # Headless H3Runtime imports only the nodes it needs. Register the stock
    # sampler/audio classes referenced by the publisher without init_extra_nodes.
    from comfy_extras import nodes_custom_sampler, nodes_audio
    for name in ("RandomNoise", "BasicGuider", "KSamplerSelect", "BasicScheduler", "SamplerCustomAdvanced"):
        runtime.nodes.NODE_CLASS_MAPPINGS[name] = getattr(nodes_custom_sampler, name)
    runtime.nodes.NODE_CLASS_MAPPINGS["VAEDecodeAudio"] = nodes_audio.VAEDecodeAudio
    try:
        from scripts.vendor import muse_minimax_director, muse_minimax_refine
    except ModuleNotFoundError:
        from vendor import muse_minimax_director, muse_minimax_refine
    return muse_minimax_director, muse_minimax_refine


def run(manifest, config, media, *, runtime_factory, muxer):
    import torch
    from safetensors.torch import load_file, save_file

    options = config["director"]
    progress = worker.Progress(manifest["status_path"], config["steps"])
    progress("initializing_director")
    runtime = runtime_factory(manifest["runtime_path"])
    director, refine = _nodes(runtime)
    directory = Path(manifest["output_path"]).parent
    allowed = {str(Path(path).resolve()) for values in media.values()
               for path in (values if isinstance(values, list) else [values] if values else [])}

    def resolve_owned(path):
        candidate = Path(path)
        if candidate.is_symlink() or str(candidate.resolve()) not in allowed:
            raise ValueError("Director reference is outside this job's owned inputs")
        return str(candidate)

    director._resolve_path = resolve_owned
    director._resolve_resolution = refine._resolve_resolution = lambda *_: (config["width"], config["height"])
    phase = {"candidate": 1, "chunk": 0}
    for module in (director, refine):
        original = module._execute_comfy_node

        def execute(node, _original=original, **kwargs):
            name = node.__name__
            if name in {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"}:
                phase["chunk"] += 1
                progress(f"Director candidate {phase['candidate']} · chunk {phase['chunk']} · conditioning", 0)
            if name == "SamplerCustomAdvanced":
                runtime.release_conditioning()
                progress(f"Director candidate {phase['candidate']} · chunk {max(1, phase['chunk'])} · sampling", 0)
            if "clip" in kwargs:
                runtime._conditioning_patcher = kwargs["clip"].patcher
            result = _original(node, **kwargs)
            if name in {"VAEDecode", "VAEDecodeAudio"}:
                runtime.cleanup_model_state()
            return result

        module._execute_comfy_node = execute

    def hook(step, total, preview=None, *args, **kwargs):
        progress(f"Director candidate {phase['candidate']} · chunk {max(1, phase['chunk'])} · sampling", step,
                 total_steps=total)

    runtime.utils.set_progress_bar_global_hook(hook)
    common = dict(aspect_ratio=director.ASPECT_RATIO_OPTIONS[0], megapixels=1, multiple=32,
                  resize_method=options["resize_method"], ref_image_size=config["reference_size"],
                  steps=config["steps"], sampler_name=config["sampler"], scheduler=config["scheduler"],
                  shift_video=config["shift_video"], shift_audio=config["shift_audio"])
    with torch.inference_mode():
        model, clip, vae, audio_vae = runtime.load(config, progress)
        models = dict(model=model, clip=clip, vae=vae, audio_vae=audio_vae)
        if options["stage"] == "refine":
            snapshot = manifest["director_input"]
            saved = load_file(snapshot["tensors"])
            metadata = json.loads(Path(snapshot["metadata"]).read_text())
            frames, audio = refine.MuseMinimaxRefine().execute(**models, **common,
                prompt=metadata["compiled_prompt"], candidate=1, denoise=options["denoise"], seed=config["seed"],
                candidate_1_images=saved["frames"].float(),
                candidate_1_audio={"waveform": saved["waveform"], "sample_rate": metadata["sample_rate"]},
                ref_images=saved["ref_images"].float())
            progress("encoding_director_refinement")
            muxer(manifest["output_path"], frames, audio)
            progress("completed", config["steps"], frames=len(frames), fps=24)
            return

        secondary = None
        if config["mode"] == "fl2va":
            secondary = model
        elif options["hybrid_continuation"]:
            progress("loading_director_hybrid_model")
            secondary = runtime.sd.load_diffusion_model(config["model_fl2va"], model_options={"load_device": torch.device("cuda", 0)})
            if type(secondary.model).__name__ != "MiniMaxH3":
                raise ValueError("Director hybrid checkpoint must be MiniMax H3")
        data = timeline(config, media)
        rows = []
        for index in controls.candidate_indices(options):
            phase.update(candidate=index, chunk=0)
            seed = config["seed"] + (index - 1) * controls.SEED_STRIDE
            progress(f"Director candidate {index} · starting", 0)
            result = director.MuseMinimaxDirector().execute(**models, **common,
                mode=director.MODE_REFERENCE if config["mode"] == "ref2va" else director.MODE_FIRST_LAST,
                model_fl2va=secondary, duration_seconds=options["duration_seconds"],
                chunk_duration_seconds=options["chunk_duration_seconds"], hybrid_continuation=options["hybrid_continuation"],
                seed=seed, seed_hunt=False, timeline_data=json.dumps(data),
                candidate_2=False, candidate_3=False, candidate_4=False)
            frames, audio, compiled, references = result[:4]
            progress(f"Director candidate {index} · saving")
            target = directory / f"candidate-{index}.mp4"
            muxer(str(target), frames, audio)
            can_refine = len(controls.chunk_lengths(options)) == 1
            if can_refine:
                # Pixel refinement uses the original pixels/audio, not a second
                # MP4 decode. Half precision bounds snapshot size without pickle.
                save_file({"frames": frames.cpu().half().contiguous(),
                           "waveform": audio["waveform"].cpu().contiguous(),
                           "ref_images": references.cpu().half().contiguous()}, str(target.with_suffix(".safetensors")))
            worker.atomic_json(target.with_suffix(".json"), {"compiled_prompt": compiled,
                "sample_rate": audio["sample_rate"], "frames": len(frames), "seed": seed})
            rows.append({"index": index, "seed": seed, "frames": len(frames), "can_refine": can_refine})
            worker.atomic_json(directory / "director-candidates.json", {"candidates": rows})
            # A queue completion receipt requires an MP4. Candidate 1 is a preview
            # only; the API withholds the main-result URL when extra slots ran.
            if index == 1:
                shutil.copyfile(target, manifest["output_path"])
            del result, frames, audio, references
            runtime.cleanup_model_state()
            gc.collect()
    progress("completed", config["steps"], candidates=len(rows), duration_seconds=options["duration_seconds"])
