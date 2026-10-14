"""Metadata discovery uses real package declarations in a CPU-only subprocess."""
import json
import os
from pathlib import Path
import sys

import pytest

from src.comfy_node_manifest import (
    FORMAT, PROJECT, convert_object_info, discover_node_package,
    load_manifests, source_receipt, verify_source,
)


@pytest.fixture
def runtime(tmp_path):
    value = tmp_path / 'runtime'
    value.mkdir()
    return value


def package(tmp_path, source, name='custom'):
    folder = tmp_path / name
    folder.mkdir()
    (folder / '__init__.py').write_text(source)
    return folder


FAKE = '''
import os
import sys
assert '--cpu' in sys.argv
assert os.environ.get('CUDA_VISIBLE_DEVICES') == ''
assert 'ODYSSEUS_TEST_PRIVATE_TOKEN' not in os.environ
class Example:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'image': ('IMAGE',), 'scale': ('FLOAT', {'default': 1.5, 'min': -5, 'max': 5, 'step': .25}),
            'mode': (['first', 'second'], {'default': 'second'}),
            'forced': ('INT', {'default': 12, 'forceInput': True}),
            'text': ('STRING', {'multiline': True, 'default': 'hello'})},
            'optional': {'latent': ('LATENT', {'lazy': True})}, 'hidden': {'unique_id': 'UNIQUE_ID', 'prompt': 'PROMPT'}}
    RETURN_TYPES = ('IMAGE', 'FLOAT')
    RETURN_NAMES = ('processed', 'scale_used')
    OUTPUT_IS_LIST = (True, False)
    INPUT_IS_LIST = True
    OUTPUT_NODE = True
    FUNCTION = 'run'
    CATEGORY = 'custom/example'
    @classmethod
    def VALIDATE_INPUTS(cls, scale, input_types=None):
        raise AssertionError('Discovery must not run validation hooks')
    @classmethod
    def IS_CHANGED(cls, **kwargs):
        raise AssertionError('Discovery must not run cache hooks')
    def run(self, **kwargs):
        raise AssertionError('Discovery must not execute node functions')
class Broken:
    @classmethod
    def INPUT_TYPES(cls):
        raise ValueError('sensitive-token-never-print')
NODE_CLASS_MAPPINGS = {'Example': Example, 'Broken': Broken}
NODE_DISPLAY_NAME_MAPPINGS = {'Example': 'Friendly Example'}
print('sensitive-token-never-print')
'''


def test_legacy_metadata_preserves_widgets_lists_hooks_hidden_and_receipt(tmp_path, runtime, monkeypatch):
    monkeypatch.setenv('ODYSSEUS_TEST_PRIVATE_TOKEN', 'sensitive-token-never-print')
    path = package(tmp_path, FAKE)
    manifest = discover_node_package(path, python_path=sys.executable, runtime_path=runtime)
    assert manifest['format'] == FORMAT
    assert manifest['status'] == 'partial'
    assert manifest['execution_status'] == 'metadata_only'
    assert 'sensitive-token-never-print' not in json.dumps(manifest)
    assert manifest['source']['package_path'] == str(path)
    assert manifest['source']['fingerprint'] == source_receipt(path)['fingerprint']
    node = manifest['nodes'][0]
    assert node['display_name'] == 'Friendly Example'
    assert node['category'] == 'custom/example'
    assert node['function'] == 'run'
    assert node['output_node'] and node['input_is_list']
    assert node['outputs'] == [{'index': 0, 'name': 'processed', 'type': 'IMAGE', 'is_list': True}, {'index': 1, 'name': 'scale_used', 'type': 'FLOAT', 'is_list': False}]
    fields = {row['name']: row for row in node['inputs']}
    assert fields['scale']['default'] == 1.5
    assert fields['scale']['metadata']['min'] == -5
    assert fields['mode']['choices'] == ['first', 'second']
    assert fields['mode']['default'] == 'second'
    assert fields['forced']['force_input'] and not fields['forced']['is_widget']
    assert fields['latent']['section'] == 'optional' and fields['latent']['metadata']['lazy']
    assert fields['unique_id']['hidden'] and fields['unique_id']['type'] == 'UNIQUE_ID'
    assert node['hooks']['validate_inputs']['parameters'][0]['name'] == 'scale'
    assert node['hooks']['is_changed']['present']
    assert node['raw_schema']['input']['required']['text'][1]['multiline']
    assert manifest['issues'][0]['class_type'] == 'Broken'
    assert os.environ['ODYSSEUS_TEST_PRIVATE_TOKEN'] == 'sensitive-token-never-print'
    assert 'custom' not in sys.modules
    assert verify_source(manifest)['fingerprint'] == manifest['source']['fingerprint']
    (path / 'helper.py').write_text('changed = True')
    with pytest.raises(ValueError, match='changed'):
        verify_source(manifest)


def test_relative_package_import_and_timeout_do_not_affect_parent(tmp_path, runtime):
    folder = package(tmp_path, 'from .declarations import NODE_CLASS_MAPPINGS')
    (folder / 'declarations.py').write_text(FAKE)
    assert discover_node_package(folder, python_path=sys.executable, runtime_path=runtime)['nodes'][0]['class_type'] == 'Example'
    slow = package(tmp_path, 'import time\ntime.sleep(10)', 'slow')
    assert discover_node_package(slow, python_path=sys.executable, runtime_path=runtime, timeout=.1)['issues'][0]['code'] == 'timeout'
    missing = package(tmp_path, 'import nonexistent_odysseus_test_dependency', 'missing')
    manifest = discover_node_package(missing, python_path=sys.executable, runtime_path=runtime)
    assert manifest['status'] == 'failed'
    assert manifest['issues'][0]['missing_module'] == 'nonexistent_odysseus_test_dependency'


def test_object_info_preserves_source_and_is_explicitly_unbound(tmp_path):
    source = {'RemoteClass': {'input': {'required': {'choose': [['x', 'y'], {'default': 'y'}], 'count': ['INT', {'default': 4, 'control_after_generate': True}]}, 'hidden': {'id': 'UNIQUE_ID'}},
                             'input_order': {'required': ['count', 'choose']}, 'output': ['LATENT'], 'output_name': ['result'], 'output_is_list': [True], 'is_input_list': True, 'category': 'remote', 'output_node': True}}
    manifest = convert_object_info(source)
    assert manifest['status'] == 'ready'
    assert manifest['execution_status'] == 'unbound'
    assert manifest['raw_source'] == source
    node = manifest['nodes'][0]
    assert node['inputs'][0]['name'] == 'count'
    assert node['inputs'][0]['metadata']['control_after_generate']
    assert node['input_is_list'] and node['outputs'][0]['is_list']
    assert node['package_id'] is None
    with pytest.raises(ValueError, match='no installed'):
        verify_source(manifest)
    (tmp_path / 'valid.json').write_text(json.dumps(manifest))
    (tmp_path / 'bad.json').write_text('not json')
    assert load_manifests(tmp_path) == [manifest]


def test_v3_projection_with_extension_entrypoint(tmp_path, runtime):
    folder = package(tmp_path, '''
class Schema: node_id = 'ModernExample'
class Example:
    FUNCTION = 'execute'
    @classmethod
    def GET_SCHEMA(cls): return Schema()
    @classmethod
    def GET_NODE_INFO_V1(cls): return {'input': {'required': {'size': ('INT', {'default': 32, 'min': 1})}}, 'output': ('IMAGE',), 'output_name': ('image',), 'output_is_list': (False,), 'display_name': 'Modern Example', 'category': 'modern', 'output_node': True}
    @classmethod
    def execute(cls, **kwargs): raise AssertionError('must not execute')
class Extension:
    async def on_load(self): pass
    async def get_node_list(self): return [Example]
async def comfy_entrypoint(): return Extension()
''')
    manifest = discover_node_package(folder, python_path=sys.executable, runtime_path=runtime)
    assert manifest['status'] == 'ready'
    assert manifest['api'] == 'v3'
    assert manifest['nodes'][0]['class_type'] == 'ModernExample'
    assert manifest['nodes'][0]['function'] == 'execute'
    assert manifest['nodes'][0]['inputs'][0]['default'] == 32


def test_real_seed_scout_cpu_metadata_without_render():
    source = PROJECT / 'runtimes/minimax-h3/seed-scout'
    runtime = PROJECT / 'runtimes/minimax-h3/ComfyUI'
    python = PROJECT / 'venv/bin/python'
    if not (source / 'nodes.py').is_file() or not (runtime / 'nodes.py').is_file() or not python.is_file():
        pytest.skip('Private Seed Scout/Comfy runtime is not installed')
    manifest = discover_node_package(source, python_path=python, runtime_path=runtime, timeout=60)
    assert manifest['status'] == 'ready', manifest['issues']
    node = next(node for node in manifest['nodes'] if node['class_type'] == 'MiniMaxH3SeedScoutSampler')
    fields = {row['name']: row for row in node['inputs']}
    assert fields['guider']['type'] == 'GUIDER'
    assert fields['mode']['choices'] == ['interactive', 'scout', 'final']
    assert fields['selection_timeout']['default'] == 0
    assert fields['unique_id']['hidden']
    assert node['function'] == 'run'
    assert node['output_node']
    assert node['hooks']['is_changed']['present']
    assert manifest['discovery']['cpu_only'] and not manifest['discovery']['server_started']
