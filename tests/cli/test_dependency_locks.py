"""Lock checks are read-only and insensitive to checkout line endings."""

from pathlib import Path
import subprocess
import sys

import pytest

from tests.helpers.cli_loader import load_script


@pytest.mark.parametrize("changed", [False, True])
def test_check_normalizes_newlines_but_detects_dependency_changes(tmp_path, monkeypatch, changed):
    module = load_script("lock_dependencies.py")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module.shutil, "which", lambda executable: "uv")
    monkeypatch.setattr(sys, "argv", ["lock_dependencies.py", "--check"])
    (tmp_path / ".python-version").write_text("3.14\n")
    original = b"example==1.0\r\n"
    locks = [tmp_path / name for name in (
        "requirements.lock", "requirements-optional.lock", "requirements-build.lock",
    )]
    for lock in locks:
        lock.write_bytes(original)

    def compile_lock(command, cwd):
        output = Path(command[command.index("--output-file") + 1])
        output.write_bytes(b"example==2.0\n" if changed else b"example==1.0\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(module.subprocess, "run", compile_lock)
    assert module.main() == (1 if changed else 0)
    assert all(lock.read_bytes() == original for lock in locks)
