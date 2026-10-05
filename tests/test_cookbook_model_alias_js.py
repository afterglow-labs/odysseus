"""A generated Cookbook alias must not trigger a different default model."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_existing_cookbook_alias_remains_available_on_its_endpoint():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the model picker behavior test")
    source = (Path(__file__).resolve().parents[1] / "static/js/modelPicker.js").read_text()
    function = source[source.index("function _modelExists("):source.index("function _firstAvailableModel(")]
    script = """
const assert = require('node:assert/strict');
const item = {url: 'http://localhost:8001/v1/chat/completions',
  models: ['/cache/model-Q8.gguf'], model_aliases: {'Example/Model-GGUF': '/cache/model-Q8.gguf'}};
global.window = {modelsModule: {getCachedItems: () => [item]}};
""" + function + """
assert.equal(_modelExists('Example/Model-GGUF', item.url), true);
assert.equal(_modelExists('/cache/model-Q8.gguf', item.url), true);
assert.equal(_modelExists('Example/Model-GGUF', 'http://other/v1/chat/completions'), false);
assert.equal(_modelExists('/cache/model-Q4.gguf', item.url), false);
item.offline = true;
assert.equal(_modelExists('Example/Model-GGUF', item.url), false);
item.offline = false;
item.models = ['another-model'];
assert.equal(_modelExists('Example/Model-GGUF', item.url), false);
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
