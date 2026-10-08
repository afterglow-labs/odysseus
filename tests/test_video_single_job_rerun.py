"""Editing a terminal job reruns a frozen copy of its saved settings and media."""

import json
from pathlib import Path
import shutil
import uuid

import pytest

from src import h3_video as h3
from tests.test_video_queue_bulk import create, request
from tests.test_video_submission import backend, image


def saved_uploads(manifest):
    result = {}
    for field, names in manifest['input_names'].items():
        paths = manifest[field] if isinstance(manifest[field], list) else [manifest[field]]
        result[field] = [(name, Path(path).read_bytes()) for name, path in zip(names, paths, strict=True)]
    return result


@pytest.mark.parametrize('family', ['h3', 'bfs'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'stopped'])
def test_single_terminal_edited_rerun_retains_all_inputs_and_recovers_frozen_request(backend, family, status):
    extra = [
        ('reference_videos', ('second.mp4', b'shared second reference', 'video/mp4')),
        ('reference_images', ('identity.png', image(), 'image/png')),
        ('reference_audio', ('voice.wav', b'saved reference soundtrack', 'audio/wav')),
    ] if family == 'h3' else None
    job = create(backend, family, extra=extra)
    manager = backend.managers[family]
    source = manager.directory(job['id'])
    original_state = h3._read(source / 'state.json')
    h3._write(source / 'state.json', {**original_state, 'status': status, 'finished_at': 123.0})
    (source / 'output.mp4').write_bytes(b'existing original result')
    before = {name: (source / name).read_bytes() for name in ('manifest.json', 'state.json', 'output.mp4')}
    original = json.loads(before['manifest.json'])
    media = saved_uploads(original)
    patch = {'steps': 9, 'seed': 987654, 'prompt': 'Change one detail and retain the full ending.'}
    key = str(uuid.uuid4())

    accepted = request(backend, 'rerun', [job], patch, family=family, key=key)
    assert accepted.status_code == 200, accepted.text
    receipt = accepted.json()
    assert receipt['succeeded'] == receipt['rerun'] == 1 and receipt['failed'] == 0
    assert len(receipt['jobs']) == 1 and receipt['jobs'][0]['source_id'] == job['id']
    copied = manager.directory(receipt['jobs'][0]['id'])
    assert copied != source
    cloned = h3._read(copied / 'manifest.json')
    assert h3._read(copied / 'state.json')['status'] == 'queued'
    assert cloned['config']['steps'] == patch['steps'] and cloned['config']['seed'] == patch['seed']
    assert patch['prompt'] in cloned['config']['prompt']
    if family == 'bfs':
        assert cloned['config']['prompt_notes'] == patch['prompt']
    for setting, value in original['config'].items():
        if setting not in {'prompt', 'prompt_notes', 'steps', 'seed'}:
            assert cloned['config'][setting] == value, setting
    submission = h3._read(copied / 'submission.json')
    assert submission['rerun_of'] == job['id'] and submission['submission_id'] == uuid.UUID(key).hex
    assert cloned['input_names'] == original['input_names']
    assert saved_uploads(cloned) == media
    assert saved_uploads(original) == media
    assert {name: (source / name).read_bytes() for name in before} == before
    assert len(backend.calls) == 2, 'Only the original job and one queued copy were launched'

    # An accepted response can be lost and retried after a server restart and
    # source deletion. Recovery must use the durable receipt, not the UI draft,
    # a new cache scan, or another copy of multi-GB saved inputs.
    frozen_manifest = (copied / 'manifest.json').read_bytes()
    shutil.rmtree(source)
    fresh = type(manager)(manager.root, project=manager.project, gallery_directory=manager.gallery_directory)
    fresh.inventory = lambda: pytest.fail('Accepted rerun recovery must not rescan model inventory')
    backend.managers[family] = fresh
    recovered = request(backend, 'rerun', [job], patch, family=family, key=key)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()['jobs'] == receipt['jobs']
    assert len(backend.calls) == 2
    assert (copied / 'manifest.json').read_bytes() == frozen_manifest
    assert saved_uploads(h3._read(copied / 'manifest.json')) == media

    conflict = request(backend, 'rerun', [job], {**patch, 'steps': 10}, family=family, key=key)
    assert conflict.status_code == 409 and 'different jobs or parameters' in conflict.text
    assert len(backend.calls) == 2
    assert (copied / 'manifest.json').read_bytes() == frozen_manifest
