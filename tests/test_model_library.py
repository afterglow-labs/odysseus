"""Readable models retain identity, saved selections, and portable workflows."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from src import model_library as library, h3_video as h3, bfs_video as bfs
from src.video_job_edit import _draft
from src.video_workflow import component_reference
from tests.test_h3_video import weights, inventory, config


@pytest.mark.parametrize('root,expected', [
    ('~/models', '~/models/author/model'),
    ('/models/', '/models/author/model'),
    (r'E:\AI\Models', 'E:/AI/Models/author/model'),
])
def test_named_destination_preserves_selected_host_path(root, expected):
    assert library.named_model_directory(root, 'author/model') == expected


@pytest.mark.parametrize('repo', ['../escape', 'author/../escape', '/absolute', 'author/repo;touch'])
def test_invalid_repository_cannot_escape_library(tmp_path, repo):
    with pytest.raises(ValueError):
        library.named_model_directory(tmp_path, repo)


def test_older_client_download_roots_keep_migrated_h3_policy(monkeypatch):
    from routes.cookbook_routes import _local_named_download_root
    from src.hf_cache import normalize_local_cache_settings
    monkeypatch.setenv('ODYSSEUS_MODEL_DIR_ALIASES', json.dumps({'/old/hub': '/new/models'}))
    monkeypatch.setenv('ODYSSEUS_H3_MODEL_DIR', '/linux/models')
    assert _local_named_download_root('/old/hub', '/new/models', 'author/MiniMax-H3') == '/linux/models'
    assert _local_named_download_root('/old/hub', '/new/models', 'author/chat') == '/new/models'
    assert _local_named_download_root('/custom', '/new/models', 'author/MiniMax-H3') == '/custom'
    environment = {'servers': [{'host': '', 'downloadDir': '/old/hub', 'modelDirs': ['/old/hub']},
                               {'host': 'remote', 'downloadDir': '/old/hub'}]}
    normalize_local_cache_settings(environment)
    assert environment['servers'][0]['downloadDir'] == '/new/models'
    assert environment['servers'][0]['modelDirs'] == ['/old/hub']
    assert environment['servers'][1]['downloadDir'] == '/old/hub'


def test_bfs_shared_runtime_does_not_offer_wan_vae_as_h3(tmp_path):
    root = tmp_path / 'runtimes/minimax-h3/models'
    weights(root / 'bfs-shared/wan_2.1_vae.safetensors')
    weights(root / 'minimax_h3_video_vae.safetensors')
    assert [c['name'] for c in h3.discover_components([root])] == ['minimax_h3_video_vae.safetensors']


def test_h3_inventory_uses_configured_library_without_saved_cookbook_state(tmp_path, monkeypatch):
    preferred = tmp_path / 'h3-library'
    weights(preferred / 'author/MiniMax-H3/minimax_h3_video_vae.safetensors')
    monkeypatch.setenv('ODYSSEUS_H3_MODEL_DIR', str(preferred))
    roots = h3.cache_roots(tmp_path / 'project', tmp_path / 'missing-state.json')
    assert roots[0] == preferred
    assert h3.discover_components(roots)[0]['path'].startswith(str(preferred) + '/')


def test_metadata_merges_concurrent_files_and_refuses_identity_overwrite(tmp_path):
    def write(index):
        library.write_model_metadata(tmp_path, 'author/model', {f'{index}.gguf': {'size': index}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    assert len(library.read_model_metadata(tmp_path)['files']) == 12
    before = (tmp_path / library.METADATA_NAME).read_bytes()
    with pytest.raises(ValueError, match='different model'):
        library.write_model_metadata(tmp_path, 'another/repository')
    assert (tmp_path / library.METADATA_NAME).read_bytes() == before


def test_portable_named_reference_tracks_updated_hf_file_and_hides_server_paths(tmp_path):
    root = tmp_path / 'author/model'
    path = weights(root / 'vae/minimax_h3_video_vae.safetensors')
    old = str(tmp_path / 'old/blob')
    library.write_model_metadata(root, 'author/model', {
        'vae/' + path.name: {'revision': 'a' * 40, 'source_paths': [old], 'size': path.stat().st_size},
    })
    expected = {'name': path.name, 'repository': 'author/model',
                'relative_path': 'vae/' + path.name, 'revision': 'a' * 40}
    assert component_reference(path) == expected
    assert library.component_aliases(path) == [hashlib.sha256(old.encode()).hexdigest()[:32]]
    metadata = root / '.cache/huggingface/download/vae' / (path.name + '.metadata')
    metadata.parent.mkdir(parents=True)
    metadata.write_text('b' * 40 + '\nsha\n0.0\n')
    assert component_reference(path) == {**expected, 'revision': 'b' * 40}
    assert old not in json.dumps(component_reference(path))
    blob = tmp_path / 'hub/blobs' / ('d' * 64)
    blob.parent.mkdir(parents=True)
    blob.symlink_to(path)
    assert component_reference(blob) == {**expected, 'revision': 'b' * 40}


def test_legacy_hf_reference_survives_readable_metadata_support(tmp_path):
    path = tmp_path / 'models--author--model/snapshots' / ('c' * 40) / 'model.gguf'
    assert component_reference(path) == {'name': 'model.gguf', 'repository': 'author/model',
                                         'relative_path': 'model.gguf', 'revision': 'c' * 40}


def test_h3_inventory_prefers_linux_migrated_adapter_over_retained_nas_copy(tmp_path):
    linux, nas = tmp_path / 'models', tmp_path / 'nas'
    old = weights(nas / 'minimax_h3_head_swap.safetensors', True)
    root = linux / 'author/MiniMax-H3'
    new = weights(root / old.name, True)
    old_id = hashlib.sha256(str(old).encode()).hexdigest()[:32]
    library.write_model_metadata(root, 'author/MiniMax-H3', {old.name: {'source_paths': [str(old)]}})
    components = h3.discover_components([linux, nas])
    assert len(components) == 1
    assert components[0]['path'] == str(new)
    assert old_id in components[0]['aliases']
    saved = {'mode': 'ref2va', 'lora': str(old), 'lora_scale': .5,
             'loras': [{'path': str(old), 'strength': .5}]}
    draft = _draft(saved, {'components': components}, 'h3')
    assert draft['lora'] == components[0]['id']
    assert draft['loras'] == [{'id': components[0]['id'], 'strength': .5}]


def test_h3_old_client_component_ids_select_current_readable_paths(inventory):
    raw = config(inventory)
    for component in inventory['components']:
        old_id = 'previous-' + component['id']
        component['aliases'] = [old_id]
        if raw.get(component['role']) == component['id']:
            raw[component['role']] = old_id
    validated = h3.validate_config(raw, inventory['components'], inventory['gpus'], {})
    assert all(Path(validated[role]).is_file() for role in ('model', 'encoder', 'video_vae', 'audio_vae'))
