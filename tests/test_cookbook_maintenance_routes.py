from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
import pytest

import routes.shell_routes as routes
from src import gpu_memory


def endpoint(path):
    return next(route.endpoint for route in routes.setup_shell_routes().routes if route.path == path)


def request(body=None, *, admin=True, cross_site=False):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_admin=lambda user: admin))),
        state=SimpleNamespace(current_user='test-user'),
        headers={'sec-fetch-site': 'cross-site' if cross_site else 'same-origin'},
        json=AsyncMock(return_value=body or {}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/api/cookbook/clear-vram', '/api/cookbook/environment-packages'])
@pytest.mark.parametrize('options', [{'admin': False}, {'cross_site': True}])
async def test_maintenance_rejects_unauthorized_requests_before_work(monkeypatch, path, options):
    monkeypatch.setattr(routes, 'inspect_environment', AsyncMock(side_effect=AssertionError('Must not probe')))
    monkeypatch.setattr(gpu_memory, 'clear_vram', Mock(side_effect=AssertionError('Must not release memory')))
    with pytest.raises(HTTPException) as error:
        await endpoint(path)(request(**options))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_clear_memory_route_stays_in_app_and_passes_explicit_unload_choice(monkeypatch):
    clear = Mock(return_value={'ok': True, 'released_mb': 12})
    monkeypatch.setattr(gpu_memory, 'clear_vram', clear)
    assert (await endpoint('/api/cookbook/clear-vram')(request({'unload_models': True})))['released_mb'] == 12
    clear.assert_called_once_with(unload_models=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'unload_models': 'false'}, {'host': 'remote'}, {'pid': 1234}])
async def test_clear_memory_cannot_target_an_arbitrary_process_or_remote_host(body):
    with pytest.raises(HTTPException) as error:
        await endpoint('/api/cookbook/clear-vram')(request(body))
    assert error.value.status_code == 400


@pytest.mark.asyncio
async def test_inventory_route_keeps_selected_environment_and_surfaces_failure(monkeypatch):
    probe = AsyncMock(return_value={'packages': []})
    monkeypatch.setattr(routes, 'inspect_environment', probe)
    api = endpoint('/api/cookbook/environment-packages')
    await api(request(), host='mac', ssh_port='2223', env='venv', env_path='/opt/env', platform='darwin', check_updates=True)
    probe.assert_awaited_once_with(host='mac', ssh_port='2223', env='venv', env_path='/opt/env', platform='darwin', check_updates=True)
    probe.side_effect = RuntimeError('Selected server unreachable')
    with pytest.raises(HTTPException) as error:
        await api(request())
    assert error.value.status_code == 502
    assert 'unreachable' in error.value.detail
