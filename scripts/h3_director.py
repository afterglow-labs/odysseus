"""Native single-clip Reference-mode adaptation of Muse Director 1.2.

Source: muse-collective-26/MiniMaxH3-Director-Seed-Hunt, d07e8408f3809af7d1406a653da3fb44152b0af6.
Refinement: Muse-MiniMax-H3-Refine, e46f0aed164f037bd76dc3b28a77df04df4440b3.
Uses owned Odysseus attachments; no upstream web routes, remote image loaders,
provider credentials, hybrid checkpoint switching or stitched multi-clip jobs.
"""
import math


def shots(text):
    """Each nonempty line is one CUT, optionally prefixed by a duration weight."""
    result = []
    if not isinstance(text, str) or len(text) > 16000 or "\0" in text:
        raise ValueError("Director shots must be text of at most 16000 characters")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        weight, prompt = 1., line
        if "|" in line:
            prefix, prompt = line.split("|", 1)
            try:
                weight = float(prefix.strip())
            except ValueError as exc:
                raise ValueError("Use weight | shot description, or a description on its own") from exc
        if not math.isfinite(weight) or not 0 < weight <= 1000 or not prompt.strip():
            raise ValueError("Each Director shot needs a positive weight and a description")
        result.append((weight, prompt.strip()))
    if len(result) > 32:
        raise ValueError("Director supports at most 32 shots per clip")
    return result


def prompt(config, prepared):
    options = config["seed_hunter"]
    if options.get("preset") != "director":
        return config["prompt"]
    cuts = shots(options.get("director_shots", ""))
    style = config["prompt"] if cuts else ""
    cuts = cuts or [(1., config["prompt"])]
    definitions, retention = [], []
    # Tag numbering follows actual conditioning iteration order, including
    # paired video soundtracks before standalone audio references.
    for index, _ in enumerate(prepared.get("ref_images", {}), 1):
        definitions.append(f"<Subject {index}> is the subject shown in `<Picture {index}>`.")
        retention.append(f"<Subject {index}>: fully_preserved - identity and details from `<Picture {index}>` as directed below.")
    for index, _ in enumerate(prepared.get("ref_videos", {}), 1):
        retention.append(f"`<Video {index}>`: reference - use according to the shot instructions.")
    audio_count = len(prepared.get("ref_video_audios", {})) + len(prepared.get("ref_audios", {}))
    for index in range(1, audio_count + 1):
        definitions.append(f"<Audio {index}> is an audio reference.")
        retention.append(f"<Audio {index}>: reference - use according to the shot instructions.")
    elapsed, weight_sum, lines = 0., sum(weight for weight, _ in cuts), []
    duration = config["frames"] / 24.
    for index, (weight, description) in enumerate(cuts, 1):
        timestamp = f"{int(elapsed // 60):02d}:{elapsed % 60:06.3f}"
        lines.append(f"[Shot {index}] " + (f"At {timestamp}, " if index > 1 else "") + description)
        elapsed += duration * weight / weight_sum
    sections = [("subject_definitions", "\n".join(definitions)),
                ("summary", "[reference generation] Follow the reference and shot instructions."),
                ("retention_analysis", "\n".join(retention)),
                ("detailed_description", "\n".join(([style] if style else []) + lines)),
                ("overall_soundscape", options.get("director_soundscape") or "N/A"),
                ("non_diegetic_music", options.get("director_music") or "N/A")]
    return "\n\n".join(f"{key}:\n{value}" for key, value in sections if value)


def pixel_refinement(runtime, config, latent, vae, audio_vae, progress):
    """Decode → resize real frames → encode joint AV; preserve source waveform."""
    from comfy_extras.nodes_lt import LTXVConcatAVLatent
    progress("decoding_refinement_source", 0)
    frames, original_audio = runtime.decode(latent, vae, audio_vae, progress)
    if len(frames) < config["frames"]:
        raise ValueError("The selected Director clip is shorter than the final output")
    progress("resizing_refinement_frames", 0)
    resized = runtime.utils.common_upscale(frames[:config["frames"]].movedim(-1, 1),
        config["width"], config["height"], "bicubic", "center").movedim(1, -1)
    del frames
    progress("encoding_refinement_frames", 0)
    video = {"samples": vae.encode(resized)}
    del resized
    audio = runtime.audio.VAEEncodeAudio.execute(audio_vae, original_audio)[0]
    return LTXVConcatAVLatent.execute(video, audio)[0], original_audio
