"""Read-only, bounded PyPI artifact checks for an explicit dependency audit.

This checks the latest release's Python constraint and published artifacts.
It does not install packages, resolve transitive requirements, or prove that a
source distribution will build. Ordinary status polling must not call it.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from packaging.specifiers import SpecifierSet
from packaging.tags import Tag, parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_sdist_filename, parse_wheel_filename
from packaging.version import Version

INDEX_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
CACHE_TTL_SECONDS = 600
MAX_CACHE_ENTRIES = 128
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_cache_lock = threading.RLock()
_DISTRIBUTION_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z")


def _release_metadata(distribution: str) -> dict:
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(distribution)
        if cached and cached[0] > now:
            _cache.move_to_end(distribution)
            return cached[1]
        _cache.pop(distribution, None)
    request = urllib.request.Request(
        f"https://pypi.org/pypi/{distribution}/json",
        headers={"Accept": "application/json", "User-Agent": "Odysseus-dependency-check"},
    )
    with urllib.request.urlopen(request, timeout=INDEX_TIMEOUT_SECONDS) as response:
        content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise ValueError("PyPI response exceeds the metadata size limit")
    metadata = json.loads(content)
    info = metadata["info"]
    files = metadata["urls"]
    if not isinstance(info, dict) or not isinstance(files, list) or not files:
        raise ValueError("PyPI returned incomplete release metadata")
    if canonicalize_name(info["name"]) != distribution:
        raise ValueError("PyPI returned a different distribution")
    Version(info["version"])
    # Do not cache the full project history or descriptions returned by PyPI.
    result = {
        "info": {key: info.get(key) for key in ("name", "version", "requires_python")},
        "urls": files,
    }
    with _cache_lock:
        _cache[distribution] = (time.monotonic() + CACHE_TTL_SECONDS, result)
        _cache.move_to_end(distribution)
        while len(_cache) > MAX_CACHE_ENTRIES:
            _cache.popitem(last=False)
    return result


def _unknown(note: str) -> dict:
    return {
        "latest_version": None,
        "requires_python": None,
        "has_compatible_artifact": None,
        "artifact_kind": None,
        "install_supported": None,
        "compatibility_note": note,
    }


def check_latest_release(
    distribution: str,
    *,
    python_version: str | None = None,
    supported_tags: Iterable[str | Tag] | None = None,
) -> dict:
    """Check official PyPI metadata against local or explicitly supplied tags.

    Remote callers must provide BOTH the remote Python version and its tags;
    never substitute the app host's platform for a remote environment. Unknown
    or unavailable metadata returns None, leaving existing install choices
    intact. A source archive is reported as potentially buildable, not ready.
    """
    if not isinstance(distribution, str) or not _DISTRIBUTION_NAME.fullmatch(distribution):
        return _unknown("Compatibility check requires a valid distribution name.")
    if (python_version is None) != (supported_tags is None):
        return _unknown("Compatibility check needs both the selected Python version and platform tags.")
    name = canonicalize_name(distribution)
    try:
        version = Version(python_version or ".".join(map(str, sys.version_info[:3])))
        tags = set()
        for index, tag in enumerate(supported_tags if supported_tags is not None else sys_tags()):
            if index >= 20_000:
                raise ValueError("too many platform tags")
            tags.update({tag} if isinstance(tag, Tag) else parse_tag(tag))
        if not tags:
            return _unknown("Compatibility check needs the selected Python's platform tags.")
        metadata = _release_metadata(name)
        info = metadata["info"]
        latest = info["version"]
        required = info.get("requires_python") or ""
        result = {
            "latest_version": latest,
            "requires_python": required,
            "has_compatible_artifact": False,
            "artifact_kind": None,
            "install_supported": False,
            "compatibility_note": "",
        }
        if not SpecifierSet(required).contains(version, prereleases=True):
            result["compatibility_note"] = f"Latest {name} {latest} requires Python {required}; selected Python is {version}."
            return result
        source_available = False
        for artifact in metadata["urls"]:
            if artifact.get("yanked"):
                continue
            if not SpecifierSet(artifact.get("requires_python") or required).contains(version, prereleases=True):
                continue
            filename = artifact["filename"]
            if artifact.get("packagetype") == "sdist":
                package, release = parse_sdist_filename(filename)
                if package == name and release == Version(latest):
                    source_available = True
            elif artifact.get("packagetype") == "bdist_wheel":
                package, release, _, wheel_tags = parse_wheel_filename(filename)
                if package == name and release == Version(latest) and tags.intersection(wheel_tags):
                    result.update(
                        has_compatible_artifact=True,
                        artifact_kind="wheel",
                        install_supported=True,
                        compatibility_note=f"Latest {name} {latest} has a wheel for the selected Python and platform; dependency resolution and runtime checks still apply.",
                    )
                    return result
        if source_available:
            result.update(
                has_compatible_artifact=True,
                artifact_kind="source",
                install_supported=True,
                compatibility_note=f"Latest {name} {latest} has a source archive; building may require additional tools or compatibility fixes.",
            )
        else:
            result["compatibility_note"] = (
                f"Latest {name} {latest} has no compatible wheel or source archive for Python {version} on the selected platform. "
                "Use a supported server or container, or wait for an upstream compatible release."
            )
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return _unknown("Could not verify current PyPI compatibility. Retry Check dependencies; existing install choices are unchanged.")
