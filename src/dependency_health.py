"""Read installed dependency metadata without importing packages or downloading."""

from __future__ import annotations

from collections import deque
from importlib import metadata
from typing import Callable, Iterable, Mapping

try:
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version
except ImportError:
    try:
        from pip._vendor.packaging.markers import default_environment
        from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
        from pip._vendor.packaging.utils import canonicalize_name
        from pip._vendor.packaging.version import InvalidVersion, Version
    except ImportError:
        Requirement = None


def check_dependency_health(
    requirements: Iterable[str] | str,
    *,
    aliases: Mapping[str, str] | None = None,
    version_fn: Callable | None = None,
    requires_fn: Callable | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict:
    """Check requested requirements and their active transitive dependencies.

    ``aliases`` maps raw VCS/path specs to explicit distribution requirements.
    Metadata functions are injectable for tests and alternate environments.
    Only requested extras are followed; unrelated test/dev extras are ignored.
    Issue requirements combine every applicable version constraint and omit
    already-evaluated markers so callers can offer a precise repair command.
    This module has no application imports and can also run on remote Python.
    """
    if Requirement is None:
        issue = {
            "name": "packaging", "requirement": "packaging", "kind": "missing",
            "message": "Install packaging in this Python environment to check dependency compatibility.",
            "required_by": ["dependency checker"],
        }
        return {"installed": False, "missing": [issue], "conflicts": [], "issues": [issue], "versions": {}}
    version_fn = version_fn or metadata.version
    requires_fn = requires_fn or metadata.requires
    marker_environment = {**default_environment(), **(environment or {})}
    aliases = aliases or {}
    if isinstance(requirements, str):
        requirements = [requirements]

    pending = deque((str(spec), None, ("",)) for spec in requirements)
    requested: dict[str, list[tuple[Requirement, str | None]]] = {}
    versions: dict[str, str] = {}
    absent: set[str] = set()
    expanded: dict[str, set[str]] = {}
    dependency_metadata: dict[str, list[str]] = {}
    metadata_errors: dict[str, str] = {}
    malformed: list[dict] = []
    seen = set()

    while pending:
        raw, parent, contexts = pending.popleft()
        try:
            requirement = Requirement(aliases.get(raw, raw))
        except InvalidRequirement as exc:
            key = (raw, parent)
            if key not in seen:
                seen.add(key)
                malformed.append({
                    "name": parent or raw,
                    "requirement": raw,
                    "kind": "incompatible",
                    "message": f"Invalid dependency metadata: {raw}: {exc}",
                    "required_by": [parent] if parent else [],
                })
            continue
        if requirement.marker and not any(
            requirement.marker.evaluate({**marker_environment, "extra": extra})
            for extra in contexts
        ):
            continue
        name = canonicalize_name(requirement.name)
        key = (str(requirement), parent)
        if key in seen:
            continue
        seen.add(key)
        requested.setdefault(name, []).append((requirement, parent))
        if name not in versions and name not in absent and name not in metadata_errors:
            try:
                versions[name] = str(version_fn(name))
            except metadata.PackageNotFoundError:
                absent.add(name)
            except Exception as exc:
                metadata_errors[name] = f"Cannot read installed version: {exc}"
        if name not in versions:
            continue

        # A dependency may be reached later with additional extras. Expand only
        # those new contexts, while always including its ordinary dependencies.
        active_extras = {"", *requirement.extras}
        new_contexts = active_extras - expanded.get(name, set())
        if not new_contexts:
            continue
        expanded.setdefault(name, set()).update(new_contexts)
        if name not in dependency_metadata and name not in metadata_errors:
            try:
                dependency_metadata[name] = list(requires_fn(name) or [])
            except Exception as exc:
                metadata_errors[name] = f"Cannot read required dependencies: {exc}"
        for dependency in dependency_metadata.get(name, []):
            pending.append((dependency, name, tuple(sorted(new_contexts))))

    missing = []
    conflicts = list(malformed)
    for name, entries in sorted(requested.items()):
        extras = sorted({extra for requirement, _ in entries for extra in requirement.extras})
        constraints = sorted({str(spec) for requirement, _ in entries for spec in requirement.specifier})
        repair = name + (f"[{','.join(extras)}]" if extras else "") + ",".join(constraints)
        urls = {requirement.url for requirement, _ in entries if requirement.url}
        if len(urls) == 1 and not constraints:
            repair += " @ " + next(iter(urls))
        for malformed_issue in malformed:
            if malformed_issue["name"] == name and malformed_issue["required_by"]:
                # Repair the distribution publishing corrupt metadata, rather
                # than asking pip to install its unparseable dependency text.
                malformed_issue["requirement"] = repair
        parents = sorted({parent for _, parent in entries if parent})
        issue = {"name": name, "requirement": repair, "required_by": parents}
        if name in absent:
            missing.append({**issue, "kind": "missing", "message": f"{repair} is missing."})
            continue
        error = metadata_errors.get(name)
        installed_version = versions.get(name)
        if installed_version is not None:
            issue["installed_version"] = installed_version
            try:
                parsed_version = Version(installed_version)
                if any(not requirement.specifier.contains(parsed_version, prereleases=True) for requirement, _ in entries):
                    error = f"{name} {installed_version} is installed; {repair} is required."
            except InvalidVersion:
                error = f"{name} has invalid installed version {installed_version!r}."
        if error:
            conflicts.append({**issue, "kind": "incompatible", "message": error})

    return {
        "installed": not missing and not conflicts,
        "missing": missing,
        "conflicts": conflicts,
        "issues": missing + conflicts,
        "versions": dict(sorted(versions.items())),
    }
