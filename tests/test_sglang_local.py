"""Default local SGLang installs and launches use one isolated interpreter."""
import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from routes.cookbook_helpers import (
    _append_serve_exit_code_lines,
    _append_serve_preflight_exit_lines,
    _venv_safe_local_pip_install_cmd,
)
from src import sglang_local


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    path = tmp_path / "Odysseus checkout" / ".venvs" / "sglang"
    monkeypatch.setattr(sglang_local, "managed_sglang_venv", lambda: path)
    return path


@pytest.mark.parametrize("binary", ["python", "python3"])
def test_default_install_and_launch_select_same_private_python_before_it_exists(runtime, binary):
    install, managed_install = sglang_local.local_sglang_command(
        f'{binary} -m pip install -U "sglang>=0.5.21"', platform="linux",
    )
    launch, managed_launch = sglang_local.local_sglang_command(
        f"{binary} -m sglang.launch_server --model-path example/model", platform="linux",
    )
    assert managed_install and managed_launch
    assert shlex.split(install)[0] == shlex.split(launch)[0] == str(runtime / "bin/python")
    assert not runtime.exists(), "Command selection must not create an environment"
    # The later generic installer normalizer must not replace SGLang's private
    # Python with the app's Python after SGLang selection has happened.
    generic = _venv_safe_local_pip_install_cmd(
        install, local=True, in_venv=True, executable="/app/venv/bin/python3",
    )
    assert shlex.split(generic)[0] == str(runtime / "bin/python")


@pytest.mark.parametrize("options", [
    {"remote_host": "linux-box"},
    {"env_prefix": "source '/selected venv/bin/activate'"},
    {"env_prefix": "conda activate inference"},
    {"platform": "darwin"},
    {"platform": "win32"},
])
@pytest.mark.parametrize("command", [
    'python3 -m pip install "sglang>=0.5.21"',
    "python3 -m sglang.launch_server --model-path example/model",
])
def test_selected_environment_remote_and_unsupported_platform_are_not_redirected(runtime, options, command):
    assert sglang_local.local_sglang_command(command, **options) == (command, False)


@pytest.mark.parametrize("command", [
    "'/explicit venv/bin/python3' -m pip install sglang",
    "'/explicit venv/bin/python3' -m sglang.launch_server --model-path example/model",
    "python3.12 -m pip install sglang",
    "python3.12 -m sglang.launch_server --model-path example/model",
    "./venv/bin/python -m pip install sglang",
    "python3 -m pip install vllm",
    "python3 -m pip install sglang-kernel",
    "python3 scripts/h3_video_worker.py",
    "python3 -m llama_cpp.server --model sglang.launch_server",
    "python3 -m pip install sglang && echo done",
    'python3 -c "print(\"sglang\")"',
])
def test_explicit_interpreter_and_unrelated_commands_are_unchanged(runtime, command):
    assert sglang_local.local_sglang_command(command, platform="linux") == (command, False)


def test_redirect_preserves_environment_and_model_argument_shell_semantics(runtime):
    prefix = 'CUDA_VISIBLE_DEVICES=1 MODEL_LABEL="same video model" '
    arguments = ' -u -m sglang.launch_server --model-path "$HOME/models/Qwen custom" --served-model-name \'Corey\' --port 8012'
    original = prefix + "python3" + arguments
    command, managed = sglang_local.local_sglang_command(original, platform="linux")
    assert managed
    assert command == prefix + shlex.quote(str(runtime / "bin/python")) + arguments


@pytest.mark.parametrize("setup_exit", [0, 23, 78])
def test_failed_managed_environment_setup_stops_task_before_package_install(runtime, tmp_path, setup_exit):
    calls = tmp_path / "setup-arguments.json"
    app_python = tmp_path / "application python"
    app_python.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"pathlib.Path({str(calls)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"sys.exit({setup_exit})\n",
        encoding="utf-8",
    )
    app_python.chmod(0o755)
    lines = ['ODYSSEUS_PREFLIGHT_EXIT=""']
    sglang_local.append_sglang_prepare(lines, app_python=str(app_python))
    _append_serve_preflight_exit_lines(lines, keep_shell_open=False)
    lines.append("printf '%s\\n' PACKAGE_INSTALL_REACHED")
    _append_serve_exit_code_lines(lines, keep_shell_open=False, is_pip_install=True)
    process = subprocess.run(
        ["bash", "-c", "\n".join(lines)], text=True, capture_output=True, timeout=15,
    )
    assert process.returncode == setup_exit, process.stdout + process.stderr
    assert json.loads(calls.read_text()) == [
        str(runtime.parents[1] / "scripts/setup_sglang_runtime.py"), "--prepare-only",
    ]
    assert ("PACKAGE_INSTALL_REACHED" in process.stdout) is (setup_exit == 0)
    assert ("DOWNLOAD_OK" in process.stdout) is (setup_exit == 0)
    assert f"=== Process exited with code {setup_exit} ===" in process.stdout
