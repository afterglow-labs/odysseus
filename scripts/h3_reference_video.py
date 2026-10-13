"""Data-only reference ranges and bounded, frame-accurate local clip extraction."""
import math
from pathlib import Path
import shutil
import subprocess
import tempfile


def validate_ranges(value, count=None):
    """An empty list retains the original whole-clip contract for older jobs."""
    if not isinstance(value, list) or len(value) > 3:
        raise ValueError("Reference video ranges must be a list of at most three selections")
    if value and count is not None and len(value) != count:
        raise ValueError("Set one reference video range for each attached reference video, in the same order")
    result = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"start_seconds", "duration_seconds"}:
            raise ValueError("Each reference video range needs start_seconds and duration_seconds")
        start, duration = row["start_seconds"], row["duration_seconds"]
        if (type(start) not in (int, float) or not math.isfinite(start) or start < 0
                or type(duration) not in (int, float) or not math.isfinite(duration) or not 2 <= duration <= 15):
            raise ValueError("Reference video start must be a finite nonnegative number and selected length must be 2–15 seconds")
        result.append({"start_seconds": start, "duration_seconds": duration})
    return result


def selected_duration(source_duration, selection):
    remaining = source_duration - selection["start_seconds"]
    duration = min(remaining, selection["duration_seconds"])
    if duration < 2 - 1e-3:
        raise ValueError("The selected reference video start leaves less than 2 seconds; choose an earlier start")
    return duration


def read_selected_video(path, selection, reader):
    """Seek and decode only the selected local interval, preserving its audio.

    Accurate input seeking decodes/discards from the preceding keyframe. The
    temporary lossless clip contains at most 15 seconds at the model's bounded
    resolution; the caller never allocates the full source movie. Audio and
    video use the same seek offset, including a delayed video stream start.
    """
    import av

    selection = validate_ranges([selection], 1)[0]
    source_path = Path(path).resolve(strict=True)
    with av.open(str(source_path), options={"protocol_whitelist": "file,pipe"}) as source:
        if not source.streams.video:
            raise ValueError("Reference video contains no video track")
        stream = source.streams.video[0]
        video_start = float(stream.start_time * stream.time_base) if stream.start_time is not None else 0.0
        file_start = float(source.start_time / av.time_base) if source.start_time is not None else 0.0
        offset = max(0.0, video_start - file_start) + selection["start_seconds"]
        if stream.duration is not None and stream.time_base is not None:
            selected_duration(float(stream.duration * stream.time_base), selection)
    executable = shutil.which("ffmpeg")
    if executable is None:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="odysseus-h3-reference-") as directory:
        target = Path(directory) / "selection.mkv"
        command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                   "-protocol_whitelist", "file,pipe", "-ss", str(offset), "-i", str(source_path),
                   "-t", str(selection["duration_seconds"]), "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "fps=24:start_time=0,scale=w='min(iw,sqrt(1032192*iw/ih))':h=-2",
                   "-af", "aresample=32000:async=1:first_pts=0", "-c:v", "ffv1", "-threads", "2",
                   "-c:a", "pcm_s16le", "-ar", "32000", "-ac", "2", str(target)]
        try:
            result = subprocess.run(command, capture_output=True, timeout=180, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Selecting this reference video segment took too long; check the source file") from exc
        except OSError as exc:
            raise ValueError("Could not start the reference video segment decoder") from exc
        if result.returncode or not target.is_file():
            raise ValueError("Could not read the selected reference video segment; check its start and length")
        try:
            return reader(target)
        except (ValueError, av.FFmpegError) as exc:
            raise ValueError("The selected reference video segment must contain at least 2 seconds of readable video") from exc
