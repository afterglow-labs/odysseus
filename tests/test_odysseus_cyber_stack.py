"""Legacy Cybersecurity shortcuts delegate only to the independent APTEX app."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "script" / "odysseus_cyber_stack.py"
SPEC = importlib.util.spec_from_file_location("aptex_shortcuts", SCRIPT)
stack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stack)


def test_configured_checkout_resolves_relative_to_odysseus(tmp_path, monkeypatch):
    odysseus = tmp_path / "Odysseus checkout"
    backend = odysseus / "separate APTEX"
    (backend / "scripts").mkdir(parents=True)
    (backend / "scripts" / "aptex.py").write_text("")
    (backend / "docker-compose.yml").write_text("")
    (odysseus / ".env").write_text('APTEX_ROOT="separate APTEX"\n')
    monkeypatch.setattr(stack, "ROOT", odysseus)
    monkeypatch.delenv("APTEX_ROOT", raising=False)
    monkeypatch.delenv("AFTERGLOW_COMPOSE_ROOT", raising=False)
    assert stack.backend_root() == backend


@pytest.mark.parametrize("name,action", [("odysseus-cyber-start", "start"), ("odysseus-cyber-stop", "stop")])
def test_legacy_shortcut_executes_only_aptex(tmp_path, monkeypatch, name, action):
    calls = []
    monkeypatch.setattr(stack, "backend_root", lambda: tmp_path)
    monkeypatch.setattr(stack.sys, "argv", [name])
    monkeypatch.setattr(stack.os, "execv", lambda executable, args: calls.append((executable, args)))
    assert stack.main() == 0
    assert calls == [(stack.sys.executable, [stack.sys.executable, str(tmp_path / "scripts" / "aptex.py"), action])]
