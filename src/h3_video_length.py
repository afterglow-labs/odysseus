"""Choose each batch clip's H3 length before publishing its queue receipt."""
import json
import math
from pathlib import Path
import subprocess
import sys

from src.h3_vfx import is_vfx_config

FPS = 24
MAX_SOURCE_SECONDS = 15.05


def _probe_video(path, *, exact_frames=False):
    """Read only video packet timestamps, without decoding pixels or audio.

    Container duration can include a longer soundtrack. Presentation timestamps
    also handle variable-rate clips and nonzero stream starts. Use the same
    last-frame duration/rate fallback as the renderer's video reader.
    """
    import av

    with av.open(str(path), options={"protocol_whitelist": "file,pipe"}) as source:
        if not source.streams.video:
            raise ValueError("The file contains no video track")
        stream = source.streams.video[0]
        # Even stream.duration can omit a fraction of the final VFR frame.
        # Inspect packet endpoints in every mode so a boundary clip cannot be
        # assigned the preceding length. This never decodes image pixels.
        rate = float(stream.average_rate or FPS)
        if not math.isfinite(rate) or rate <= 0:
            rate = FPS
        first = last = None
        last_duration = 1 / rate
        for packet in source.demux(stream):
            if not packet.size or packet.pts is None or packet.time_base is None:
                continue
            timestamp = float(packet.pts * packet.time_base)
            if not math.isfinite(timestamp):
                continue
            first = timestamp if first is None else min(first, timestamp)
            if last is None or timestamp >= last:
                last = timestamp
                last_duration = float(packet.duration * packet.time_base) if packet.duration else 1 / rate
        if first is not None and last is not None:
            last_timestamp = last - first
            duration = last_timestamp + last_duration
            # read_video emits a frame at every 24fps timestamp through the
            # final source timestamp, then fills to round(duration * FPS).
            source_frames = max(math.floor(last_timestamp * FPS + 1e-8) + 1, round(duration * FPS))
        elif stream.duration is not None and stream.time_base is not None:
            duration = float(stream.duration * stream.time_base)
            source_frames = round(duration * FPS)
        else:
            raise ValueError("The video track has no readable duration")
        if not math.isfinite(duration) or duration <= 0 or source_frames <= 0:
            raise ValueError("The video track has no readable duration")
        return {"duration_seconds": duration, "source_frames": min(source_frames, 15 * FPS)}


def probe_video(path, *, exact_frames=False):
    """Bound malformed/slow media probing and use Odysseus's private Python."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.h3_video_length", "--probe-frames" if exact_frames else "--probe",
             str(Path(path).resolve())],
            cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
            timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Could not detect this video's duration within 30 seconds; check the file or turn off automatic batch length") from exc
    except OSError as exc:
        raise ValueError("Could not start video duration detection; check the private Python environment") from exc
    try:
        metadata = json.loads(result.stdout)
        duration = metadata["duration_seconds"]
        source_frames = metadata["source_frames"]
        if (result.returncode or type(duration) not in (float, int) or not math.isfinite(duration)
                or duration <= 0 or type(source_frames) is not int or source_frames <= 0):
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Could not detect this video's duration; use a readable video file or turn off automatic batch length") from exc
    return metadata


def select_batch_video_length(config, uploads):
    """Return an independent config and durable explanation of its chosen size."""
    vfx = is_vfx_config(config)
    field = "source_video" if vfx else "reference_videos"
    sources = uploads.get(field)
    sources = [sources] if isinstance(sources, str) else sources or []
    if config.get("mode") != "ref2va" or len(sources) != 1:
        raise ValueError("Automatic batch length needs exactly one source video per job in Reference mode")
    probe = probe_video(sources[0], exact_frames=vfx)
    duration = probe["duration_seconds"]
    if duration < 2 - 1e-3 or duration > MAX_SOURCE_SECONDS:
        raise ValueError(f"This video is {duration:.3f} seconds long; H3 reference videos must be 2–15 seconds. Trim the clip before adding it to the batch.")
    if vfx and probe["source_frames"] < 73:
        raise ValueError("Automatic batch length: VFX Edit needs at least 73 source frames at 24 fps (about 3.05 seconds)")
    # VFX already pads the resampled source and removes that padding from the
    # output. Ordinary references need a ceiling, so their tail is not cut off.
    required = probe["source_frames"] if vfx else math.ceil(duration * FPS - 1e-8)
    minimum = 73 if vfx else 124
    frames = max(minimum, required + (5 - required) % 17)
    if frames > 362:
        raise ValueError("No available H3 length covers this video; trim it to 15 seconds")
    selected = frames / FPS
    metadata = {"duration_seconds": duration, "frames": frames, "selected_seconds": selected,
                "source_field": field, "capped": False, "preserve_source_duration": vfx,
                "output_seconds": probe["source_frames"] / FPS if vfx else selected}
    return {**config, "frames": frames}, metadata


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in {"--probe", "--probe-frames"}:
        raise SystemExit(2)
    try:
        print(json.dumps(_probe_video(sys.argv[2], exact_frames=sys.argv[1] == "--probe-frames")))
    except Exception:
        # Media diagnostics may contain paths or file metadata. The request
        # receives a bounded, clear error instead of publishing those details.
        raise SystemExit(1)
