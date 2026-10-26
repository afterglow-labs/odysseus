"""A large saved-job list must not repeatedly resolve network model paths."""

from collections import Counter
import hashlib
from pathlib import Path

import pytest

from src import h3_video as h3, video_queue_bulk as bulk
from src.h3_loras import frozen_components
from src.video_job_edit import _draft
from tests.test_video_queue_bulk import create
from tests.test_video_submission import backend


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_single_job_snapshot_ignores_large_unrelated_history(backend, monkeypatch, family):
    job = create(backend, family)
    manager = backend.managers[family]
    directory = manager.directory(job['id'])
    h3._write(directory / 'state.json', {**h3._read(directory / 'state.json'), 'status': 'completed'})
    original = (directory / 'manifest.json').read_bytes()
    for index in range(1600):
        history = manager.root / f'{index:032x}'
        history.mkdir()
        h3._write(history / 'state.json', {'id': history.name, 'status': 'completed', 'owner': 'corey'})
        # A damaged/unavailable unrelated manifest must not block this editor.
        (history / 'manifest.json').write_text('{broken')
    projected, light = [], bulk._light

    def track(path, *args, **kwargs):
        projected.append(path.name)
        return light(path, *args, **kwargs)

    monkeypatch.setattr(bulk, '_light', track)
    response = backend.client().get(f'/api/video/{family}/queue/jobs',
                                    params={'status': 'finished', 'job_id': job['id']})
    assert response.status_code == 200, response.text
    rows = response.json()['jobs']
    assert [row['id'] for row in rows] == projected == [job['id']]
    saved = h3._read(directory / 'manifest.json')['config']
    assert (rows[0]['config']['width'], rows[0]['config']['height']) == (saved['width'], saved['height'])
    assert (directory / 'manifest.json').read_bytes() == original


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_single_job_snapshot_keeps_ownership_and_status_filters(backend, family):
    job, other = create(backend, family), create(backend, family, owner='other')
    client = backend.client()
    url = f'/api/video/{family}/queue/jobs'
    response = client.get(url, params={'status': 'queued', 'job_id': job['id']})
    assert response.status_code == 200
    assert [row['id'] for row in response.json()['jobs']] == [job['id']]
    assert client.get(url, params={'status': 'finished', 'job_id': job['id']}).json()['jobs'] == []
    for identifier in (other['id'], 'f' * 32, '../state'):
        response = client.get(url, params={'status': 'all', 'job_id': identifier})
        assert response.status_code == 404, response.text
        assert 'config' not in response.json()


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_finished_snapshot_orders_mixed_legacy_and_current_jobs(backend, family):
    jobs = [create(backend, family) for _ in range(4)]
    manager = backend.managers[family]
    overrides = [
        {'created_at': 2},  # Legacy state has no queue_order at all.
        {'created_at': 1, 'queue_order': None},
        {'created_at': 9, 'queue_order': 0},  # Zero is a valid explicit order.
        {'created_at': 0, 'queue_order': 3_000_000_000},
    ]
    original_states = []
    for job, override in zip(jobs, overrides):
        path = manager.directory(job['id']) / 'state.json'
        state = h3._read(path)
        state.pop('queue_order', None)
        h3._write(path, {**state, **override, 'status': 'completed'})
        original_states.append(path.read_bytes())
    response = backend.client().get(f'/api/video/{family}/queue/jobs?status=finished')
    assert response.status_code == 200, response.text
    assert [row['id'] for row in response.json()['jobs']] == [jobs[index]['id'] for index in (2, 1, 0, 3)]
    assert [(manager.directory(job['id']) / 'state.json').read_bytes() for job in jobs] == original_states


@pytest.mark.parametrize('family', ['h3', 'bfs'])
def test_finished_snapshot_resolves_each_component_once_per_request(backend, monkeypatch, family):
    jobs = [create(backend, family) for _ in range(4)]
    manager = backend.managers[family]
    for job in jobs:
        path = manager.directory(job['id']) / 'state.json'
        h3._write(path, {**h3._read(path), 'status': 'completed'})
    inventory = backend.inventories[family]
    known_paths = {item['path'] for item in inventory['components']}
    unavailable = '/mnt/unavailable-nas/unrelated.safetensors'
    inventory['components'].insert(0, {'id': 'irrelevant', 'role': 'unrelated', 'name': 'unrelated.safetensors',
                                       'path': unavailable, 'variant': 'shared'})
    counts, original = Counter(), Path.resolve

    def resolve(path, *args, **kwargs):
        value = str(path)
        assert value != unavailable, 'An incompatible component must be filtered before any path I/O'
        if value in known_paths:
            counts[value] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    for attempt in (1, 2):
        response = backend.client().get(f'/api/video/{family}/queue/jobs?status=finished')
        assert response.status_code == 200, response.text
        assert [row['id'] for row in response.json()['jobs']] == [job['id'] for job in jobs]
        assert all(count == attempt for count in counts.values()), 'Resolve cost must scale with distinct paths, not job count'
        assert counts, 'The test must exercise real component projection'
        for job in response.json()['jobs']:
            returned = job['config'] if family == 'h3' else job['config']['components']
            expected = backend.configs[family] if family == 'h3' else backend.configs[family]['components']
            assert returned['model'] == expected['model']
        assert str(manager.project) not in response.text, 'Snapshot controls expose IDs, never server model paths'


def test_saved_lora_stack_shares_path_resolution_with_scalar_compatibility_fields(tmp_path, monkeypatch):
    paths = [tmp_path / 'adapter-one.safetensors', tmp_path / 'adapter-two.safetensors']
    for path in paths:
        path.touch()
    inventory = {'components': [{'id': f'layer-{index}', 'role': 'lora', 'path': str(path)}
                                for index, path in enumerate(paths)]}
    config = {'mode': 'ref2va', 'lora': str(paths[0]), 'lora_scale': .6,
              'loras': [{'path': str(paths[0]), 'strength': .6}, {'path': str(paths[1]), 'strength': 1.1}]}
    original, counts = Path.resolve, Counter()

    def resolve(path, *args, **kwargs):
        counts[str(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    cache = {}
    for _ in range(200):
        draft = _draft(config, inventory, 'h3', resolved_paths=cache)
        assert draft['lora'] == 'layer-0'
        assert draft['loras'] == [{'id': 'layer-0', 'strength': .6}, {'id': 'layer-1', 'strength': 1.1}]
        frozen = frozen_components(config, inventory, resolved_paths=cache)
        assert [item['id'] for item in frozen] == ['layer-0', 'layer-1']
        assert all('path' not in item for item in frozen)
    assert counts == {str(path): 1 for path in paths}


def test_new_snapshot_reconsiders_relinked_component_instead_of_using_global_cache(tmp_path):
    original, replacement, alias = (tmp_path / name for name in ('original.safetensors', 'new.safetensors', 'alias.safetensors'))
    original.touch()
    replacement.touch()
    alias.symlink_to(original)
    inventory = {'components': [{'id': 'available-model', 'role': 'model', 'path': str(alias)}]}
    config = {'mode': 'ref2va', 'model': str(original)}
    assert _draft(config, inventory, 'h3')['model'] == 'available-model'
    alias.unlink()
    alias.symlink_to(replacement)
    assert _draft(config, inventory, 'h3')['model'] == hashlib.sha256(str(original.resolve()).encode()).hexdigest()[:32]
