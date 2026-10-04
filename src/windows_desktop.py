"""Small Windows desktop bootstrap, using the same app venv as native installs.

Only this launcher is frozen. The application keeps a complete, pip-capable
Python environment, so optional dependencies can be installed and imported.
"""

import json
import os
from pathlib import Path
import subprocess
import sys


def launch_desktop(repo_dir, *, data_dir=None):
    repo = Path(repo_dir).resolve()
    runtime = repo / "venv"
    python = runtime / "Scripts/python.exe"
    entrypoint = repo / "launcher.py"
    if not python.is_file() or not (runtime / "pyvenv.cfg").is_file():
        raise RuntimeError(
            f"Odysseus's Python environment is missing at {runtime}. "
            "Run launch-windows.ps1 in the install folder to set it up."
        )
    if not entrypoint.is_file():
        raise RuntimeError(f"Odysseus's install folder is missing: {repo}")
    profile = Path(data_dir) if data_dir else Path.home() / ".odysseus/data"
    logs = profile / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PIP_TARGET", "PIP_PREFIX"):
        env.pop(key, None)
    env.update(
        ODYSSEUS_DATA_DIR=str(profile), DATA_DIR=str(profile),
        VIRTUAL_ENV=str(runtime), PYTHONNOUSERSITE="1",
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_USER="0",
        PIP_REQUIRE_VIRTUALENV="true",
        PATH=str(python.parent) + os.pathsep + env.get("PATH", ""),
    )
    if getattr(sys, "frozen", False) and sys.platform == "win32":
        # Do not make the real Python child load DLLs from PyInstaller's
        # temporary bootstrap directory.
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    with (logs / "desktop.log").open("ab") as log:
        return subprocess.Popen(
            [str(python), str(entrypoint), "--desktop"], cwd=repo, env=env,
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def main():
    try:
        if getattr(sys, "frozen", False):
            config = Path(sys.executable).with_name("Odysseus-launcher.json")
            repo = json.loads(config.read_text(encoding="utf-8"))["repo_dir"]
        else:
            repo = Path(__file__).resolve().parent.parent
        launch_desktop(repo)
    except Exception as exc:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(exc), "Odysseus", 0x10)
        else:
            raise
        raise SystemExit(1)


if __name__ == "__main__":
    main()
