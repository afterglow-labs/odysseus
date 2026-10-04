"""Local dependency runners prepare fixed Real-ESRGAN wheels on every OS."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

import routes.cookbook_routes as cookbook_routes
import routes.shell_routes as shell_routes
from routes.cookbook_helpers import ServeRequest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_frozen_app_refuses_local_pip_instead_of_using_path_python(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "load_stored_hf_token", lambda **kwargs: "")
    monkeypatch.setattr(cookbook_routes.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("Must reject before launching an install"))
    endpoint = next(route.endpoint for route in cookbook_routes.setup_cookbook_routes().routes
                    if route.path == "/api/model/serve" and "POST" in route.methods)
    with pytest.raises(HTTPException) as error:
        await endpoint(SimpleNamespace(), ServeRequest(
            repo_id="playwright", cmd="python -m pip install playwright", platform="windows",
        ))
    assert error.value.status_code == 400
    assert "desktop launcher" in error.value.detail


@pytest.mark.asyncio
async def test_frozen_legacy_installer_does_not_relaunch_the_app(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(shell_routes, "_require_admin", lambda request: None)
    monkeypatch.setattr(shell_routes.asyncio, "create_subprocess_exec", lambda *args, **kwargs: pytest.fail("Must reject before launching Python"))
    endpoint = next(route.endpoint for route in shell_routes.setup_shell_routes().routes
                    if route.path == "/api/cookbook/packages/install")
    request = AsyncMock()
    request.json.return_value = {"pip": "playwright"}
    with pytest.raises(HTTPException) as error:
        await endpoint(request)
    assert error.value.status_code == 400
    assert "desktop launcher" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("package", ["realesrgan", "basicsr", "gfpgan", "facexlib", "playwright"])
@pytest.mark.parametrize(
    "platform,local_windows,remote",
    [
        ("darwin", False, None),
        ("linux", False, None),
        ("windows", True, None),
        ("linux", False, "tester@gpu-box"),
        ("windows", False, "tester@gpu-box"),
    ],
)
async def test_realesrgan_wheel_preparation_only_for_local_app_installs(
    monkeypatch, tmp_path, package, platform, local_windows, remote,
):
    process = AsyncMock()
    process.returncode = 1
    process.stderr.read.return_value = b"launch intentionally stopped"

    def stop_detached_launch(*args, **kwargs):
        raise RuntimeError("launch intentionally stopped")

    monkeypatch.setattr(cookbook_routes, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook_routes, "IS_WINDOWS", local_windows)
    monkeypatch.setattr(cookbook_routes, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook_routes, "load_stored_hf_token", lambda **kwargs: "")
    monkeypatch.setattr(cookbook_routes, "_binary_available", AsyncMock(return_value=True))
    monkeypatch.setattr(cookbook_routes, "find_bash", lambda: "bash")
    monkeypatch.setattr(cookbook_routes.subprocess, "Popen", stop_detached_launch)
    monkeypatch.setattr(
        cookbook_routes.asyncio, "create_subprocess_shell", AsyncMock(return_value=process),
    )
    endpoint = next(
        route.endpoint for route in cookbook_routes.setup_cookbook_routes().routes
        if route.path == "/api/model/serve" and "POST" in route.methods
    )
    response = await endpoint(
        Request({"type": "http", "method": "POST", "path": "/api/model/serve", "headers": []}),
        ServeRequest(
            repo_id=package,
            cmd=f"python3 -m pip install --user --break-system-packages {package}",
            platform=platform,
            remote_host=remote,
        ),
    )

    assert response["error"] == "launch intentionally stopped"
    runner = next(tmp_path.glob("serve-*_run.*")).read_text(encoding="utf-8")
    needs_compatibility = package != "playwright" and not remote
    assert ("build_realesrgan_wheels.py" in runner) is bool(needs_compatibility)
    if needs_compatibility:
        assert runner.index("-m pip --version") < runner.index("build_realesrgan_wheels.py")
        assert "--user" not in runner  # The app venv must remain the install target.
        assert "--break-system-packages" not in runner


async def _legacy_install(monkeypatch, package, statuses):
    calls = []
    remaining = iter(statuses)

    async def execute(*args, **kwargs):
        calls.append(args)
        code = next(remaining)
        return SimpleNamespace(
            returncode=code,
            communicate=AsyncMock(return_value=(b"installed", b"stage failed" if code else b"")),
        )

    monkeypatch.setattr(shell_routes, "_require_admin", lambda request: None)
    monkeypatch.setattr(shell_routes.asyncio, "create_subprocess_exec", execute)
    endpoint = next(
        route.endpoint for route in shell_routes.setup_shell_routes().routes
        if route.path == "/api/cookbook/packages/install" and "POST" in route.methods
    )
    request = AsyncMock()
    request.json.return_value = {"pip": package}
    response = await endpoint(request)
    assert all(command[0] == sys.executable for command in calls)
    return response, [command[1:] for command in calls]


@pytest.mark.asyncio
@pytest.mark.parametrize("package", ["realesrgan", "basicsr", "gfpgan", "facexlib", "rembg[cpu]"])
async def test_legacy_installer_bootstraps_then_prepares_compatibility(monkeypatch, package):
    needs_compatibility = package != "rembg[cpu]"
    response, calls = await _legacy_install(
        monkeypatch, package, [1, 0, 0] + ([0] if needs_compatibility else []) + [0],
    )
    assert response == {"ok": True, "output": "installed"}
    assert calls[:3] == [("-m", "pip", "--version"), ("-m", "ensurepip", "--upgrade"), ("-m", "pip", "--version")]
    if needs_compatibility:
        assert calls[3][0].endswith("build_realesrgan_wheels.py")
        assert calls[3][1:] == ("--install",)
    assert calls[-1] == ("-m", "pip", "install", package)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "statuses,expected_stage",
    [([1, 31], "Pip bootstrap failed"), ([1, 0, 23], "Pip is still unavailable"), ([0, 19], "compatibility preparation failed")],
)
async def test_legacy_installer_preparation_failure_stops_pip_install(monkeypatch, statuses, expected_stage):
    response, calls = await _legacy_install(monkeypatch, "realesrgan", statuses)
    assert response["ok"] is False
    assert expected_stage in response["error"]
    assert "stage failed" in response["error"]
    assert ("-m", "pip", "install", "realesrgan") not in calls


@pytest.mark.asyncio
async def test_legacy_installer_preserves_install_failure_response(monkeypatch):
    response, calls = await _legacy_install(monkeypatch, "playwright", [0, 41])
    assert response == {"ok": False, "error": "stage failed"}
    assert calls == [("-m", "pip", "--version"), ("-m", "pip", "install", "playwright")]
