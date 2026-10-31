"""File transfers do not use the generic API response deadline.

Identify uploads before reading their bodies, and downloads before opening or
preparing their files. Waiting for response headers to identify a download is
too late when assembling an archive or opening a network file takes time.
"""
import re


_UPLOAD_METHODS = {"POST", "PUT", "PATCH"}
_UPLOAD_TYPES = {
    "multipart/form-data", "application/octet-stream", "application/zip",
    "application/x-zip-compressed", "application/gzip", "application/x-tar",
    "application/pdf",
}

# Some transfers use JSON or URL-encoded forms: server-side input files are
# copied into jobs, gallery uploads carry base64, and exports prepare archives.
_TRANSFER_POST_PATHS = {
    "/api/import", "/api/contacts/import", "/api/gallery/upload",
    "/api/gallery/download-zip", "/api/documents/export-zip",
    "/api/workflow-graphs/import", "/api/workflow-graphs/runs",
    "/api/tts/synthesize", "/api/skills/import-from-url",
    "/api/video/h3/lora-library/download",
}
_VIDEO = r"/api/video/(?:h3|bfs)"
_TRANSFER_POST_PATTERNS = tuple(map(re.compile, (
    _VIDEO + r"/(?:jobs|local-previews|workflow/(?:export|import))",
    r"/api/document/[^/]+/export-pdf/preview",
    r"/api/embeddings/models/.+/download",
)))
_TRANSFER_PATCH = re.compile(_VIDEO + r"/jobs/[^/]+")

_DOWNLOAD_PATHS = {"/api/export", "/api/contacts/export",
                   "/api/codex/plugin.zip", "/api/claude/plugin.zip"}
_DOWNLOAD_PATTERNS = tuple(map(re.compile, (
    _VIDEO + r"/jobs/[^/]+/(?:video|workflow)",
    _VIDEO + r"/jobs/[^/]+/input-videos/[^/]+/[0-9]+/preview",
    r"/api/video/h3/jobs/[^/]+/seed-hunter/candidates/[0-9]+/(?:video|audio)",
    r"/api/video/h3/jobs/[^/]+/scout/previews/[0-9]+",
    _VIDEO + r"/local-previews/[^/]+/video",
    _VIDEO + r"/server-files/(?:preview|video)",
    _VIDEO + r"/workflow/exports/[^/]+",
    _VIDEO + r"/workflow/imports/[^/]+/attachments/[0-9]+",
    r"/api/workflow-graphs/runs/[^/]+/outputs/[^/]+",
    r"/api/generated-image/[^/]+",
    r"/api/session/[^/]+/export",
    r"/api/document/[^/]+/(?:render-pdf|export-pdf)",
    r"/api/calendar/export/[^/]+",
    r"/api/email/attachment/[^/]+/[0-9]+",
    r"/api/email/(?:attachments-download|inline-image)/[^/]+",
)))


def is_file_transfer(method, path, content_type=""):
    method = method.upper()
    path = path.rstrip("/")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if method in _UPLOAD_METHODS and (
        media_type in _UPLOAD_TYPES or media_type.startswith(("video/", "audio/", "image/"))
    ):
        return True
    if method == "POST":
        return path in _TRANSFER_POST_PATHS or any(pattern.fullmatch(path) for pattern in _TRANSFER_POST_PATTERNS)
    if method == "PATCH":
        return bool(_TRANSFER_PATCH.fullmatch(path))
    if method in {"GET", "HEAD"}:
        return (path.startswith("/static/") or path in _DOWNLOAD_PATHS
                or any(pattern.fullmatch(path) for pattern in _DOWNLOAD_PATTERNS))
    return False
