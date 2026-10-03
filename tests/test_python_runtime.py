"""The shared runtime contract must fail before installs or application imports."""

from collections import namedtuple
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from src import python_runtime


REPO = Path(__file__).resolve().parent.parent
VersionInfo = namedtuple("VersionInfo", "major minor micro releaselevel serial")


def _runtime(monkeypatch, *, version=(3, 14), release="final", implementation="cpython", free_threaded=0):
    monkeypatch.setattr(sys, "version_info", VersionInfo(*version, 7, release, 0))
    monkeypatch.setattr(sys.implementation, "name", implementation)
    monkeypatch.setattr(python_runtime.sysconfig, "get_config_var", lambda key: free_threaded)


def test_accepts_supported_standard_cpython(monkeypatch):
    _runtime(monkeypatch, version=python_runtime.required_python_version())
    python_runtime.require_supported_python()


@pytest.mark.parametrize("options", [
    {"version": (3, 9)},
    {"version": (3, 13)},
    {"version": (3, 15)},
    {"release": "candidate"},
    {"implementation": "pypy"},
    {"free_threaded": 1},
])
def test_rejects_unsupported_runtime_with_repair_guidance(monkeypatch, options):
    _runtime(monkeypatch, **options)
    with pytest.raises(SystemExit) as error:
        python_runtime.require_supported_python()
    message = str(error.value)
    assert "standard GIL build" in message
    assert sys.executable in message
    assert "move any existing venv aside" in message
    assert "requirements.lock" in message


def test_frozen_runtime_reads_bundled_version(tmp_path, monkeypatch):
    (tmp_path / ".python-version").write_text("3.14\n", encoding="utf-8")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert python_runtime.required_python_version() == (3, 14)


def test_missing_version_file_has_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    with pytest.raises(SystemExit, match="Restore .python-version"):
        python_runtime.required_python_version()


def test_rosetta_runtime_is_rejected(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(python_runtime.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(python_runtime.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "1\n"))
    with pytest.raises(SystemExit, match="native arm64 Python"):
        python_runtime.require_native_architecture()


@pytest.mark.parametrize("entrypoint", ["setup.py", "launcher.py", "app.py"])
def test_entrypoint_rejects_old_python_before_application_imports(entrypoint):
    code = """
import runpy
import sys
class BlockApplicationImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'core', 'app', 'dotenv', 'uvicorn', 'tkinter'}:
            raise AssertionError('Application imported before runtime check: ' + fullname)
sys.meta_path.insert(0, BlockApplicationImports())
sys.version_info = (3, 9, 0, 'final', 0)
runpy.run_path(sys.argv[1], run_name='__main__')
"""
    result = subprocess.run([sys.executable, "-S", "-c", code, entrypoint], cwd=REPO, text=True, capture_output=True)
    assert result.returncode != 0
    assert "Odysseus requires stable CPython" in result.stderr
    assert "Application imported before runtime check" not in result.stderr


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="POSIX launcher checks")
@pytest.mark.parametrize("script", ["start-macos.sh", "build-macos-app.sh", "install-service.sh"])
@pytest.mark.parametrize("compatible", [True, False])
def test_shell_check_mode_validates_existing_venv_without_side_effects(tmp_path, script, compatible):
    shutil.copy2(REPO / script, tmp_path / script)
    shutil.copy2(REPO / ".python-version", tmp_path / ".python-version")
    (tmp_path / "src").mkdir()
    shutil.copy2(REPO / "src/python_runtime.py", tmp_path / "src/python_runtime.py")
    python_path = tmp_path / "venv/bin/python"
    python_path.parent.mkdir(parents=True)
    if compatible:
        python_path.symlink_to(sys.executable)
    else:
        python_path.write_text("#!/bin/sh\necho 'Old Python environment' >&2\nexit 1\n", encoding="utf-8")
        python_path.chmod(0o755)
    before = set(tmp_path.rglob("*"))
    result = subprocess.run(["bash", str(tmp_path / script), "--check-python"], cwd=tmp_path, text=True, capture_output=True)
    assert (result.returncode == 0) == compatible, result.stdout + result.stderr
    assert set(tmp_path.rglob("*")) == before
    assert "Using CPython" in result.stdout if compatible else "Old Python environment" in result.stderr


@pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="POSIX launcher checks")
def test_macos_launcher_bootstraps_missing_pip_without_replacing_venv(tmp_path):
    shutil.copy2(REPO / "start-macos.sh", tmp_path / "start-macos.sh")
    shutil.copy2(REPO / ".python-version", tmp_path / ".python-version")
    (tmp_path / "requirements.lock").write_text("# Test lock\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("brew", "tmux", "llama-server", "apfel"):
        command = bin_dir / name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    python_path = tmp_path / "venv/bin/python"
    python_path.parent.mkdir(parents=True)
    python_path.write_text("""#!/bin/sh
echo "$*" >> "$STUB_CALLS"
case "$*" in
  "-m pip --version") test -f "$PIP_READY" ;;
  "-m ensurepip --upgrade") touch "$PIP_READY" ;;
  "-m pip show chromadb-client") exit 1 ;;
  "setup.py") echo "Reached setup after dependency installation"; exit 37 ;;
  *) exit 0 ;;
esac
""", encoding="utf-8")
    python_path.chmod(0o755)
    sentinel = tmp_path / "venv/preserve-me"
    sentinel.write_text("existing environment", encoding="utf-8")
    calls = tmp_path / "python-calls"
    environment = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                       STUB_CALLS=str(calls), PIP_READY=str(tmp_path / "pip-ready"), ODYSSEUS_PORT="0")
    result = subprocess.run(["bash", str(tmp_path / "start-macos.sh")], cwd=tmp_path,
                            env=environment, text=True, capture_output=True)
    assert "Reached setup after dependency installation" in result.stdout, result.stdout + result.stderr
    assert sentinel.read_text(encoding="utf-8") == "existing environment"
    actions = calls.read_text(encoding="utf-8")
    assert actions.index("-m ensurepip --upgrade") < actions.index("-m pip install")
    assert "--require-hashes -r requirements.lock" in actions
    assert "-m venv" not in actions


@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell launcher checks")
@pytest.mark.parametrize("script", ["launch-windows.ps1", "build-windows-portable.ps1"])
@pytest.mark.parametrize("compatible", [True, False])
def test_windows_check_mode_validates_venv_without_installing(tmp_path, script, compatible):
    shutil.copy2(REPO / script, tmp_path / script)
    (tmp_path / ".python-version").write_text("3.14\n" if compatible else "3.13\n", encoding="utf-8")
    for relative in ("src/python_runtime.py", "scripts/_lib/python-runtime.ps1"):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / relative, destination)
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(tmp_path / "venv")], check=True)
    before = set(tmp_path.rglob("*"))
    result = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(tmp_path / script), "-CheckPython"], cwd=tmp_path, text=True, capture_output=True)
    assert (result.returncode == 0) == compatible, result.stdout + result.stderr
    assert set(tmp_path.rglob("*")) == before
    assert "Using CPython" in result.stdout if compatible else "Move it aside" in result.stdout
