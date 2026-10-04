"""System installs must stay on the selected OS, never implicitly enter WSL."""

from unittest.mock import AsyncMock

from fastapi import HTTPException
import pytest

import routes.shell_routes as routes


@pytest.fixture
def install_endpoint(monkeypatch):
    monkeypatch.setattr(routes, "_require_admin", lambda request: None)
    return next(route.endpoint for route in routes.setup_shell_routes().routes
                if route.path == "/api/cookbook/install-system-deps")


@pytest.mark.asyncio
@pytest.mark.parametrize("packages", [["tmux"], ["cmake", "git"]])
@pytest.mark.parametrize("claimed_platform", [None, "linux", "darwin", "windows"])
async def test_local_windows_rejects_posix_installer_before_launch(
    install_endpoint, monkeypatch, packages, claimed_platform,
):
    monkeypatch.setattr(routes, "IS_WINDOWS", True)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec",
                        lambda *args, **kwargs: pytest.fail("Must not enter WSL or run an installer"))
    request = AsyncMock()
    request.json.return_value = {"packages": packages, "platform": claimed_platform}
    with pytest.raises(HTTPException) as error:
        await install_endpoint(request)
    assert error.value.status_code == 400
    assert "Windows" in error.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["win32", "windows", "win"])
async def test_remote_windows_rejects_posix_installer(install_endpoint, monkeypatch, platform):
    monkeypatch.setattr(routes, "IS_WINDOWS", False)
    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec",
                        lambda *args, **kwargs: pytest.fail("Must not send a POSIX installer to Windows"))
    request = AsyncMock()
    request.json.return_value = {"packages": ["tmux"], "remote_host": "user@windows-box", "platform": platform}
    with pytest.raises(HTTPException) as error:
        await install_endpoint(request)
    assert error.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["linux", "darwin", None])
async def test_windows_client_can_install_on_selected_posix_remote(install_endpoint, monkeypatch, platform):
    monkeypatch.setattr(routes, "IS_WINDOWS", True)
    launched = []

    async def launch(*args, **kwargs):
        launched.append(args)
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"installed tmux on selected target", b"")
        return process

    monkeypatch.setattr(routes.asyncio, "create_subprocess_exec", launch)
    request = AsyncMock()
    request.json.return_value = {"packages": ["tmux"], "remote_host": "user@server", "ssh_port": "2222", "platform": platform}
    result = await install_endpoint(request)
    assert result["ok"] is True
    assert launched[0][0] == "ssh"
    assert launched[0][-2] == "user@server"
    assert launched[0][-4:-2] == ("-p", "2222")
    assert "install" in launched[0][-1]
