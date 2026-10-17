"""Keep explicit Hub updates compatible with the selected Python environment.

The source is also sent to remote interpreters; keep it independent of app imports.
"""

from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

try:
    from packaging.markers import default_environment
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
except ImportError:
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import InvalidRequirement, Requirement
    from pip._vendor.packaging.utils import canonicalize_name


def hub_requirements(arguments):
    found = []
    for argument in arguments:
        try:
            requirement = Requirement(argument)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) == "huggingface-hub":
            found.append(requirement)
    return found


def installed_hub_constraints(*, distributions=None, environment=None):
    """Read active base requirements; unrelated optional extras are not enabled."""
    marker_environment = {**default_environment(), **(environment or {}), "extra": ""}
    constraints = set()
    parents = set()
    for distribution in metadata.distributions() if distributions is None else distributions:
        for raw in distribution.requires or ():
            try:
                requirement = Requirement(raw)
            except InvalidRequirement:
                if "huggingface" in raw.lower():
                    raise ValueError(f"Cannot safely read Hugging Face requirement: {raw}")
                continue
            if canonicalize_name(requirement.name) != "huggingface-hub":
                continue
            if requirement.marker and not requirement.marker.evaluate(marker_environment):
                continue
            if requirement.url:
                raise ValueError("An installed package requires a specific Hugging Face source URL; automatic Hub updates are unavailable.")
            if requirement.specifier:
                constraints.add("huggingface-hub" + str(requirement.specifier))
                parents.add(distribution.metadata.get("Name", "installed package"))
    return sorted(constraints), sorted(parents)


def validate_hub_pins(requested, constraints):
    for requirement in requested:
        if requirement.url:
            raise ValueError("Use a Hugging Face package version, not a source URL, for a compatibility-checked update.")
        for specifier in requirement.specifier:
            if specifier.operator in {"==", "==="} and "*" not in specifier.version:
                for constraint in constraints:
                    if not Requirement(constraint).specifier.contains(specifier.version, prereleases=True):
                        raise ValueError(f"Requested Hugging Face {specifier.version} conflicts with installed requirement {constraint}. No packages were changed.")


def run_hub_install(arguments):
    requested = hub_requirements(arguments)
    if not requested:
        raise ValueError("Expected a huggingface_hub install request")
    isolated = sys.prefix != sys.base_prefix or (
        os.environ.get("CONDA_PREFIX") and Path(os.environ["CONDA_PREFIX"]).resolve() == Path(sys.prefix).resolve()
    )
    if not isolated:
        raise ValueError("Hugging Face updates require an isolated Python environment. Select the Odysseus venv; global Python will not be changed.")
    if any(arg.split("=", 1)[0] in {"--target", "-t", "--prefix", "--root", "--python"}
           or arg.startswith("-t") for arg in arguments):
        raise ValueError("Hugging Face updates must stay inside the selected Python environment.")
    constraints, parents = installed_hub_constraints()
    validate_hub_pins(requested, constraints)
    arguments = [arg for arg in arguments if arg not in {"--user", "--break-system-packages"}]
    print(f"[odysseus] Updating Hugging Face with {sys.executable}", flush=True)
    if constraints:
        print("[odysseus] Keeping installed requirements: " + "; ".join(constraints), flush=True)
        print("[odysseus] Required by: " + ", ".join(parents), flush=True)
    environment = dict(os.environ)
    for key in ("PIP_TARGET", "PIP_PREFIX", "PIP_ROOT"):
        environment.pop(key, None)
    environment["PIP_USER"] = "false"
    environment["PIP_REQUIRE_VIRTUALENV"] = "false" if sys.prefix == sys.base_prefix else "true"
    # Close the file before pip opens it, including on Windows.
    with tempfile.TemporaryDirectory(prefix="odysseus-hf-update-") as directory:
        path = Path(directory) / "constraints.txt"
        path.write_text("\n".join(constraints) + "\n", encoding="utf-8")
        return subprocess.call([sys.executable, "-m", "pip", *arguments, "--constraint", str(path)], env=environment)


def hub_install_source():
    return Path(__file__).read_text(encoding="utf-8") + "\ntry:\n    raise SystemExit(run_hub_install(sys.argv[1:]))\nexcept (ValueError, OSError) as error:\n    print('[odysseus] ' + str(error), file=sys.stderr)\n    raise SystemExit(1)\n"


def hub_environment_preflight_code():
    """Check isolation before bootstrapping pip, without needing packaging."""
    return """import os, sys
from pathlib import Path
isolated = sys.prefix != sys.base_prefix or (os.environ.get('CONDA_PREFIX') and Path(os.environ['CONDA_PREFIX']).resolve() == Path(sys.prefix).resolve())
if not isolated:
    print('[odysseus] Hugging Face updates require an isolated Python environment. Select the Odysseus venv; global Python will not be changed.', file=sys.stderr)
    raise SystemExit(1)
"""


def hub_install_command(command, *, powershell=False):
    """Wrap only a Python/pip Hub install; preserve the selected interpreter."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    if len(parts) < 5 or parts[1:4] != ["-m", "pip", "install"] or not hub_requirements(parts[4:]):
        return command
    quote = (lambda value: "'" + value.replace("'", "''") + "'") if powershell else shlex.quote
    prefix = "& " if powershell else ""
    return prefix + " ".join(quote(part) for part in [parts[0], "-c", hub_install_source(), *parts[3:]])
