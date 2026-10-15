"""Original videos from owner-scoped, immutable job input snapshots."""
import math
from pathlib import Path

from fastapi import HTTPException

from src.h3_video import _locked, _read
from src.video_job_edit import revision
from src.video_server_files import MediaRoot, VIDEO_EXTENSIONS, _open_selection

VIDEO_FIELDS = ("source_video", "reference_videos")


def _videos(manifest, field):
    source = manifest.get("uploads", manifest)
    values = source.get(field, []) if isinstance(source, dict) else []
    return [values] if isinstance(values, str) else values if isinstance(values, list) else []


def _section(manifest, field, index):
    config = manifest.get("config", {})
    section = {}
    if field == "reference_videos":
        ranges = config.get("reference_video_ranges", [])
        if isinstance(ranges, list) and index < len(ranges) and isinstance(ranges[index], dict):
            section = ranges[index]
    elif config.get("seed_hunter", {}).get("stage") == "continuation":
        options = config["seed_hunter"]
        start, end = options.get("source_start_seconds", 0), options.get("source_end_seconds")
        if type(start) in (int, float) and (end is None or type(end) in (int, float)):
            section = {"start_seconds": start, "duration_seconds": None if end is None else end - start}
    start, duration = section.get("start_seconds", 0), section.get("duration_seconds")
    if (type(start) not in (int, float) or not math.isfinite(start) or start < 0
            or duration is not None and (type(duration) not in (int, float)
                or not math.isfinite(duration) or duration <= 0)):
        return None
    return start, duration


def input_videos(job_id, manifest):
    """Manifest-only descriptors: history polling never opens or probes media."""
    result = []
    names = manifest.get("input_names", {})
    version = revision(manifest)
    for field in VIDEO_FIELDS:
        labels = names.get(field, []) if isinstance(names, dict) else []
        for index, value in enumerate(_videos(manifest, field)[:17]):
            if not isinstance(value, str) or Path(value).suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            section = _section(manifest, field, index)
            if section is None:
                continue
            start, duration = section
            label = labels[index] if isinstance(labels, list) and index < len(labels) else Path(value).name
            name = str(label).replace("\\", "/").rsplit("/", 1)[-1]
            name = "".join(char for char in name if char.isprintable())[:255] or "Input video"
            result.append({"id": f"{field}:{index}", "field": field, "index": index, "name": name,
                "url": f"/api/video/h3/jobs/{job_id}/input-videos/{field}/{index}/preview?revision={version}",
                "playback": "selected_section" if start or duration is not None else "original",
                "start_seconds": start, "duration_seconds": duration, "revision": version})
    return result


def _owned_manifest(manager, job_id, owner, expected_revision=None):
    directory = manager.directory(job_id)
    state = _read(directory / "state.json")
    if not state or state.get("owner") != owner:
        raise HTTPException(404, "Video job not found")
    manifest = _read(directory / "manifest.json")
    if expected_revision is not None and revision(manifest) != expected_revision:
        raise HTTPException(409, "This job's inputs changed. Refresh the job before playing its input video.")
    return directory, manifest


def open_input_video_proxy(manager, job_id, owner, field, index, expected_revision=None):
    from src.video_preview_proxy import open_owned_video_proxy
    if field not in VIDEO_FIELDS or type(index) is not int or not 0 <= index < 17:
        raise HTTPException(404, "Saved input video not found")
    directory, manifest = _owned_manifest(manager, job_id, owner, expected_revision)
    selected = next((row for row in input_videos(job_id, manifest)
                     if row["field"] == field and row["index"] == index), None)
    if selected is None:
        raise HTTPException(404, "Saved input video not found")
    version = revision(manifest)

    def opener():
        # Hold the edit lock only until acquiring the validated source handle.
        # A second revision check after conversion rejects edits made meanwhile.
        with _locked(directory / "job.lock"):
            _, current = _owned_manifest(manager, job_id, owner, version)
            values = _videos(current, field)
            if index >= len(values) or not isinstance(values[index], str):
                raise HTTPException(404, "Saved input video not found")
            path = Path(values[index])
            if path.parent != directory.absolute() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                raise HTTPException(404, "Saved input video is unavailable")
            try:
                return _open_selection({"root": "job", "path": f"{job_id}/{path.name}",
                    "size": 1, "mtime_ns": "0", "index": 0},
                    [MediaRoot("job", "Saved job inputs", manager.root)])
            except HTTPException as error:
                raise HTTPException(404, "Saved input video is unavailable") from error

    upload = open_owned_video_proxy(opener, {"root": "job-input", "path": f"{job_id}/{version}/{field}/{index}"},
                                   start_seconds=selected["start_seconds"], duration_seconds=selected["duration_seconds"])
    try:
        _owned_manifest(manager, job_id, owner, version)
    except BaseException:
        upload.file.close()
        raise
    return upload, selected
