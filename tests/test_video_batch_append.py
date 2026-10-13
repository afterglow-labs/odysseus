"""A new draft can append another batch while the previous batch renders."""
import copy
import uuid

import pytest

from src import h3_video as h3
from tests.test_video_submission import backend, send


@pytest.mark.parametrize('family,recipe', [('h3', None), ('h3', 'seed_hunter'), ('bfs', None)])
def test_append_distinct_batch_while_first_runs_preserves_existing_jobs(backend, family, recipe):
    first_config = copy.deepcopy(backend.configs[family])
    first_config.update(prompt='First batch: keep the blue coat.', steps=6, seed=123)
    if recipe:
        first_config.update(recipe=recipe, seed_hunter={'stage': 'preview'}, frames=73,
                            width=480, height=864, sampler='er_sde', scheduler='beta')
    backend.configs[family] = first_config
    first_batch = [send(backend, family, key=uuid.uuid4().hex, name=f'clip-{index}.mp4') for index in range(3)]
    assert [reply.status_code for reply in first_batch] == [201] * 3, [reply.text for reply in first_batch]
    manager = backend.managers[family]
    first_jobs = [reply.json() for reply in first_batch]
    active_directory = manager.directory(first_jobs[0]['id'])
    state = h3._read(active_directory / 'state.json')
    h3._write(active_directory / 'state.json', {**state, 'status': 'running', 'phase': 'sampling'})
    original_manifests = {job['id']: (manager.directory(job['id']) / 'manifest.json').read_bytes()
                          for job in first_jobs}
    original_states = {job['id']: (manager.directory(job['id']) / 'state.json').read_bytes()
                       for job in first_jobs}
    # Reusing the same input filenames is intentional; each new submission ID
    # represents a fresh job with the second draft's independent settings.
    second_config = {**copy.deepcopy(first_config), 'prompt': 'Second batch: use a purple jacket.', 'steps': 9, 'seed': 456}
    backend.configs[family] = second_config
    second_batch = [send(backend, family, key=uuid.uuid4().hex, name=f'clip-{index}.mp4') for index in range(2)]
    assert [reply.status_code for reply in second_batch] == [201] * 2, [reply.text for reply in second_batch]
    second_jobs = [reply.json() for reply in second_batch]
    assert [job['queue_position'] for job in second_jobs] == [3, 4]
    assert all(job['status'] == 'queued' for job in second_jobs)
    assert len({job['id'] for job in first_jobs + second_jobs}) == 5
    orders = [h3._read(manager.directory(job['id']) / 'state.json')['queue_order']
              for job in first_jobs + second_jobs]
    assert orders == sorted(orders) and len(set(orders)) == 5
    for job in first_jobs:
        directory = manager.directory(job['id'])
        assert (directory / 'manifest.json').read_bytes() == original_manifests[job['id']]
        assert (directory / 'state.json').read_bytes() == original_states[job['id']]
    for job in second_jobs:
        config = h3._read(manager.directory(job['id']) / 'manifest.json')['config']
        assert second_config['prompt'] in config['prompt']
        assert config['steps'] == 9 and config['seed'] == 456
        # The append path must not silently change device mapping, adapters,
        # dimensions, duration, sampler, or recipe when only these three draft
        # settings were edited. Compare the whole normalized worker config.
        previous = h3._read(manager.directory(first_jobs[0]['id']) / 'manifest.json')['config']
        changed = {'prompt', 'steps', 'seed', *({'prompt_notes'} if family == 'bfs' else set())}
        assert {key: value for key, value in config.items() if key not in changed} == {
            key: value for key, value in previous.items() if key not in changed}
    assert manager.view(first_jobs[0]['id'], 'corey')['status'] == 'running'
    assert len(backend.calls) == 5
