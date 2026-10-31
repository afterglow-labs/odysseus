"""A slow/offline model library must not stall local workflow controls."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
import time
import uuid

import pytest

from src import h3_video as h3
from src import video_component_inventory as inventory_cache
from tests.test_h3_video import weights
from tests.test_video_submission import backend, send


def drain(index):
    with index.condition:
        assert index.condition.wait_for(lambda: not any(e.scanning for e in index.roots.values()), timeout=3)


def component(path, **changes):
    return {"id": str(path), "name": path.name, "path": str(path), "role": "model", **changes}


def test_slow_remote_scan_does_not_block_local_models_or_new_loras(tmp_path):
    local, remote = tmp_path / 'linux', tmp_path / 'nas'
    local.mkdir()
    remote.mkdir()
    weights(local / 'minimax_h3_ref2va.safetensors')
    release = threading.Event()
    scanning = threading.Event()
    calls = []

    def scanner(roots):
        calls.append(roots[0])
        if roots[0] == remote:
            scanning.set()
            assert release.wait(5)
        return h3.discover_components(roots)

    index = inventory_cache.ComponentIndex('h3', scanner, directory=tmp_path / 'cache')
    try:
        rows, status = index.get([local, remote], wait=.1)
        assert [row['name'] for row in rows] == ['minimax_h3_ref2va.safetensors']
        assert scanning.wait(1) and status['refreshing']
        assert status['pending_roots'] == [str(remote)]
        started = time.monotonic()
        for _ in range(20):
            assert index.get([local, remote])[0] == rows
        assert time.monotonic() - started < .5
        assert calls.count(remote) == 1

        weights(local / 'minimax_h3_new_lora.safetensors', lora=True)
        rows, status = index.get([local, remote], refresh=True, wait=.1)
        assert {row['role'] for row in rows} == {'model', 'lora'}
        assert status['refreshing'] and calls.count(remote) == 1
    finally:
        release.set()
        drain(index)


def test_restart_and_concurrent_refresh_keep_snapshot_while_offline(tmp_path):
    root = tmp_path / 'models'
    root.mkdir()
    row = component(root / 'minimax_h3.safetensors')
    cache = tmp_path / 'cache'
    first = inventory_cache.ComponentIndex('project:h3', lambda roots: [row], directory=cache)
    assert first.get([root], refresh=True)[0] == [row]
    drain(first)
    release = threading.Event()
    calls = []

    def scanner(roots):
        calls.append(roots)
        assert release.wait(5)
        raise OSError('Network unavailable')

    restarted = inventory_cache.ComponentIndex('project:h3', scanner, directory=cache)
    try:
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: restarted.get([root], refresh=True, wait=.02), range(16)))
        assert time.monotonic() - started < .8
        assert all(rows == [row] for rows, _ in results)
        assert len(calls) == 1
        # Returned structures cannot mutate another request or the saved index.
        results[0][0][0]['name'] = 'changed'
        assert restarted.get([root])[0] == [row]
    finally:
        release.set()
        drain(restarted)
    rows, status = restarted.get([root])
    assert rows == [row] and status['unavailable_roots'] == [str(root)]
    assert not status['refreshing']


def test_removed_roots_and_deleted_local_files_do_not_remain_selectable(tmp_path):
    local, remote = tmp_path / 'linux', tmp_path / 'nas'
    for root in (local, remote):
        root.mkdir()
    a = weights(local / 'minimax_h3_ref2va.safetensors')
    weights(remote / 'minimax_h3_fl2va.safetensors')
    index = inventory_cache.ComponentIndex('h3', h3.discover_components, directory=tmp_path / 'cache')
    assert len(index.get([local, remote], refresh=True)[0]) == 2
    assert [row['name'] for row in index.get([local])[0]] == [a.name]
    a.unlink()
    assert index.get([local], refresh=True)[0] == []
    drain(index)


def test_aliases_and_blob_copies_are_deduplicated_without_path_io():
    original = component(Path('/old/minimax_h3.safetensors'), id='old')
    replacement = component(Path('/linux/minimax_h3.safetensors'), id='new', aliases=['old'])
    blob = component(Path('/hub/model.safetensors'), id='blob', _blob_identity='hash/model.safetensors')
    duplicate = {**blob, 'id': 'copy', 'path': '/other-hub/model.safetensors'}
    rows = inventory_cache.merge_components([original, replacement, blob, duplicate, replacement])
    assert [row['id'] for row in rows] == ['new', 'blob']
    assert not any('_blob_identity' in row for row in rows)


def test_root_enumeration_does_not_touch_network_paths(tmp_path, monkeypatch):
    state = tmp_path / 'cookbook.json'
    state.write_text('{"env":{"servers":[{"host":"","modelDirs":["/mnt/e/models"]}]}}')
    def forbidden(*args, **kwargs):
        pytest.fail('Root selection must not resolve/stat mounted libraries')
    monkeypatch.setattr(Path, 'resolve', forbidden)
    monkeypatch.setattr(Path, 'is_dir', forbidden)
    roots = h3.cache_roots(tmp_path, state)
    assert Path('/mnt/e/models') in roots


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_manager_uses_saved_inventory_and_refreshes_added_files(tmp_path, monkeypatch, family):
    from src import bfs_video as bfs
    module = h3 if family == 'h3' else bfs
    manager_type = h3.H3JobManager if family == 'h3' else bfs.BFSJobManager
    local, remote = tmp_path / 'models', tmp_path / 'nas'
    local.mkdir()
    remote.mkdir()
    weights(local / 'minimax_h3_ref2va.safetensors')
    scanner = module.discover_components
    release = threading.Event()

    def slow(roots):
        if roots == [remote]:
            assert release.wait(8)
        return scanner(roots)

    monkeypatch.setattr(inventory_cache, 'VIDEO_COMPONENT_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(module, 'discover_components', slow)
    monkeypatch.setattr(module, 'cache_roots', lambda project: [local, remote])
    monkeypatch.setattr(module, 'gpu_inventory', lambda: [])
    monkeypatch.setattr(module, 'runtime_error', lambda *args: '')
    if family == 'bfs':
        monkeypatch.setattr(module, 'helper_error', lambda *args: '')
    manager = manager_type(tmp_path / 'jobs', project=tmp_path, preset_file=tmp_path / 'no-preset.json')
    index = inventory_cache.component_index(tmp_path, family, slow)
    try:
        current = manager.inventory()
        assert len(current['components']) == 1 and current['component_scan']['refreshing']
        started = time.monotonic()
        for _ in range(10):
            assert manager.inventory()['components'] == current['components']
        assert time.monotonic() - started < .5
        weights(local / 'minimax_h3_video_vae.safetensors')
        assert len(manager.refresh_inventory()['components']) == 2
        assert len(manager.submission_inventory()['components']) == 2
    finally:
        release.set()
        drain(index)


def test_cold_empty_index_is_bounded_and_reports_pending(tmp_path):
    root = tmp_path / 'models'
    root.mkdir()
    release = threading.Event()
    def slow(roots):
        assert release.wait(3)
        return []
    index = inventory_cache.ComponentIndex('empty', slow, directory=tmp_path / 'cache')
    try:
        started = time.monotonic()
        rows, status = index.get([root], wait=.05)
        assert time.monotonic() - started < .5
        assert not rows and status['pending_roots'] == [str(root)]
    finally:
        release.set()
        drain(index)


def test_cold_inventory_does_not_return_only_a_fast_runtime_helper(tmp_path):
    primary, helper, remote = [tmp_path / name for name in ('models', 'runtime', 'nas')]
    for root in (primary, helper, remote):
        root.mkdir()
    helper_done = threading.Event()
    release = threading.Event()
    def scanner(roots):
        root = roots[0]
        if root == primary:
            assert helper_done.wait(1)
            time.sleep(.02)
        elif root == helper:
            helper_done.set()
        else:
            assert release.wait(3)
        return [component(root / 'minimax_h3.safetensors')]
    index = inventory_cache.ComponentIndex('h3', scanner, directory=tmp_path / 'cache')
    try:
        rows, _ = index.get([primary, helper, remote], wait=.1)
        assert {row['path'] for row in rows} == {str(root / 'minimax_h3.safetensors') for root in (primary, helper)}
    finally:
        release.set()
        drain(index)


def test_profile_update_wins_over_inflight_old_scan_and_survives_restart(tmp_path):
    root = tmp_path / 'models'
    root.mkdir()
    original = component(root / 'minimax_h3_lora.safetensors', role='lora', display_name='Original')
    cached_old_scan = threading.Event()
    release = threading.Event()
    calls = []
    def scanner(roots):
        calls.append(True)
        if len(calls) > 1:
            cached_old_scan.set()
            assert release.wait(3)
        return [dict(original)]
    index = inventory_cache.ComponentIndex('h3', scanner, directory=tmp_path / 'cache')
    assert index.get([root], refresh=True)[0] == [original]
    drain(index)
    try:
        index.get([root], refresh=True, wait=.02)
        assert cached_old_scan.wait(1)
        updated = {**original, 'display_name': 'Updated'}
        index.invalidate(original['path'], updated)
        assert index.get([root], wait=0)[0] == [updated]
    finally:
        release.set()
        drain(index)
    restored = inventory_cache.ComponentIndex('h3', scanner, directory=tmp_path / 'cache')
    assert restored._load(str(root)) == [updated]


@pytest.mark.parametrize('stack', [False, True])
def test_migrated_preset_does_not_resolve_old_network_lora(tmp_path, monkeypatch, stack):
    from tests.test_h3_video import inventory as inventory_fixture
    current = inventory_fixture.__wrapped__(tmp_path)
    old_path = '/mnt/e/unavailable/minimax_h3_lora.safetensors'
    adapter = next(row for row in current['components'] if row['role'] == 'lora')
    adapter['aliases'] = [hashlib.sha256(old_path.encode()).hexdigest()[:32]]
    values = {role: next(row['path'] for row in current['components'] if row['role'] == role)
              for role in ('encoder', 'video_vae', 'audio_vae')}
    values['model'] = next(row['path'] for row in current['components'] if row['role'] == 'model' and row['variant'] == 'fl2va')
    values.update({'mode': 't2va', **({'loras': [{'id': old_path, 'strength': 1}]} if stack else {'lora': old_path})})
    path = tmp_path / 'preset.json'
    path.write_text(json.dumps({'format_version': 1, 'id': 'test', 'name': 'Preset', 'config': values}))
    resolve, stat = Path.resolve, Path.stat
    def guarded_resolve(path, *args, **kwargs):
        assert not str(path).startswith('/mnt/e/'), 'Migrated presets must not access the old NAS'
        return resolve(path, *args, **kwargs)
    def guarded_stat(path, *args, **kwargs):
        assert not str(path).startswith('/mnt/e/'), 'LoRA validation must use installed metadata'
        return stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', guarded_resolve)
    monkeypatch.setattr(Path, 'stat', guarded_stat)
    preset, error = h3.installed_h3_preset(path, current['components'], current['gpus'], {**h3.DEFAULTS, 'gpu': 'GPU-blackwell'})
    assert not error and preset is not None
    assert (preset['config']['loras'][0]['id'] if stack else preset['config']['lora']) == adapter['id']


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_edit_and_rerun_routes_work_during_a_blocked_network_scan(backend, tmp_path, monkeypatch, family):
    from src import bfs_video as bfs
    module = h3 if family == 'h3' else bfs
    manager = backend.managers[family]
    job = send(backend, family).json()
    # Use the real manager/inventory, keeping only process launch and GPU
    # discovery stubbed. These are the same endpoints used by the iOS client.
    monkeypatch.delattr(manager, 'inventory')
    monkeypatch.setattr(module, 'gpu_inventory', lambda: backend.inventories[family]['gpus'])
    monkeypatch.setattr(module, 'runtime_error', lambda *args: '')
    if family == 'bfs':
        monkeypatch.setattr(module, 'helper_error', lambda *args: '')
    local, remote = tmp_path / 'models', tmp_path / 'nas'
    remote.mkdir()
    scanner = module.discover_components
    release = threading.Event()
    def slow(roots):
        if roots == [remote]:
            assert release.wait(10)
        return scanner(roots)
    monkeypatch.setattr(module, 'discover_components', slow)
    monkeypatch.setattr(module, 'cache_roots', lambda project: [local, remote])
    monkeypatch.setattr(inventory_cache, 'VIDEO_COMPONENT_CACHE_DIR', str(tmp_path / 'cache'))
    index = inventory_cache.component_index(tmp_path, family, slow)
    client = backend.client()
    prefix = '/api/video/' + family
    try:
        assert client.get(prefix + '/inventory').status_code == 200
        started = time.monotonic()
        edit = client.get(prefix + '/jobs/' + job['id'] + '/edit')
        assert edit.status_code == 200, edit.text
        chosen = [{'id': job['id'], 'revision': 0}]
        changed = client.post(prefix + '/queue/edit', json={'jobs': chosen, 'patch': {'steps': 9}})
        assert changed.status_code == 200, changed.text
        chosen[0]['revision'] = 1
        rerun = client.post(prefix + '/queue/rerun', json={'jobs': chosen, 'patch': {'steps': 10}, 'request_id': str(uuid.uuid4())})
        assert rerun.status_code == 200, rerun.text
        assert rerun.json()['rerun'] == 1
        assert time.monotonic() - started < 2
        assert not release.is_set(), 'Requests must finish while the NAS scan is still blocked'
    finally:
        release.set()
        drain(index)
