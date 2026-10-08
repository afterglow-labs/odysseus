"""SGLang's managed recipe must not resolve old wheels in the app's Python."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from routes.cookbook_helpers import (
    _append_pip_install_runner_lines,
    _append_serve_exit_code_lines,
)
from src.sglang_runtime import (
    SGLANG_REQUIREMENT,
    is_sglang_install_cmd,
    is_sglang_launch_cmd,
    managed_sglang_venv,
    normalize_sglang_install_cmd,
    sglang_python_compatibility_error,
)


def test_managed_runtime_stays_inside_project_and_does_not_create_it(monkeypatch, tmp_path):
    import src.sglang_runtime as runtime
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "src" / "sglang_runtime.py"))
    assert managed_sglang_venv() == tmp_path / ".venvs" / "sglang"
    assert not managed_sglang_venv().exists()


@pytest.mark.parametrize("requirement", ["sglang", "sglang[all]", "SGLang[ALL]"])
def test_unpinned_sglang_uses_current_binary_recipe(requirement):
    command = normalize_sglang_install_cmd(f"python3 -m pip install {shlex.quote(requirement)}")
    assert shlex.split(command) == [
        "python3", "-m", "pip", "install", SGLANG_REQUIREMENT, "--pre", "--only-binary=sglang",
    ]
    assert normalize_sglang_install_cmd(command) == command


@pytest.mark.parametrize("requirement,expected", [
    ("sglang[all]==0.5.21", "sglang==0.5.21"),
    ("sglang[all,diffusion]>=0.5.21,<0.6", "sglang[diffusion]>=0.5.21,<0.6"),
    ("sglang~=0.5.21", "sglang~=0.5.21"),
    ("sglang[all]==0.5.4.post2", "sglang==0.5.4.post2"),
    ('sglang[all]; python_version >= "3.12"', 'sglang>=0.5.21; python_version >= "3.12"'),
])
def test_explicit_constraints_other_extras_and_markers_survive(requirement, expected):
    command = normalize_sglang_install_cmd("python3 -m pip install " + shlex.quote(requirement))
    assert expected in shlex.split(command)


def test_normalization_preserves_selected_python_and_unrelated_url_text():
    prefix = 'CUDA_HOME=/opt/cuda "$HOME/selected venv/bin/python3" -m pip install '
    suffix = '--extra-index-url "https://example.invalid/sglang[all]/wheels" "other[all]>=2"'
    normalized = normalize_sglang_install_cmd(prefix + '"sglang[all]" ' + suffix)
    assert normalized.startswith(prefix)
    assert suffix in normalized
    assert is_sglang_install_cmd(normalized)


@pytest.mark.parametrize("command", [
    'python3 -m pip install other --extra-index-url "https://example.invalid/sglang[all]"',
    "python3 -m pip install --index-url sglang other",
    "python3 -m pip install sglang-kernel",
    "python3 -m pip install sglang_helpers",
    "python3 -m pip install 'sglang @ https://example.invalid/sglang.whl'",
    "python3 -m pip install sglang @ https://example.invalid/sglang.whl",
    "python3 -c 'print(\"sglang\")'",
    "echo python3 -m pip install sglang",
    "python3 -m pip install sglang && echo done",
    "python3 -m pip install sglang\necho done",
    '"$(echo python3)" -m pip install sglang',
    "python3 -m pip install 'sglang",
])
def test_non_recipe_text_and_compound_commands_are_unchanged(command):
    assert not is_sglang_install_cmd(command)
    assert normalize_sglang_install_cmd(command) == command


def test_pip_delimiter_keeps_flags_before_requirements():
    normalized = normalize_sglang_install_cmd("python3 -m pip install -- 'sglang[all]' other")
    assert shlex.split(normalized) == [
        "python3", "-m", "pip", "install", "--pre", "--only-binary=sglang", "--",
        SGLANG_REQUIREMENT, "other",
    ]


@pytest.mark.parametrize("command", [
    "python3 -m sglang.launch_server --model-path example/model",
    "'/selected environment/bin/python3' -u -m sglang.launch_server --port 30000",
    "CUDA_VISIBLE_DEVICES=1 python3.12 -m sglang.launch_server",
    "/selected/bin/sglang serve --model-path example/model",
])
def test_recognize_real_sglang_launches(command):
    assert is_sglang_launch_cmd(command)
    assert not is_sglang_install_cmd(command)


@pytest.mark.parametrize("command", [
    "python3 -m llama_cpp.server --model sglang.launch_server",
    "python3 -c 'print(\"sglang.launch_server\")'",
    "python3 -m sglang.launch_server_extra",
    "sglang --help",
    "echo python3 -m sglang.launch_server",
])
def test_sglang_words_are_not_launches(command):
    assert not is_sglang_launch_cmd(command)


@pytest.mark.parametrize("version,compatible", [
    ((3, 9), False), ((3, 10), True), ((3, 12), True), ((3, 13), True), ((3, 14), False),
])
def test_supported_python_boundaries(version, compatible):
    error = sglang_python_compatibility_error(version, "/selected/bin/python")
    assert bool(error) is not compatible
    if error:
        assert "/selected/bin/python" in error
        assert "Python 3.12 venv" in error


@pytest.mark.parametrize("version,pip_exit,expected", [
    ((3, 14, 6), 0, 78), ((3, 9, 9), 0, 78),
    ((3, 12, 9), 0, 0), ((3, 13, 9), 31, 31),
])
@pytest.mark.parametrize("bootstrap", [False, True])
def test_runner_checks_selected_python_before_pip_and_keeps_exit_status(
    tmp_path, version, pip_exit, expected, bootstrap,
):
    # Executable fixture models a remote/different Python. The current test
    # interpreter's version must never decide whether this install runs.
    selected = tmp_path / "selected environment" / "bin" / "python3"
    selected.parent.mkdir(parents=True)
    calls = tmp_path / "pip-calls.jsonl"
    selected.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"sys.version_info = {version!r}\n"
        f"sys.executable = {str(selected)!r}\n"
        "if sys.argv[1] == '-c':\n"
        "    exec(sys.argv[2])\n"
        "else:\n"
        f"    with pathlib.Path({str(calls)!r}).open('a') as stream:\n"
        "        stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"    sys.exit({pip_exit} if 'install' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    selected.chmod(0o755)
    lines = []
    _append_pip_install_runner_lines(
        lines, f"{shlex.quote(str(selected))} -m pip install 'sglang[all]'",
        bootstrap_pip=bootstrap,
    )
    _append_serve_exit_code_lines(lines, keep_shell_open=False, is_pip_install=True)
    process = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=15)
    assert process.returncode == expected, process.stdout + process.stderr
    assert f"=== Process exited with code {expected} ===" in process.stdout
    assert ("DOWNLOAD_OK" in process.stdout) is (expected == 0)
    if expected == 78:
        assert not calls.exists(), "Unsupported Python must stop before even bootstrapping pip"
        assert str(selected) in process.stderr
        assert "Python 3.12 venv" in process.stderr
    else:
        requests = [json.loads(line) for line in calls.read_text().splitlines()]
        installs = [args for args in requests if "install" in args]
        assert installs == [["-m", "pip", "install", SGLANG_REQUIREMENT, "--pre", "--only-binary=sglang"]]
        assert str(selected) in process.stdout


def test_unrelated_pip_runner_remains_unchanged():
    lines = []
    command = "python3 -m pip install huggingface_hub"
    _append_pip_install_runner_lines(lines, command)
    assert lines == [command]
