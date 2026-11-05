#!/usr/bin/env python3
"""Start or stop native Odysseus and its separate local Afterglow stack.

Install symlinks named ``odysseus-cyber-start`` and ``odysseus-cyber-stop`` to
this file, or run it with an explicit ``start`` or ``stop`` argument. The normal
``odysseus-start`` shortcut remains independent.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request


PROJECT = "afterglow"
PROFILE = "local-llm"
ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def read_env(path: Path) -> dict[str, str]:
    """Read the simple KEY=value entries used by both local Compose files."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, raw = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw[1:-1]
        elif raw.startswith("'") and raw.endswith("'"):
            value = raw[1:-1]
        else:
            value = raw.split(" #", 1)[0].strip()
        values[key] = value
    return values


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".cyber-" + secrets.token_hex(6))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def update_env(path: Path, updates: dict[str, str]) -> bool:
    """Change only requested keys, retaining every other local setting."""
    original = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    lines = original.splitlines()
    existing = read_env(path)
    for key, value in updates.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value or "\r" in value or "$" in value:
            raise ValueError(f"invalid value for {key}")
        if existing.get(key) == value:
            continue
        literal = json.dumps(value) if any(char in value for char in " #\\\"'") else value
        matching = [i for i, line in enumerate(lines) if re.match(rf"^\s*(?:export\s+)?{re.escape(key)}\s*=", line)]
        if len(matching) > 1:
            raise ValueError(f"{path} has duplicate {key} assignments")
        replacement = f"{key}={literal}"
        if matching:
            lines[matching[0]] = replacement
        else:
            lines.append(replacement)
    result = "\n".join(lines).rstrip("\n") + "\n"
    if result == original:
        return False
    atomic_write(path, result)
    return True


def backend_root(odysseus_settings: dict[str, str]) -> Path:
    configured = os.getenv("AFTERGLOW_COMPOSE_ROOT") or odysseus_settings.get("AFTERGLOW_COMPOSE_ROOT")
    candidate = Path(configured).expanduser() if configured else ROOT.parent / "afterglow-security" / "afterglow-cybersecurity-gateway"
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"Afterglow checkout does not exist: {candidate}") from exc
    for name in ("docker-compose.yml", "docker-compose.host-model.yml", ".env.example"):
        if not (candidate / name).is_file():
            raise ValueError(f"Afterglow checkout is missing {candidate / name}")
    return candidate


def model_directory(backend: Path, backend_settings: dict[str, str], odysseus_settings: dict[str, str]) -> Path:
    configured = (os.getenv("OLLAMA_MODELS_DIR") or backend_settings.get("OLLAMA_MODELS_DIR")
                  or odysseus_settings.get("AFTERGLOW_OLLAMA_MODELS_DIR"))
    candidates = ([Path(configured).expanduser()] if configured else [
        Path.home() / ".ollama" / "models",
        Path.home() / ".local" / "share" / "afterglow-ollama" / "models",
    ])
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = backend / candidate
        candidate = candidate.resolve()
        if (candidate / "blobs").is_dir() and (candidate / "manifests").is_dir():
            return candidate
    raise ValueError("OLLAMA_MODELS_DIR must name an existing model directory with blobs/ and manifests/")


def prepare_backend(backend: Path, odysseus_settings: dict[str, str]) -> dict[str, str]:
    env_path = backend / ".env"
    fresh = not env_path.is_file()
    if fresh:
        atomic_write(env_path, (backend / ".env.example").read_text(encoding="utf-8"))
    settings = read_env(env_path)
    model_path = model_directory(backend, settings, odysseus_settings)
    updates = {
        "OLLAMA_MODELS_DIR": str(model_path),
        "SCOPEWEAVER_LOCAL_LLM_ONLY": "true",
        "SCOPEWEAVER_LLM_PROVIDER": "ollama",
        "SCOPEWEAVER_LLM_BASE_URL": "http://ollama:11434",
    }
    if not settings.get("POSTGRES_PASSWORD"):
        updates["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
    if not settings.get("SCOPEWEAVER_LLM_MODEL"):
        updates["SCOPEWEAVER_LLM_MODEL"] = "qwen3:32b"
    if not settings.get("SCOPEWEAVER_LLM_CONTEXT_WINDOW_K"):
        updates["SCOPEWEAVER_LLM_CONTEXT_WINDOW_K"] = "32"
    update_env(env_path, updates)
    for name in ("data", "private/c2-binaries"):
        (backend / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    return read_env(env_path)


def docker_running() -> bool:
    if not shutil.which("docker"):
        raise ValueError("Docker is not installed")
    return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def ensure_docker(odysseus_settings: dict[str, str]) -> None:
    if docker_running():
        return
    if not shutil.which("systemctl") or not shutil.which("sudo"):
        raise ValueError("Start the Docker daemon, then run odysseus-cyber-start again")
    print("Starting Docker...", flush=True)
    subprocess.run(["sudo", "-n", "systemctl", "start", "docker.service"], check=True)
    for _ in range(30):
        if docker_running():
            break
        time.sleep(1)
    else:
        raise ValueError("Docker did not become ready")
    # A disabled Docker service can wake unrelated restart-policy containers.
    # This optional, host-specific list restores only the projects the user
    # configured for that case; ordinary Docker starts never touch them.
    configured = odysseus_settings.get("AFTERGLOW_AUTO_PAUSE_COMPOSE_PROJECTS", "")
    for project in filter(None, (part.strip() for part in configured.split(","))):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", project):
            raise ValueError("invalid AFTERGLOW_AUTO_PAUSE_COMPOSE_PROJECTS entry")
        result = subprocess.run(["docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"],
                                check=True, text=True, capture_output=True)
        containers = result.stdout.split()
        if containers:
            subprocess.run(["docker", "stop", "--time", "10", *containers], check=True,
                           stdout=subprocess.DEVNULL)
            print(f"Stopped {len(containers)} auto-started {project} containers; volumes untouched.", flush=True)


def compose_command(backend: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-p", PROJECT, "-f", str(backend / "docker-compose.yml"),
            "-f", str(backend / "docker-compose.host-model.yml"), "--profile", PROFILE, *args]


def wait_for_local_model(backend: Path, model: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.:/-]*", model):
        raise ValueError("SCOPEWEAVER_LLM_MODEL must name one local Ollama model")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = subprocess.run(compose_command(backend, "exec", "-T", "ollama", "ollama", "show", model),
                                cwd=backend, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        time.sleep(1)
    raise ValueError(f"local Ollama model {model} was not found or Ollama did not become ready; no model was downloaded")


def ollama_ip_and_mount(backend: Path, expected_models: Path) -> str:
    container_id = subprocess.run(compose_command(backend, "ps", "-q", "ollama"), cwd=backend,
                                  check=True, text=True, capture_output=True).stdout.strip()
    if not container_id:
        raise ValueError("the Afterglow Ollama container did not start")
    container = json.loads(subprocess.run(["docker", "inspect", container_id], check=True,
                                          text=True, capture_output=True).stdout)[0]
    if not container["State"]["Running"] or "OLLAMA_NO_CLOUD=1" not in container["Config"]["Env"]:
        raise ValueError("Ollama must be running with cloud access disabled")
    mount = next((item for item in container["Mounts"] if item["Destination"] == "/models"), None)
    if not mount or mount["RW"] or Path(mount["Source"]).resolve() != expected_models:
        raise ValueError("Ollama must read the verified host model directory read-only")
    if (container["HostConfig"].get("PortBindings") or {}).get("11434/tcp"):
        raise ValueError("Ollama port 11434 must not be published on the host")
    network = container["NetworkSettings"]["Networks"].get(PROJECT + "_default")
    if not network or not network.get("IPAddress"):
        raise ValueError("Ollama has no address on the private Afterglow network")
    return str(ipaddress.ip_address(network["IPAddress"]))


def reconcile_cidrs(configured: str, previous: str, current: str) -> str:
    entries = [part.strip() for part in configured.split(",") if part.strip()]
    entries = [part for part in entries if part != previous]
    if current not in entries:
        entries.append(current)
    return ",".join(entries)


def update_local_allowlist(backend: Path, settings: dict[str, str], address: str) -> bool:
    if settings.get("SCOPEWEAVER_LOCAL_LLM_ONLY", "false").lower() != "true":
        return False
    credential_dir = Path.home() / ".config" / "afterglow-security"
    credential_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    credential_dir.chmod(0o700)
    state = credential_dir / "managed-ollama-cidr"
    previous = state.read_text().strip() if state.is_file() else ""
    current = address + ("/32" if ":" not in address else "/128")
    value = reconcile_cidrs(settings.get("SCOPEWEAVER_LOCAL_LLM_ALLOWED_CIDRS", ""), previous, current)
    changed = update_env(backend / ".env", {"SCOPEWEAVER_LOCAL_LLM_ALLOWED_CIDRS": value})
    atomic_write(state, current + "\n")
    return changed


def request(base: str, method: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, dict]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    outgoing = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        response = OPENER.open(outgoing, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return response.status, payload if isinstance(payload, dict) else {}


def wait_for_backend(base: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            status, _ = request(base, "GET", "/api/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise ValueError("Afterglow did not become healthy within 90 seconds")


def admin_password_file() -> Path:
    directory = Path.home() / ".config" / "afterglow-security"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory / "afterglow-admin.txt"


def admin_password(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f"Afterglow admin password file is missing: {path}")
    for line in path.read_text().splitlines():
        if line.startswith("Afterglow password: "):
            return line.split(": ", 1)[1]
    raise ValueError(f"Afterglow admin password file has no password: {path}")


def ensure_gateway_token(base: str, odysseus_env: Path) -> bool:
    status, state = request(base, "GET", "/api/auth/status")
    if status != 200:
        raise ValueError(f"Afterglow authentication status returned HTTP {status}")
    username = state.get("username")
    if not isinstance(username, str) or not username:
        raise ValueError("Afterglow authentication status omitted its username")
    admin_token = ""
    if not state.get("initialized"):
        path = admin_password_file()
        if path.is_file():
            password = admin_password(path)
        else:
            password = secrets.token_urlsafe(32)
            atomic_write(path, f"Afterglow username: {username}\nAfterglow password: {password}\n")
        status, payload = request(base, "POST", "/api/auth/init", {"password": password})
        if status != 200 or not isinstance(payload.get("token"), str):
            raise ValueError(f"Afterglow admin initialization returned HTTP {status}")
        admin_token = payload["token"]
        print(f"Afterglow administrator created; password saved in {path}.", flush=True)

    existing = read_env(odysseus_env)
    token = existing.get("AFTERGLOW_TOKEN", "")
    if token:
        status, _ = request(base, "GET", "/api/stats", token=token)
        if status == 200:
            return update_env(odysseus_env, {"AFTERGLOW_URL": base, "AFTERGLOW_PUBLIC_URL": base})
        if status not in (401, 403):
            raise ValueError(f"Afterglow gateway check returned HTTP {status}")

    if not admin_token:
        password = admin_password(admin_password_file())
        status, payload = request(base, "POST", "/api/auth/login",
                                  {"username": username, "password": password})
        if status != 200 or not isinstance(payload.get("token"), str):
            raise ValueError(f"Afterglow admin login returned HTTP {status}; check the saved password")
        admin_token = payload["token"]
    status, payload = request(base, "POST", "/api/auth/gateway-token", {}, admin_token)
    token = payload.get("token")
    if status != 200 or not isinstance(token, str) or not token:
        raise ValueError(f"Afterglow gateway token request returned HTTP {status}")
    changed = update_env(odysseus_env, {
        "AFTERGLOW_URL": base, "AFTERGLOW_PUBLIC_URL": base, "AFTERGLOW_TOKEN": token,
    })
    print("Scoped Odysseus gateway token installed or renewed.", flush=True)
    return changed


def verified_native_pid(state: dict, prefix: str = "") -> int | None:
    """Identify only a recorded process from this checkout before signaling it."""
    pid = state.get(prefix + "pid")
    if not isinstance(pid, int) or pid <= 1:
        return None
    proc = Path("/proc") / str(pid)
    try:
        stat = proc.joinpath("stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z" or stat[19] != state.get(prefix + "start"):
            return None
        uid = int(next(line for line in proc.joinpath("status").read_text().splitlines()
                       if line.startswith("Uid:")).split()[1])
        if uid != os.getuid():
            return None
        args = proc.joinpath("cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        python = str(ROOT / "venv" / "bin" / "python")
        if prefix == "chroma_":
            expected = [python, str(ROOT / "venv" / "bin" / "chroma"), "run", "--host", "127.0.0.1",
                        "--port", str(state["chroma_port"]), "--path", str(ROOT / "data" / "chroma")]
        else:
            expected = [python, "-", "serve"]
        if args != expected:
            return None
        try:
            cwd = proc.joinpath("cwd").resolve(strict=True)
        except PermissionError:
            cwd = None
        if cwd is not None and cwd != ROOT:
            return None
        return pid
    except (OSError, ValueError, IndexError, KeyError, StopIteration):
        return None


def stop_native(launcher: Path) -> None:
    state_path = ROOT / "logs" / "odysseus-server.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    recorded = [(prefix, verified_native_pid(state, prefix)) for prefix in ("", "chroma_")]
    subprocess.run([str(launcher), "--stop"], cwd=ROOT, check=True)
    for prefix, pid in recorded:
        if pid is None or verified_native_pid(state, prefix) != pid:
            continue
        print(f"Stopping verified Odysseus process {pid} left by the native launcher...", flush=True)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 25
        while verified_native_pid(state, prefix) == pid and time.monotonic() < deadline:
            time.sleep(0.1)
        if verified_native_pid(state, prefix) == pid:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.2)
        if verified_native_pid(state, prefix) == pid:
            raise ValueError(f"verified Odysseus process {pid} did not stop")


def start() -> None:
    odysseus_env = ROOT / ".env"
    settings = read_env(odysseus_env)
    backend = backend_root(settings)
    launcher = ROOT / "dist" / "Odysseus"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(f"native Odysseus launcher is missing: {launcher}")
    if not (ROOT / "routes" / "cybersecurity_routes.py").is_file() or "product-mode-cybersecurity" not in (ROOT / "static" / "index.html").read_text():
        raise ValueError("this Odysseus checkout does not contain Cybersecurity mode")

    backend_settings = prepare_backend(backend, settings)
    ensure_docker(settings)
    print("Starting private Afterglow, PostgreSQL, and Ollama services...", flush=True)
    subprocess.run(compose_command(backend, "up", "-d", "--build", "postgres", "ollama", "scopeweaver"),
                   cwd=backend, check=True)
    address = ollama_ip_and_mount(backend, model_directory(backend, backend_settings, settings))
    wait_for_local_model(backend, backend_settings["SCOPEWEAVER_LLM_MODEL"])
    if update_local_allowlist(backend, read_env(backend / ".env"), address):
        subprocess.run(compose_command(backend, "up", "-d", "--no-deps", "--no-build", "--force-recreate", "scopeweaver"),
                       cwd=backend, check=True)
    port_text = read_env(backend / ".env").get("ARTEX_PORT") or "8787"
    if not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
        raise ValueError("ARTEX_PORT must be a valid TCP port")
    base = f"http://127.0.0.1:{port_text}"
    wait_for_backend(base)
    config_url = settings.get("AFTERGLOW_URL", "")
    if config_url and config_url not in (base, base.replace("127.0.0.1", "localhost")):
        raise ValueError("AFTERGLOW_URL points outside this local Afterglow stack")
    ensure_gateway_token(base, odysseus_env)
    # The native launcher deliberately rejects a second --start. Restarting
    # also loads any updated gateway settings or Cybersecurity source code.
    stop_native(launcher)
    subprocess.run([str(launcher), "--start"], cwd=ROOT, check=True)
    print(f"Cybersecurity stack ready. Open the client with odysseus; Afterglow: {base}", flush=True)


def stop() -> None:
    settings = read_env(ROOT / ".env")
    backend = backend_root(settings)
    launcher = ROOT / "dist" / "Odysseus"
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise ValueError(f"native Odysseus launcher is missing: {launcher}")
    stop_native(launcher)
    if docker_running():
        subprocess.run(compose_command(backend, "stop", "scopeweaver", "ollama", "postgres"),
                       cwd=backend, check=True)
    print("Odysseus and Afterglow servers stopped; their data is retained.", flush=True)


def main() -> int:
    if len(sys.argv) > 1:
        action = sys.argv[1]
    else:
        action = "stop" if Path(sys.argv[0]).name.endswith("-stop") else "start"
    if action not in ("start", "stop") or len(sys.argv) > 2:
        print("Usage: odysseus-cyber-start | odysseus-cyber-stop", file=sys.stderr)
        return 2
    try:
        (start if action == "start" else stop)()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"odysseus-cyber-{action}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
