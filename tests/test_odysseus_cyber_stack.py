"""Focused checks for the native two-server Cybersecurity launcher."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "script" / "odysseus_cyber_stack.py"
assert SCRIPT.is_file()
SPEC = importlib.util.spec_from_file_location("odysseus_cyber_stack", SCRIPT)
assert SPEC and SPEC.loader
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)


def test_update_env_preserves_other_settings_and_protects_credentials(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# personal setting\nAPP_PORT=7860\nAFTERGLOW_TOKEN=expired\n")
    assert stack.update_env(path, {"AFTERGLOW_TOKEN": "new-token", "AFTERGLOW_URL": "http://127.0.0.1:8787"})
    assert not stack.update_env(path, {"AFTERGLOW_TOKEN": "new-token", "AFTERGLOW_URL": "http://127.0.0.1:8787"})
    assert "# personal setting\nAPP_PORT=7860\n" in path.read_text()
    assert stack.read_env(path)["AFTERGLOW_TOKEN"] == "new-token"
    assert path.stat().st_mode & 0o777 == 0o600


def test_prepare_backend_uses_existing_model_without_downloading(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env.example").write_text("POSTGRES_PASSWORD=\nSCOPEWEAVER_LOCAL_LLM_ONLY=false\n")
    models = tmp_path / "models"
    (models / "blobs").mkdir(parents=True)
    (models / "manifests").mkdir()
    monkeypatch.setenv("OLLAMA_MODELS_DIR", str(models))
    settings = stack.prepare_backend(backend, {})
    assert settings["POSTGRES_PASSWORD"]
    assert settings["OLLAMA_MODELS_DIR"] == str(models)
    assert settings["SCOPEWEAVER_LOCAL_LLM_ONLY"] == "true"
    assert settings["SCOPEWEAVER_LLM_MODEL"] == "qwen3:32b"
    assert (backend / ".env").stat().st_mode & 0o777 == 0o600
    assert (backend / "private" / "c2-binaries").is_dir()
    stack.update_env(backend / ".env", {"SCOPEWEAVER_LOCAL_LLM_ONLY": "false", "SCOPEWEAVER_LLM_BASE_URL": "https://other.example"})
    updated = stack.prepare_backend(backend, {})
    assert updated["SCOPEWEAVER_LOCAL_LLM_ONLY"] == "true"
    assert updated["SCOPEWEAVER_LLM_BASE_URL"] == "http://ollama:11434"


def test_model_cidr_replacement_preserves_other_rules():
    assert stack.reconcile_cidrs("10.0.0.0/8,172.19.0.2/32", "172.19.0.2/32", "172.20.0.4/32") == "10.0.0.0/8,172.20.0.4/32"
    assert stack.reconcile_cidrs("172.20.0.4/32", "", "172.20.0.4/32") == "172.20.0.4/32"


def test_expired_gateway_token_is_renewed_with_afterglow_login(tmp_path, monkeypatch):
    odysseus_env = tmp_path / ".env"
    odysseus_env.write_text("APP_PORT=7860\nAFTERGLOW_TOKEN=expired\n")
    credential = tmp_path / "afterglow-admin.txt"
    credential.write_text("Afterglow username: Afterglow\nAfterglow password: private-password\n")
    monkeypatch.setattr(stack, "admin_password_file", lambda: credential)
    calls = []

    def request(base, method, path, body=None, token=""):
        calls.append((method, path, body, token))
        if path == "/api/auth/status":
            return 200, {"initialized": True, "username": "Afterglow"}
        if path == "/api/stats":
            return 401, {}
        if path == "/api/auth/login":
            return 200, {"token": "admin-token"}
        if path == "/api/auth/gateway-token":
            return 200, {"token": "scoped-token"}
        raise AssertionError(path)

    monkeypatch.setattr(stack, "request", request)
    assert stack.ensure_gateway_token("http://127.0.0.1:8787", odysseus_env)
    assert ("POST", "/api/auth/login", {"username": "Afterglow", "password": "private-password"}, "") in calls
    assert stack.read_env(odysseus_env)["AFTERGLOW_TOKEN"] == "scoped-token"
    assert "private-password" not in odysseus_env.read_text()


def test_native_stop_fallback_signals_only_a_verified_recorded_process(tmp_path, monkeypatch):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "odysseus-server.json").write_text('{"pid":123,"start":"recorded"}')
    monkeypatch.setattr(stack, "ROOT", tmp_path)
    running = [True]
    monkeypatch.setattr(stack, "verified_native_pid", lambda state, prefix="": 123 if prefix == "" and running[0] else None)
    launched = []
    monkeypatch.setattr(stack.subprocess, "run", lambda command, **kwargs: launched.append(command))
    signals = []

    def signal_process(pid, sig):
        signals.append((pid, sig))
        running[0] = False

    monkeypatch.setattr(stack.os, "kill", signal_process)
    stack.stop_native(tmp_path / "dist" / "Odysseus")
    assert launched == [[str(tmp_path / "dist" / "Odysseus"), "--stop"]]
    assert signals == [(123, stack.signal.SIGTERM)]


def test_docker_socket_activation_pauses_only_configured_auto_started_project(monkeypatch):
    monkeypatch.setattr(stack.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(stack, "docker_running", lambda: True)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[:2] == ["systemctl", "is-active"]:
            return SimpleNamespace(returncode=3)
        if command[:2] == ["docker", "ps"]:
            return SimpleNamespace(returncode=0, stdout="cvat-one\ncvat-two\n")
        if command[:2] == ["docker", "stop"]:
            return SimpleNamespace(returncode=0)
        raise AssertionError(command)

    monkeypatch.setattr(stack.subprocess, "run", run)
    stack.ensure_docker({"AFTERGLOW_AUTO_PAUSE_COMPOSE_PROJECTS": "cvat"})
    assert ["docker", "stop", "--time", "10", "cvat-one", "cvat-two"] in calls
    assert not any(command[:4] == ["sudo", "-n", "systemctl", "start"] for command in calls)


def test_start_keeps_existing_odysseus_process_running(tmp_path, monkeypatch):
    backend = tmp_path / "backend"
    backend.mkdir()
    (backend / ".env").write_text("ARTEX_PORT=8787\n")
    launcher = tmp_path / "dist" / "Odysseus"
    launcher.parent.mkdir()
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    route = tmp_path / "routes" / "cybersecurity_routes.py"
    route.parent.mkdir()
    route.write_text("")
    page = tmp_path / "static" / "index.html"
    page.parent.mkdir()
    page.write_text("product-mode-cybersecurity")
    monkeypatch.setattr(stack, "ROOT", tmp_path)
    monkeypatch.setattr(stack, "backend_root", lambda settings: backend)
    monkeypatch.setattr(stack, "prepare_backend", lambda backend, settings: {"SCOPEWEAVER_LLM_MODEL": "qwen3:32b"})
    monkeypatch.setattr(stack, "ensure_docker", lambda settings: None)
    monkeypatch.setattr(stack, "compose_command", lambda backend, *args: ["docker", "compose", *args])
    monkeypatch.setattr(stack, "model_directory", lambda *args: tmp_path / "models")
    monkeypatch.setattr(stack, "ollama_ip_and_mount", lambda *args: "172.20.0.2")
    monkeypatch.setattr(stack, "wait_for_local_model", lambda *args: None)
    monkeypatch.setattr(stack, "update_local_allowlist", lambda *args: False)
    monkeypatch.setattr(stack, "wait_for_backend", lambda base: None)
    monkeypatch.setattr(stack, "ensure_gateway_token", lambda *args: True)
    monkeypatch.setattr(stack, "stop_native", lambda launcher: (_ for _ in ()).throw(AssertionError("server was stopped")))
    calls = []
    monkeypatch.setattr(stack.subprocess, "run", lambda command, **kwargs: calls.append(command) or SimpleNamespace(returncode=0))

    stack.start()

    assert calls == [
        ["docker", "compose", "up", "-d", "--build", "postgres", "ollama", "scopeweaver"],
        [str(launcher), "--start"],
    ]
