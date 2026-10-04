"""Stale dependency install/repair actions cannot install unsupported runtimes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

import routes.cookbook_routes as cookbook
import routes.shell_routes as shell
from routes.cookbook_helpers import ServeRequest


@pytest.fixture
def install_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(cookbook, "require_admin", lambda request: None)
    monkeypatch.setattr(cookbook, "load_stored_hf_token", lambda **kwargs: "")
    monkeypatch.setattr(cookbook, "TMUX_LOG_DIR", tmp_path)
    monkeypatch.setattr(cookbook, "_binary_available", AsyncMock(return_value=False))
    monkeypatch.setattr(cookbook.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unsupported install must not launch"))
    monkeypatch.setattr(cookbook.asyncio, "create_subprocess_shell", lambda *a, **kw: pytest.fail("Unsupported install must not launch"))
    return next(route.endpoint for route in cookbook.setup_cookbook_routes().routes
                if route.path == "/api/model/serve" and "POST" in route.methods)


@pytest.mark.asyncio
@pytest.mark.parametrize("package", ["mlx-lm", "mlx>=0.32", "vllm", "sglang[all]", "mflux", "mlx_vlm"])
@pytest.mark.parametrize("local_windows,host,claimed_platform", [
    (True, None, "darwin"),  # An imported Mac profile cannot override the local OS.
    (True, None, "windows"),
    (False, "user@win-box", "win32"),
    (False, "user@win-box", "windows"),
])
async def test_windows_rejects_unsupported_dependency_before_install(
    install_endpoint, monkeypatch, package, local_windows, host, claimed_platform,
):
    monkeypatch.setattr(cookbook, "IS_WINDOWS", local_windows)
    with pytest.raises(HTTPException) as error:
        await install_endpoint(SimpleNamespace(), ServeRequest(
            repo_id="dependency-repair", cmd=f"python -m pip install '{package}'",
            remote_host=host, platform=claimed_platform,
        ))
    assert error.value.status_code == 400
    assert "native Windows" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("package,platform", [("vllm", "linux"), ("mlx-lm", "darwin")])
async def test_windows_client_can_install_on_supported_remote(install_endpoint, monkeypatch, package, platform):
    monkeypatch.setattr(cookbook, "IS_WINDOWS", True)
    result = await install_endpoint(SimpleNamespace(), ServeRequest(
        repo_id=package, cmd=f"python3 -m pip install {package}",
        remote_host="user@model-server", platform=platform,
    ))
    # Reaching the normal tmux preflight means platform validation accepted the target.
    assert "tmux" in result["error"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("package", [
    "mlx-lm", "mlx-vlm", "mflux", "sglang[all]", "vllm",
    "git+https://github.com/xocialize/boogu-image-mlx.git",
])
async def test_legacy_installer_rejects_unsupported_native_windows_packages(monkeypatch, package):
    monkeypatch.setattr(shell, "IS_WINDOWS", True)
    monkeypatch.setattr(shell, "_require_admin", lambda request: None)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", lambda *a, **kw: pytest.fail("Unsupported install must not launch"))
    endpoint = next(route.endpoint for route in shell.setup_shell_routes().routes
                    if route.path == "/api/cookbook/packages/install")
    request = AsyncMock()
    request.json.return_value = {"pip": package}
    with pytest.raises(HTTPException) as error:
        await endpoint(request)
    assert error.value.status_code == 400
    assert "native Windows" in error.value.detail
