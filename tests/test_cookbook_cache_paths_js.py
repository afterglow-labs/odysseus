"""Cookbook cache defaults must follow the actual local server, not browser HOME."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def normalize(state):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the UI state regression')
    source = (ROOT / 'static/js/cookbookRunning.js').read_text()
    function = source.split('function _normalizeState(state) {', 1)[1].split('\nexport async function _syncFromServer', 1)[0]
    script = 'function _normalizeState(state) {' + function + '\nconsole.log(JSON.stringify(_normalizeState(' + json.dumps(state) + ')));'
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def test_local_cache_default_does_not_reinsert_global_home():
    cache = '/home/corey/projects/odysseus/cache/huggingface/hub'
    state = {'env': {'localHfCacheDir': cache, 'servers': [
        {'host': '', 'modelDirs': ['~/.cache/huggingface/hub', cache, '/mnt/e/models'], 'downloadDir': cache},
        {'host': 'remote.example', 'modelDirs': ['/srv/models'], 'downloadDir': '/srv/models'},
    ]}}
    local, remote = normalize(state)['env']['servers']
    assert local['modelDirs'] == [cache, '/mnt/e/models']
    assert local['downloadDir'] == cache
    assert remote['modelDirs'] == ['~/.cache/huggingface/hub', '/srv/models']
    assert remote['downloadDir'] == '/srv/models'


def test_legacy_default_target_resolves_and_explicit_extra_cache_survives():
    cache = '/private/huggingface/hub'
    local = normalize({'env': {'localHfCacheDir': cache, 'servers': [
        {'host': 'local', 'modelDir': '~/.cache/huggingface/hub',
         'modelDirs': ['/home/user/.cache/huggingface/hub'], 'downloadDir': '~/.cache/huggingface/hub'},
    ]}})['env']['servers'][0]
    assert set(local['modelDirs']) == {cache, '/home/user/.cache/huggingface/hub'}
    assert local['downloadDir'] == cache
    assert 'modelDir' not in local


def test_missing_server_metadata_keeps_compatible_default():
    local = normalize({'env': {'servers': [{'host': '', 'modelDirs': []}]}})['env']['servers'][0]
    assert local['modelDirs'] == ['~/.cache/huggingface/hub']
