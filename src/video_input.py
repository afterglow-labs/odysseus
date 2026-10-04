"""Video attachment detection and llama.cpp's native video content format."""

import base64
from pathlib import Path


def is_video_file(filename: str, mime: str = "") -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    # Voice recordings can also use .webm or .ogg containers.
    if mime.startswith("audio/"):
        return False
    return mime.startswith("video/") or Path(filename or "").suffix.lower() in {
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".mpg", ".ogv",
    }


def video_content_part(path: str) -> dict:
    # Native llama.cpp accepts raw base64 here and determines the container
    # from the bytes. Do not send a client filesystem path to a remote server.
    with open(path, "rb") as video:
        data = base64.b64encode(video.read()).decode("ascii")
    return {"type": "input_video", "input_video": {"data": data}}
