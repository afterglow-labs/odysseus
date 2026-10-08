#!/usr/bin/env python3
"""Install SGLang in Odysseus's separate Python 3.12 environment.

Uses uv's package cache and hardlinks on Linux to avoid copying shared CUDA
wheels. Does not install packages in the app environment or system Python.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from src.sglang_runtime import managed_sglang_venv, sglang_python_preflight_code


def prepare_runtime(*, python="3.12", uv=None):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The managed SGLang runtime requires Linux/WSL2.")
    runtime = managed_sglang_venv()
    executable = runtime / "bin/python"
    uv = uv or shutil.which("uv")
    if not uv:
        raise RuntimeError("Install uv, then rerun scripts/setup_sglang_runtime.py.")
    env = dict(os.environ)
    # Any interpreter uv needs to download also belongs to Odysseus.
    env["UV_PYTHON_INSTALL_DIR"] = str(runtime.parent / "python")
    if not executable.exists():
        if runtime.exists() and any(runtime.iterdir()):
            raise RuntimeError(f"Incomplete environment at {runtime}; inspect it before retrying.")
        subprocess.run([uv, "venv", "--seed", "--python", python, str(runtime)], env=env, check=True)
    subprocess.run([str(executable), "-c", sglang_python_preflight_code()], env=env, check=True)
    # A venv previously created without --seed still needs pip for Cookbook.
    has_pip = subprocess.run([str(executable), "-m", "pip", "--version"], capture_output=True).returncode == 0
    if not has_pip:
        subprocess.run([uv, "pip", "install", "--python", str(executable), "pip"], env=env, check=True)
    return executable, uv, env


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default="3.12", help="Python used when creating the private runtime")
    parser.add_argument("--prepare-only", action="store_true", help="Create/check the environment without installing SGLang")
    parser.add_argument("--dry-run", action="store_true", help="Resolve SGLang dependencies without installing them")
    args = parser.parse_args(argv)
    try:
        import fcntl
        runtime = managed_sglang_venv()
        runtime.parent.mkdir(parents=True, exist_ok=True)
        with (runtime.parent / "sglang-setup.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another SGLang setup is already running.") from None
            executable, uv, env = prepare_runtime(python=args.python)
            print(f"[odysseus] SGLang environment: {runtime}", flush=True)
            if args.prepare_only:
                return 0
            free = shutil.disk_usage(runtime).free / 1024**3
            print(f"[odysseus] Available disk space: {free:.1f} GiB", flush=True)
            if free < 2 and not args.dry_run:
                raise RuntimeError("Less than 2 GiB free. Free disk space before installing SGLang.")
            command = [uv, "pip", "install", "--python", str(executable), "--prerelease=allow",
                       "--only-binary=sglang", "--link-mode=hardlink", "--upgrade", "sglang>=0.5.21"]
            if args.dry_run:
                command.append("--dry-run")
            subprocess.run(command, env=env, check=True)
            if not args.dry_run:
                subprocess.run([str(executable), "-c", "import sglang; print('SGLang', sglang.__version__)"], env=env, check=True)
                print(f"[odysseus] Ready. Cookbook will use {executable} for local SGLang launches.", flush=True)
            return 0
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
