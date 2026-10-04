"""Inspect installed packages in the exact local or selected remote Python."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import sys

from src.dependency_catalog import is_native_windows


# Keep this probe stdlib-only: it must still list an environment whose pip or
# optional runtimes are broken. Never import the packages being inspected.
ENVIRONMENT_PROBE = r'''
import importlib.metadata as metadata
import json, re, subprocess, sys
normalize = lambda name: re.sub(r"[-_.]+", "-", name).lower()
installed = {}
for dist in metadata.distributions():
    name = dist.metadata.get("Name")
    if name and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
        installed.setdefault(normalize(name), {"name": name, "version": dist.version})
result = {"python_version": sys.version.split()[0], "executable": sys.executable,
          "prefix": sys.prefix, "isolated": sys.prefix != sys.base_prefix,
          "packages": sorted(installed.values(), key=lambda p: p["name"].lower()),
          "updates_checked": False}
if "--check-updates" in sys.argv:
    try:
        check = subprocess.run([sys.executable, "-m", "pip", "list", "--outdated", "--format=json",
                                "--disable-pip-version-check", "--timeout", "8", "--retries", "0"],
                               capture_output=True, text=True, timeout=90)
        if check.returncode:
            raise RuntimeError((check.stderr or check.stdout or "Package update check failed")[-1500:])
        if re.search(r'could not fetch|connection.*(?:failed|error)|failed to establish|no matching distributions|name resolution|sslerror|proxyerror', check.stderr, re.I):
            raise RuntimeError(check.stderr[-1500:])
        updates = {normalize(p["name"]): p for p in json.loads(check.stdout)}
        for name, pkg in installed.items():
            pkg["latest_version"] = updates.get(name, {}).get("latest_version", pkg["version"])
            pkg["update_available"] = name in updates
        result["updates_checked"] = True
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        result["check_error"] = str(exc)
print(json.dumps(result))
'''


def _posix_path(path):
    return '"$HOME"/' + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)


def probe_command(*, host=None, ssh_port=None, env="none", env_path="", platform="", check_updates=False):
    """Build argv without routing local Windows through WSL or PATH Python."""
    check = ["--check-updates"] if check_updates else []
    if not host:
        if getattr(sys, "frozen", False):
            raise ValueError("Environment updates require the native desktop launcher.")
        return [sys.executable, "-c", ENVIRONMENT_PROBE, *check]
    if not re.fullmatch(r"[A-Za-z0-9_.@:\[\]-]+", host) or host.startswith("-"):
        raise ValueError("Invalid remote host")
    if ssh_port and (not str(ssh_port).isdigit() or not 1 <= int(ssh_port) <= 65535):
        raise ValueError("Invalid SSH port")
    if any(char in env_path for char in "\r\n\x00"):
        raise ValueError("Invalid environment path")
    if env not in {"none", "venv", "conda"}:
        raise ValueError("Unknown environment type")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    if ssh_port:
        argv += ["-p", str(ssh_port)]
    argv.append(host)
    # A base64 payload keeps remote shell quoting small and deterministic.
    payload = base64.b64encode(ENVIRONMENT_PROBE.encode()).decode()
    code = f"import base64;exec(base64.b64decode('{payload}'))"
    if is_native_windows(platform):
        quote = lambda value: "'" + value.replace("'", "''") + "'"
        if env == "venv" and env_path:
            root = re.sub(r"[\\/]Scripts[\\/]Activate\.ps1$", "", env_path, flags=re.I).rstrip("\\/")
            command = "& " + quote(root + "\\Scripts\\python.exe")
        elif env == "conda" and env_path:
            selector = "-p" if re.search(r"[\\/:]", env_path) else "-n"
            command = f"conda run --no-capture-output {selector} {quote(env_path)} python"
        else:
            command = "python"
        script = command + " -c " + quote(code) + (" --check-updates" if check else "")
        encoded = base64.b64encode(script.encode("utf-16-le")).decode()
        argv.append("powershell -NoProfile -NonInteractive -EncodedCommand " + encoded)
    else:
        if env == "venv" and env_path:
            root = env_path.removesuffix("/bin/activate").rstrip("/")
            command = _posix_path(root + "/bin/python")
        elif env == "conda" and env_path:
            selector = "-p" if "/" in env_path else "-n"
            command = f"conda run --no-capture-output {selector} {_posix_path(env_path)} python"
        else:
            command = "python3"
        argv.append(command + " -c " + shlex.quote(code) + (" --check-updates" if check else ""))
    return argv


async def inspect_environment(**target):
    argv = probe_command(**target)
    try:
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        raise RuntimeError(f"Cannot start the selected environment check: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=105 if target.get("check_updates") else 20)
    except asyncio.TimeoutError as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise RuntimeError("The environment check timed out. Retry with the selected server available.") from exc
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    if process.returncode:
        raise RuntimeError((stderr or stdout).decode("utf-8", errors="replace")[-1500:] or "Environment check failed")
    # Activation tools may print a banner before the single JSON result.
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        try:
            result = json.loads(line)
            if isinstance(result, dict) and isinstance(result.get("packages"), list):
                return result
        except ValueError:
            pass
    raise RuntimeError("The selected Python returned no package inventory.")
