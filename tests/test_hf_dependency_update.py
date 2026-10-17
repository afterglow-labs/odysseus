import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routes.cookbook_helpers import _append_pip_install_runner_lines, _venv_safe_local_pip_install_cmd
from routes.shell_routes import _package_installed_from_probe, _package_pip_update_status, _package_probe_script
from src.dependency_catalog import dependency_catalog
from src.hf_dependency_update import (
    hub_install_command, hub_install_source, hub_requirements,
    installed_hub_constraints, run_hub_install, validate_hub_pins,
)


def distribution(name, *requires):
    return SimpleNamespace(metadata={"Name": name}, requires=requires)


DEPENDENTS = [
    distribution("diffusers", "huggingface-hub>=1.23,<2.0"),
    distribution("fastembed", "huggingface-hub>=0.20,<2.0"),
    distribution("tokenizers", "huggingface-hub>=0.16.4,<2.0"),
    distribution("transformers", "huggingface-hub>=1.31,<3.0"),
    distribution("optional", 'huggingface-hub<1; extra == "test"'),
    distribution("windows", 'huggingface-hub<1; sys_platform == "win32"'),
]


def test_catalog_offers_hf_cli_update_in_selected_environment():
    row = next(p for p in dependency_catalog(local_platform="linux", target_platform="windows")
               if p["name"] == "huggingface_hub")
    assert row["target"] == "remote"
    assert row["pip"] == "huggingface_hub"
    assert row["category"] == "Tools"
    assert "hf CLI" in row["desc"]
    assert "compatible" in _package_pip_update_status(row).note or "requirements" in _package_pip_update_status(row).note


def test_constraints_combine_active_installed_requirements_only():
    constraints, parents = installed_hub_constraints(distributions=DEPENDENTS, environment={"sys_platform": "linux"})
    assert parents == ["diffusers", "fastembed", "tokenizers", "transformers"]
    assert len(constraints) == 4
    validate_hub_pins(hub_requirements(["huggingface_hub==1.33.0"]), constraints)
    with pytest.raises(ValueError, match="2.2.*conflicts.*No packages were changed"):
        validate_hub_pins(hub_requirements(["huggingface-hub==2.2"]), constraints)


def test_conflicting_pin_fails_before_pip_or_temp_files(monkeypatch):
    import src.hf_dependency_update as update
    monkeypatch.setattr(sys, "prefix", "/isolated")
    monkeypatch.setattr(update.metadata, "distributions", lambda: DEPENDENTS)
    monkeypatch.setattr(update.subprocess, "call", lambda *a, **k: pytest.fail("pip must not run"))
    monkeypatch.setattr(update.tempfile, "TemporaryDirectory", lambda **k: pytest.fail("no temp files needed"))
    with pytest.raises(ValueError, match="conflicts"):
        run_hub_install(["install", "-U", "huggingface-hub==2.2"])


def test_update_keeps_selected_python_constraints_and_removes_global_flags(monkeypatch):
    import src.hf_dependency_update as update
    monkeypatch.setattr(sys, "prefix", "/isolated")
    monkeypatch.setattr(sys, "executable", "/isolated/bin/python")
    monkeypatch.setattr(update.metadata, "distributions", lambda: DEPENDENTS)
    monkeypatch.setenv("PIP_TARGET", "/global/packages")
    monkeypatch.setenv("PIP_PREFIX", "/global")
    monkeypatch.setenv("PIP_USER", "true")
    captured = {}

    def pip(argv, *, env):
        captured.update(argv=argv, env=env, path=Path(argv[-1]))
        captured["constraints"] = captured["path"].read_text()
        return 17

    monkeypatch.setattr(update.subprocess, "call", pip)
    assert run_hub_install(["install", "-U", "--user", "--break-system-packages", "huggingface_hub"]) == 17
    assert captured["argv"][:6] == ["/isolated/bin/python", "-m", "pip", "install", "-U", "huggingface_hub"]
    assert "huggingface-hub<2.0,>=1.23" in captured["constraints"]
    assert captured["env"]["PIP_USER"] == "false"
    assert "PIP_TARGET" not in captured["env"] and "PIP_PREFIX" not in captured["env"]
    assert not captured["path"].exists()


def test_global_python_is_rejected_without_install(monkeypatch):
    monkeypatch.setattr(sys, "prefix", sys.base_prefix)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    with pytest.raises(ValueError, match="global Python will not be changed"):
        run_hub_install(["install", "-U", "huggingface_hub"])


@pytest.mark.parametrize("argument", ["--target=/global", "--prefix=/global", "--root=/global", "-t", "-t/global"])
def test_redirecting_hub_install_outside_environment_is_rejected(monkeypatch, argument):
    monkeypatch.setattr(sys, "prefix", "/isolated")
    with pytest.raises(ValueError, match="inside the selected Python"):
        run_hub_install(["install", argument, "huggingface_hub"])


@pytest.mark.parametrize("requested,expected", [("huggingface_hub", 23), ("huggingface-hub==2.2", 1)])
@pytest.mark.parametrize("bootstrap", [False, True])
def test_real_runner_uses_selected_environment_and_preserves_pip_exit(tmp_path, requested, expected, bootstrap):
    # Only this fixture interpreter runs. Its subprocess stub records pip argv;
    # no package installer or network request is ever executed.
    selected = tmp_path / "selected env" / "bin" / "python"
    selected.parent.mkdir(parents=True)
    receipt = tmp_path / "pip.json"
    selected.write_text(
        f"#!{sys.executable}\n"
        "import importlib.metadata, json, pathlib, subprocess, sys\n"
        "from types import SimpleNamespace\n"
        f"sys.executable={str(selected)!r}\n"
        f"sys.prefix={str(selected.parent.parent)!r}\n"
        "importlib.metadata.distributions=lambda: [SimpleNamespace(metadata={'Name':'diffusers'}, requires=['huggingface-hub>=1.23,<2'])]\n"
        "def fake_pip(argv, **kwargs):\n"
        f" pathlib.Path({str(receipt)!r}).write_text(json.dumps({{'argv':argv,'constraint':pathlib.Path(argv[-1]).read_text()}}))\n"
        " return 23\n"
        "subprocess.call=fake_pip\n"
        "if sys.argv[1:4] == ['-m','pip','--version']: sys.exit(0)\n"
        "assert sys.argv[1] == '-c', sys.argv\n"
        "code=sys.argv[2]; sys.argv=['-c', *sys.argv[3:]]; exec(code)\n"
    )
    selected.chmod(0o755)
    cmd = f"{shlex.quote(str(selected))} -m pip install -U --user --break-system-packages {requested}"
    lines = []
    _append_pip_install_runner_lines(lines, cmd, bootstrap_pip=bootstrap)
    result = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=15)
    assert result.returncode == expected, result.stdout + result.stderr
    if expected == 23:
        saved = json.loads(receipt.read_text())
        assert saved["argv"][0] == str(selected)
        assert "huggingface-hub<2,>=1.23" in saved["constraint"]
        assert "--user" not in saved["argv"] and "--break-system-packages" not in saved["argv"]
        assert not Path(saved["argv"][-1]).exists()
    else:
        assert not receipt.exists()
        assert "conflicts" in result.stderr


def test_native_local_recipe_pins_private_interpreter_before_guard():
    cmd = _venv_safe_local_pip_install_cmd("python3 -m pip install -U huggingface_hub", local=True,
                                         in_venv=True, executable="/odysseus/venv/bin/python")
    wrapped = shlex.split(hub_install_command(cmd))
    assert wrapped[:2] == ["/odysseus/venv/bin/python", "-c"]
    assert wrapped[3:] == ["install", "-U", "huggingface_hub"]
    assert "installed_hub_constraints" in wrapped[2]


def test_runner_rejects_global_python_before_pip_bootstrap(tmp_path):
    selected = tmp_path / "python"
    selected.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "sys.prefix=sys.base_prefix\n"
        "os.environ.pop('CONDA_PREFIX', None)\n"
        "assert sys.argv[1] == '-c', 'must stop before pip or ensurepip'\n"
        "code=sys.argv[2]; sys.argv=['-c', *sys.argv[3:]]; exec(code)\n"
    )
    selected.chmod(0o755)
    lines = []
    _append_pip_install_runner_lines(lines, f"{selected} -m pip install -U huggingface_hub", bootstrap_pip=True)
    result = subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "global Python will not be changed" in result.stderr
    assert "AssertionError" not in result.stderr


def test_powershell_runner_uses_same_guard_without_posix_quoting():
    cmd = hub_install_command("python -m pip install -U huggingface_hub", powershell=True)
    assert cmd.startswith("& 'python' '-c' '")
    assert cmd.endswith("'install' '-U' 'huggingface_hub'")
    assert "installed_hub_constraints" in cmd
    assert "'\"'\"'" not in cmd


def test_hf_readiness_requires_local_launcher_and_distribution():
    probe = {"dists": {"huggingface_hub": "1.33.0"}, "modules": {"huggingface_hub": {"real_module": True}},
             "binaries": {"hf": "/odysseus/venv/bin/hf"}}
    assert _package_installed_from_probe("huggingface_hub", probe)
    probe["binaries"]["hf"] = None
    assert not _package_installed_from_probe("huggingface_hub", probe)
    probe["binaries"]["hf"] = "/global/hf"
    probe["dists"] = {}
    assert not _package_installed_from_probe("huggingface_hub", probe)


def test_probe_does_not_use_global_hf_launcher(tmp_path, monkeypatch, capsys):
    launcher = tmp_path / "bin" / "hf"
    launcher.parent.mkdir()
    monkeypatch.setattr(sys, "executable", str(launcher.parent / "python"))
    monkeypatch.setattr("shutil.which", lambda name: "/global/bin/" + name)
    exec(_package_probe_script(["huggingface_hub"]), {})
    probe = json.loads(capsys.readouterr().out)["huggingface_hub"]
    assert probe["binaries"]["hf"] is None
    launcher.touch()
    exec(_package_probe_script(["huggingface_hub"]), {})
    probe = json.loads(capsys.readouterr().out)["huggingface_hub"]
    assert probe["binaries"]["hf"] == str(launcher)
    assert probe["python_executable"] == str(launcher.parent / "python")


@pytest.mark.asyncio
async def test_legacy_install_api_uses_same_constraint_guard(monkeypatch):
    import routes.shell_routes as routes
    monkeypatch.setattr(routes, "_require_admin", lambda request: None)
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"fixture", b"")))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)
    endpoint = next(route.endpoint for route in routes.setup_shell_routes().routes
                    if route.path == "/api/cookbook/packages/install")
    result = await endpoint(SimpleNamespace(json=AsyncMock(return_value={"pip": "huggingface_hub", "upgrade": True})))
    assert result["ok"]
    argv = execute.call_args_list[-1].args
    assert argv[:2] == (sys.executable, "-c")
    assert "installed_hub_constraints" in argv[2]
    assert argv[3:] == ("install", "--upgrade", "huggingface_hub")
