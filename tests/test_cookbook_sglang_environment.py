"""Serving dependencies are inspected and installed in their selected Python."""

from copy import deepcopy
import importlib.metadata
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

import routes.shell_routes as routes


def _python(root):
    executable = root / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    return executable


def _probe(name, version):
    return {
        "dists": {name: version},
        "health": {"installed": True, "versions": {name: version}, "issues": []},
    }


@pytest.fixture
def package_environment(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    packages = [
        {"name": "sglang", "pip": routes.SGLANG_REQUIREMENT, "target": "remote"},
        {"name": "engine", "pip": "engine", "target": "remote"},
        {"name": "app_tool", "pip": "app-tool", "target": "local"},
    ]
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    monkeypatch.setattr(routes, "managed_sglang_venv", lambda: managed)
    monkeypatch.setattr(routes, "_require_admin", lambda request: None)
    monkeypatch.setattr(routes, "_reject_cross_site", lambda request: None)
    monkeypatch.setattr(routes, "_prepend_user_install_bins_to_path", lambda: None)
    monkeypatch.setattr(routes, "dependency_catalog", lambda **kwargs: deepcopy(packages))
    monkeypatch.setattr(routes, "_import_optional_dependency_for_status", lambda name: None)
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.5.21" if name == "sglang" else "1")
    monkeypatch.setattr(importlib.metadata, "requires", lambda name: [])
    monkeypatch.setattr("site.getusersitepackages", lambda: "")
    monkeypatch.setattr(routes.shutil, "which", lambda name: None)
    endpoint = next(route.endpoint for route in routes.setup_shell_routes().routes
                    if route.path == "/api/cookbook/packages")

    async def get(**kwargs):
        result = await endpoint(SimpleNamespace(), **kwargs)
        return {package["name"]: package for package in result["packages"]}

    return managed, get


def _mock_process(monkeypatch, payload):
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(payload).encode(), b"")))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)
    return execute


def _mock_index(monkeypatch):
    checks = []

    def check(name, **kwargs):
        checks.append((name, kwargs))
        return {"latest_version": "0.5.21", "install_supported": True, "has_compatible_artifact": True}

    monkeypatch.setattr(routes, "check_latest_release", check)
    return checks


ENVIRONMENT = {"python_version": "3.12.10", "supported_tags": ["cp312-cp312-manylinux_2_34_x86_64"]}


@pytest.mark.asyncio
async def test_managed_sglang_status_uses_its_python_without_changing_other_features(package_environment, monkeypatch):
    managed, get = package_environment
    python = _python(managed)
    execute = _mock_process(monkeypatch, {"sglang": _probe("sglang", "0.5.22"), "_environment": ENVIRONMENT})
    checks = _mock_index(monkeypatch)

    rows = await get(check_index=True)

    assert rows["sglang"]["installed"] is True
    assert rows["sglang"]["dependency_health"]["versions"] == {"sglang": "0.5.22"}
    assert rows["sglang"]["python_executable"] == str(python)
    assert rows["sglang"]["managed_environment"] is True
    assert rows["engine"]["dependency_health"]["versions"] == {"engine": "1"}
    assert rows["app_tool"]["dependency_health"]["versions"] == {"app-tool": "1"}
    assert ("sglang", ENVIRONMENT) in checks
    assert ("engine", {}) in checks
    assert ("app-tool", {}) in checks
    assert execute.await_count == 1
    assert execute.call_args.args[:3] == (str(python), "-I", "-c")


@pytest.mark.asyncio
async def test_explicit_local_venv_wins_over_managed_and_does_not_use_shell(package_environment, monkeypatch, tmp_path):
    managed, get = package_environment
    _python(managed)
    # Spaces and shell syntax are harmless filesystem characters when passed
    # as a single executable argument, without activation or shell parsing.
    selected = tmp_path / "selected env;echo wrong"
    python = _python(selected)
    monkeypatch.setenv("PYTHONPATH", "/wrong/app/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/wrong/app/python")
    execute = _mock_process(monkeypatch, {
        "sglang": _probe("sglang", "0.5.23"), "engine": _probe("engine", "2"), "_environment": ENVIRONMENT,
    })
    checks = _mock_index(monkeypatch)

    rows = await get(venv=str(selected / "bin" / "activate"), check_index=True)

    assert execute.call_args.args[0] == str(python)
    assert execute.call_args.kwargs["env"]["PATH"].split(":")[0] == str(python.parent)
    assert "PYTHONPATH" not in execute.call_args.kwargs["env"]
    assert "PYTHONHOME" not in execute.call_args.kwargs["env"]
    assert rows["sglang"]["managed_environment"] is False
    assert rows["engine"]["dependency_health"]["versions"] == {"engine": "2"}
    assert rows["app_tool"]["dependency_health"]["versions"] == {"app-tool": "1"}
    assert ("sglang", ENVIRONMENT) in checks
    assert ("engine", ENVIRONMENT) in checks
    assert ("app-tool", {}) in checks


@pytest.mark.asyncio
async def test_missing_selected_venv_never_falls_back_to_app_python(package_environment, monkeypatch, tmp_path):
    managed, get = package_environment
    _python(managed)
    execute = AsyncMock(side_effect=AssertionError("A missing environment must not run a Python probe"))
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)
    checks = _mock_index(monkeypatch)

    rows = await get(venv=str(tmp_path / "missing"), check_index=True)

    for name in ["sglang", "engine"]:
        assert rows[name]["installed"] is None
        assert "no interpreter" in rows[name]["probe_error"]
        assert "unknown" in rows[name]["compatibility_note"].lower()
        assert "dependency_health" not in rows[name]
    assert rows["app_tool"]["installed"] is True
    assert checks == [("app-tool", {})]
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_managed_environment_tags_do_not_use_app_tags(package_environment, monkeypatch):
    managed, get = package_environment
    _python(managed)
    _mock_process(monkeypatch, {"sglang": _probe("sglang", "0.5.21")})
    checks = _mock_index(monkeypatch)
    rows = await get(check_index=True)
    assert rows["sglang"]["installed"] is True
    assert "unknown" in rows["sglang"]["compatibility_note"].lower()
    assert not any(name == "sglang" for name, _ in checks)


@pytest.fixture
def install_environment(monkeypatch, tmp_path):
    managed = tmp_path / "managed"
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    monkeypatch.setattr(routes, "managed_sglang_venv", lambda: managed)
    monkeypatch.setattr(routes, "_require_admin", lambda request: None)
    endpoint = next(route.endpoint for route in routes.setup_shell_routes().routes
                    if route.path == "/api/cookbook/packages/install")

    async def install(package, **kwargs):
        request = AsyncMock()
        request.json.return_value = {"pip": package, **kwargs}
        return await endpoint(request)

    return managed, install


@pytest.mark.asyncio
async def test_legacy_sglang_install_requires_its_environment(install_environment, monkeypatch):
    _, install = install_environment
    execute = AsyncMock(side_effect=AssertionError("Do not install SGLang into the app Python"))
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)
    with pytest.raises(HTTPException) as error:
        await install("sglang[all]")
    assert error.value.status_code == 400
    assert "scripts/setup_sglang_runtime.py" in error.value.detail
    execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("package", ["sglang", "sglang[all]", routes.SGLANG_REQUIREMENT])
@pytest.mark.parametrize("explicit", [False, True])
async def test_legacy_sglang_install_is_bounded_and_uses_selected_python(install_environment, monkeypatch, tmp_path, package, explicit):
    managed, install = install_environment
    _python(managed)
    selected = tmp_path / "selected" if explicit else managed
    python = _python(selected) if explicit else selected / "bin" / "python"
    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(b"installed", b"")))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)

    result = await install(package, **({"venv": str(selected)} if explicit else {}))

    assert result["ok"] is True
    assert all(call.args[0] == str(python) for call in execute.call_args_list)
    assert all(call.kwargs["env"]["VIRTUAL_ENV"] == str(selected) for call in execute.call_args_list)
    assert execute.call_args_list[0].args[1] == "-c"  # Python preflight precedes pip.
    assert execute.call_args_list[-1].args[1:] == (
        "-m", "pip", "install", "--pre", "--only-binary=sglang", routes.SGLANG_REQUIREMENT,
    )


@pytest.mark.asyncio
async def test_incompatible_selected_python_stops_before_pip(install_environment, monkeypatch):
    managed, install = install_environment
    _python(managed)
    process = SimpleNamespace(returncode=78, communicate=AsyncMock(return_value=(b"", b"Python 3.14 is unsupported; use Python 3.12")))
    execute = AsyncMock(return_value=process)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", execute)
    result = await install("sglang[all]")
    assert result["ok"] is False
    assert "Python 3.14" in result["error"]
    assert execute.await_count == 1
    assert execute.call_args.args[1] == "-c"


def test_environment_interpreter_symlink_keeps_venv_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    actual = tmp_path / "base-python"
    actual.touch()
    root = tmp_path / "venv"
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").symlink_to(actual)
    assert routes._local_venv_python(root) == root / "bin" / "python"
