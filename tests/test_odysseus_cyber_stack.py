"""Focused checks for the native two-server Cybersecurity launcher."""

import importlib.util
from pathlib import Path


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
