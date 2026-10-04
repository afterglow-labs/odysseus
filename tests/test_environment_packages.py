"""Environment maintenance inspects the same Python selected for installs."""

import asyncio
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src import environment_packages as packages


def test_local_inventory_uses_app_interpreter_and_ignores_imported_remote_environment():
    argv = packages.probe_command(env_path='/old/mac/venv', platform='darwin')
    assert argv[:2] == [sys.executable, '-c']
    assert '/old/mac/venv' not in argv


def test_inventory_returns_installed_versions_without_importing_packages_or_networking(tmp_path):
    info = tmp_path / 'broken_demo-1.2.dist-info'
    info.mkdir()
    (info / 'METADATA').write_text('Name: broken-demo\nVersion: 1.2\n', encoding='utf-8')
    script = "import importlib.metadata as m; original = m.distributions; m.distributions = lambda: original(path=[" + repr(str(tmp_path)) + "]);\n"
    result = subprocess.run([sys.executable, '-c', script + packages.ENVIRONMENT_PROBE],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['executable'] == sys.executable
    assert data['packages'] == [{'name': 'broken-demo', 'version': '1.2'}]
    assert data['updates_checked'] is False


def test_remote_posix_inventory_quotes_venv_path_and_uses_selected_port():
    argv = packages.probe_command(host='user@gpu', ssh_port='2222', env='venv', env_path='~/model env', platform='linux')
    assert argv[-2] == 'user@gpu'
    assert argv[-4:-2] == ['-p', '2222']
    assert '"$HOME"/\'model env/bin/python\'' in argv[-1]
    assert '-c ' in argv[-1]


def test_remote_windows_inventory_uses_powershell_and_the_selected_python():
    import base64
    argv = packages.probe_command(host='user@win', env='venv', env_path=r'C:\Model env', platform='windows')
    assert argv[-1].startswith('powershell -NoProfile -NonInteractive -EncodedCommand ')
    script = base64.b64decode(argv[-1].split()[-1]).decode('utf-16-le')
    assert "& 'C:\\Model env\\Scripts\\python.exe'" in script
    assert '-c ' in script


@pytest.mark.parametrize('kwargs', [{'host': '-oProxyCommand=bad'}, {'host': 'x', 'ssh_port': 'bad'}, {'host': 'x', 'env_path': 'ok\nmalicious'}])
def test_inventory_rejects_invalid_targets(kwargs):
    with pytest.raises(ValueError):
        packages.probe_command(**kwargs)


@pytest.mark.asyncio
async def test_failed_inventory_is_visible_instead_of_an_empty_success(monkeypatch):
    process = SimpleNamespace(returncode=1, communicate=AsyncMock(return_value=(b'', b'selected Python not found')))
    monkeypatch.setattr(packages.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    with pytest.raises(RuntimeError, match='selected Python not found'):
        await packages.inspect_environment()


@pytest.mark.asyncio
async def test_timeout_stops_the_probe(monkeypatch):
    process = SimpleNamespace(returncode=None, communicate=AsyncMock(), kill=lambda: None, wait=AsyncMock())
    killed = []
    process.kill = lambda: killed.append(True)
    monkeypatch.setattr(packages.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process))
    async def timeout(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError
    monkeypatch.setattr(packages.asyncio, 'wait_for', timeout)
    with pytest.raises(RuntimeError, match='timed out'):
        await packages.inspect_environment()
    assert killed == [True]


@pytest.mark.parametrize('stderr,returncode,checked', [('', 0, True), ('index unreachable', 1, False), ('WARNING: Could not fetch URL: SSLerror', 0, False)])
def test_update_check_uses_selected_python_and_does_not_claim_success_after_index_failure(monkeypatch, capsys, stderr, returncode, checked):
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'distributions', lambda: [SimpleNamespace(metadata={'Name': 'Foo_Bar'}, version='1.0')])
    invoked = []
    def pip_check(argv, **kwargs):
        invoked.append(argv)
        return SimpleNamespace(returncode=returncode, stdout='[{"name":"foo-bar","latest_version":"2.0"}]', stderr=stderr)
    monkeypatch.setattr(subprocess, 'run', pip_check)
    monkeypatch.setattr(sys, 'argv', ['probe', '--check-updates'])
    exec(packages.ENVIRONMENT_PROBE, {})
    result = json.loads(capsys.readouterr().out)
    assert invoked[0][:5] == [sys.executable, '-m', 'pip', 'list', '--outdated']
    assert result['updates_checked'] is checked
    if checked:
        assert result['packages'][0]['latest_version'] == '2.0'
        assert result['packages'][0]['update_available']
    else:
        assert result['check_error']
        assert 'update_available' not in result['packages'][0]
