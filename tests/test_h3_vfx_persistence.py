"""VFX source guides keep their identity through edits, reruns, and transfers."""
import json
from pathlib import Path

import pytest

from src import h3_video as h3, video_workflow as workflow
from src.video_job_edit import _inputs
from tests.test_h3_video import weights
from tests.test_video_submission import backend, send, image
from tests.test_video_job_edit import patch
from tests.test_video_queue_bulk import request
from tests.test_video_workflow import document, archive, transfer


@pytest.fixture
def vfx(backend):
    manager = backend.managers['h3']
    folder = manager.project / 'models'
    for name in ('minimax_h3_vfx_edit_v1.0_r128.safetensors', 'minimax_h3_vfx_edit_v1.0_r128_ffp.safetensors'):
        weights(folder / name, True)
    inventory = backend.inventories['h3']
    inventory['components'] = h3.discover_components([folder])
    selected = next(c for c in inventory['components'] if c['name'] == 'minimax_h3_vfx_edit_v1.0_r128.safetensors')
    raw = {**backend.configs['h3'], 'loras': [{'id': selected['id'], 'strength': 1}]}
    return backend, manager, raw


def legacy(vfx):
    backend, manager, raw = vfx
    response = send(backend, 'h3', name='original-guide.mp4')
    assert response.status_code == 201, response.text
    job = response.json()
    path = manager.directory(job['id']) / 'manifest.json'
    saved = h3._read(path)
    saved['config'] = h3.validate_config(raw, backend.inventories['h3']['components'], backend.inventories['h3']['gpus'], {'source_video': [None]})
    saved.pop('source_video', None)
    saved.get('input_names', {}).pop('source_video', None)
    h3._write(path, saved)
    return job, path


def modern(vfx):
    backend, manager, raw = vfx
    response = backend.client().post('/api/video/h3/jobs', data={'config': json.dumps(raw)}, files=[
        ('source_video', ('guide.mp4', b'guide', 'video/mp4')),
        ('reference_videos', ('motion.mp4', b'motion', 'video/mp4')),
        ('reference_images', ('jacket.png', image(), 'image/png')),
        ('reference_audio', ('style.wav', b'audio', 'audio/wav')),
    ])
    assert response.status_code == 201, response.text
    job = response.json()
    return job, manager.directory(job['id']) / 'manifest.json'


def test_read_legacy_source_promotes_names_without_writing_and_edit_persists_single_path(vfx):
    backend, manager, _ = vfx
    job, path = legacy(vfx)
    before = path.read_bytes()
    response = backend.client().get(f'/api/video/h3/jobs/{job["id"]}/edit')
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['inputs']['source_video'][0]['name'] == 'original-guide.mp4'
    assert not draft['inputs'].get('reference_videos')
    assert path.read_bytes() == before
    response = patch(backend, job, config={**draft['config'], 'steps': 8}, retain={'source_video': [0]})
    assert response.status_code == 200, response.text
    saved = h3._read(path)
    assert isinstance(saved['source_video'], str) and saved['reference_videos'] == []
    assert saved['input_names']['source_video'] == ['original-guide.mp4']
    assert saved['source_name'] == 'original-guide.mp4'


def test_new_source_and_reference_channels_survive_edit_and_rerun(vfx):
    backend, manager, raw = vfx
    job, path = modern(vfx)
    before = h3._read(path)
    response = patch(backend, job, config={**raw, 'steps': 11})
    assert response.status_code == 200, response.text
    edited = response.json()
    after = h3._read(path)
    for key in ('source_video', 'reference_videos', 'reference_images', 'reference_audio'):
        assert after[key] == before[key]
    assert isinstance(after['source_video'], str) and after['source_name'] == 'guide.mp4'
    response = request(backend, 'rerun', [edited], {'steps': 9})
    assert response.status_code == 200, response.text
    duplicate = h3._read(manager.directory(response.json()['jobs'][0]['id']) / 'manifest.json')
    assert isinstance(duplicate['source_video'], str)
    assert Path(duplicate['source_video']).read_bytes() == b'guide'
    assert Path(duplicate['reference_videos'][0]).read_bytes() == b'motion'
    assert duplicate['source_name'] == 'guide.mp4'
    assert duplicate['input_names']['reference_images'] == ['jacket.png']
    assert len(duplicate['reference_audio']) == 1


def test_bulk_edit_persists_legacy_source_provenance_and_rerun_does_not_change_original(vfx):
    backend, manager, _ = vfx
    job, path = legacy(vfx)
    original = path.read_bytes()
    response = request(backend, 'rerun', [job], {'steps': 9})
    assert response.status_code == 200, response.text
    assert path.read_bytes() == original
    copied = h3._read(manager.directory(response.json()['jobs'][0]['id']) / 'manifest.json')
    assert copied['reference_videos'] == [] and isinstance(copied['source_video'], str)
    assert copied['source_name'] == 'original-guide.mp4'
    response = request(backend, 'edit', [job], {'steps': 8})
    assert response.status_code == 200, response.text
    saved = h3._read(path)
    assert saved['source_video'] == json.loads(original)['reference_videos'][0]
    assert saved['reference_videos'] == [] and saved['input_names']['source_video'] == ['original-guide.mp4']


def test_explicit_missing_source_never_promotes_reference_during_read_or_bulk(vfx):
    backend, manager, _ = vfx
    job, path = legacy(vfx)
    saved = h3._read(path)
    saved['source_video'] = []
    h3._write(path, saved)
    before = path.read_bytes()
    snapshot = _inputs(path.parent, saved, h3.UPLOAD_EXTENSIONS)
    assert snapshot['source_video'] == [] and len(snapshot['reference_videos']) == 1
    result = backend.client().get(f'/api/video/h3/jobs/{job["id"]}/edit').json()
    assert result['inputs']['source_video'] == [] and len(result['inputs']['reference_videos']) == 1
    response = request(backend, 'edit', [job], {'steps': 8})
    assert response.status_code == 400, response.text
    assert path.read_bytes() == before


def test_legacy_job_export_migrates_guide_before_empty_fields_and_preserves_archive_content(vfx):
    _, manager, _ = vfx
    job, path = legacy(vfx)
    before = path.read_bytes()
    doc, handles = workflow.job_export(manager, job['id'], 'corey', 'h3', workflow.export_options({'include_attachments': True}))
    assert doc['input_layout'] == 'source-guide-v1'
    assert [(row['field'], row['name']) for row in doc['attachments']] == [('source_video', 'original-guide.mp4')]
    assert b'video fixture' in b''.join(workflow.stream_zip(doc, handles))
    assert path.read_bytes() == before


@pytest.mark.parametrize('ffp,stacked', [(False, False), (True, True)])
def test_old_portable_vfx_reference_becomes_source_without_renaming_zip_path(tmp_path, ffp, stacked):
    service, stage = transfer(tmp_path)
    name = 'minimax_h3_vfx_edit_v1.0_r128' + ('_ffp' if ffp else '') + '.safetensors'
    config = {'mode': 'ref2va', **({'lora_strengths': [1]} if stacked else {'lora_scale': 1})}
    role = 'lora_0' if stacked else 'lora'
    member = 'attachments/reference_videos-1.mp4'
    doc = document(config=config, components={role: {'name': name}}, attachments=[{'field': 'reference_videos', 'name': 'guide.mp4', 'path': member, 'size': 5}])
    upload = archive(stage / 'upload.zip', doc, [(member, b'guide')])
    result = service.import_file(stage, 'corey', upload)
    assert result['workflow']['input_layout'] == 'source-guide-v1'
    assert result['workflow']['attachments'][0]['field'] == 'source_video'
    assert result['workflow']['attachments'][0]['path'] == member
    assert result['attachments'][0]['field'] == 'source_video'
    restored, name = service.attachment(stage.name, 0, 'corey')
    assert restored.read_bytes() == b'guide' and name == 'guide.mp4'


def test_new_portable_missing_source_keeps_real_reference_and_invalid_layout_is_rejected(tmp_path):
    service, stage = transfer(tmp_path)
    member = 'attachments/reference_videos-1.mp4'
    doc = document(input_layout='source-guide-v1', config={'mode': 'ref2va'}, components={'lora': {'name': 'minimax_h3_vfx_edit_v1.0_r128.safetensors'}}, attachments=[{'field': 'reference_videos', 'name': 'real-reference.mp4', 'path': member, 'size': 3}])
    result = service.import_file(stage, 'corey', archive(stage / 'upload.zip', doc, [(member, b'ref')]))
    assert result['attachments'][0]['field'] == 'reference_videos'
    assert result['workflow']['attachments'][0]['name'] == 'real-reference.mp4'
    with pytest.raises(ValueError, match='layout'):
        workflow.validate_document({**doc, 'input_layout': 'guess'}, 'h3')


def test_source_and_all_reference_channels_roundtrip_through_workflow_zip(vfx, tmp_path):
    _, manager, _ = vfx
    job, _ = modern(vfx)
    doc, handles = workflow.job_export(manager, job['id'], 'corey', 'h3', workflow.export_options({'include_attachments': True}))
    service, stage = transfer(tmp_path)
    source = stage / 'upload.zip'
    source.write_bytes(b''.join(workflow.stream_zip(doc, handles)))
    result = service.import_file(stage, 'corey', source)
    assert result['workflow']['input_layout'] == 'source-guide-v1'
    assert {item['field']: item['name'] for item in result['attachments']} == {
        'source_video': 'guide.mp4', 'reference_videos': 'motion.mp4',
        'reference_images': 'jacket.png', 'reference_audio': 'style.wav',
    }
    for index, item in enumerate(result['attachments']):
        path, _ = service.attachment(stage.name, index, 'corey')
        if item['field'] == 'source_video':
            assert path.read_bytes() == b'guide'
        elif item['field'] == 'reference_videos':
            assert path.read_bytes() == b'motion'


def test_disabled_vfx_adapter_does_not_reclassify_old_portable_reference():
    doc = document(config={'mode': 'ref2va', 'lora_strengths': [0]}, components={'lora_0': {'name': 'minimax_h3_vfx_edit_v1.0_r128.safetensors'}}, attachments=[{'field': 'reference_videos', 'name': 'reference.mp4', 'path': 'attachments/ref.mp4', 'size': 3}])
    workflow.validate_document(doc, 'h3')
    normalized = workflow.normalize_portable_inputs(doc)
    assert normalized['attachments'][0]['field'] == 'reference_videos'
    assert 'input_layout' not in doc
