"""Dependency-free checks for the one supported Odysseus Python runtime."""

import platform
from pathlib import Path
import subprocess
import sys
import sysconfig


def required_python_version():
    """Read the same minor version used by installers and CI, including bundles."""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    version_file = root / ".python-version"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
        parts = tuple(int(part) for part in value.split("."))
        if len(parts) != 2 or parts[0] != 3:
            raise ValueError(value)
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"Cannot read the supported Python version from {version_file}. "
            "Restore .python-version from the Odysseus checkout or rebuild the portable app."
        ) from exc
    return parts


def require_supported_python():
    """Reject unsupported interpreters before importing application dependencies."""
    required = required_python_version()
    if (
        sys.implementation.name == "cpython"
        and sys.version_info[:2] == required
        and sys.version_info.releaselevel == "final"
        and not sysconfig.get_config_var("Py_GIL_DISABLED")
    ):
        return
    version = ".".join(str(part) for part in required)
    actual = platform.python_version()
    raise SystemExit(
        f"Odysseus requires stable CPython {version}.x (standard GIL build). "
        f"Running {sys.implementation.name} {actual} at {sys.executable}. "
        f"Install the latest Python {version} patch release, move any existing venv "
        "aside, then recreate it with that interpreter and install requirements.lock. "
        "On macOS run ./start-macos.sh; on Windows run .\\launch-windows.ps1."
    )


def require_native_architecture():
    """Do not build/load Intel extensions through Rosetta on Apple Silicon."""
    if sys.platform != "darwin" or platform.machine() == "arm64":
        return
    try:
        translated = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return
    if translated == "1":
        version = ".".join(str(part) for part in required_python_version())
        raise SystemExit(
            "Odysseus requires native arm64 Python on Apple Silicon. "
            f"Install /opt/homebrew/bin/python{version} with brew install python@{version}, "
            "move the existing venv aside, then rerun ./start-macos.sh."
        )


if __name__ == "__main__":
    require_supported_python()
    require_native_architecture()
    print(f"Using CPython {platform.python_version()} ({platform.machine()}): {sys.executable}")
