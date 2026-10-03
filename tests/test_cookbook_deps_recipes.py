"""Guard the llama.cpp Docker pull recipe surfaced in Cookbook → Dependencies.

The upstream repo moved from github.com/ggerganov/llama.cpp to
github.com/ggml-org/llama.cpp. The old GHCR namespace
(ghcr.io/ggerganov/llama.cpp) no longer publishes images, so the
docker variant in the Dependencies panel returned
"failed to resolve reference … not found" when copied verbatim (#4457).
The other llama.cpp reference in routes/cookbook_routes.py already uses
ggml-org; this guards the JS recipe so the two stay aligned.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

RECIPES_JS = (
    Path(__file__).resolve().parent.parent / "static" / "js" / "cookbook-deps-recipes.js"
)


def test_llama_cpp_docker_recipe_uses_ggml_org_namespace():
    source = RECIPES_JS.read_text(encoding="utf-8")

    assert "ghcr.io/ggml-org/llama.cpp:server-cuda" in source, (
        "Expected the llama.cpp docker recipe to pull from the ggml-org namespace."
    )
    assert "ghcr.io/ggerganov/llama.cpp" not in source, (
        "The ggerganov GHCR namespace no longer publishes llama.cpp images. "
        "Use ghcr.io/ggml-org/llama.cpp:server-cuda."
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_llama_cpp_recipe_matches_selected_platform_and_scanned_hardware():
    script = r"""
import assert from 'node:assert/strict';
import { pickRecipe, recipeCommands } from './static/js/cookbook-deps-recipes.js';
const recipe = pickRecipe('llama_cpp', 'example/model.gguf');
const hardware = { _scannedHost: 'cuda-box', system: { backend: 'cuda' } };
const linuxCuda = { platform: 'linux', host: 'cuda-box', hardware };
const remoteMac = { platform: 'darwin', host: 'mac-box', hardware };
const localWindows = { platform: 'windows', host: '', hardware };
const commands = (variant, target) => recipeCommands(recipe, variant, target).join('\n');

assert.match(commands('pip', remoteMac), /GGML_METAL=on/);
assert.doesNotMatch(commands('pip', remoteMac), /CUDA|\buv\b/);
assert.match(commands('pip', remoteMac), /^python -m pip install/);
assert.match(commands('pip', remoteMac), /--no-binary=llama-cpp-python/);
assert.equal(commands('docker', remoteMac), 'docker pull ghcr.io/ggml-org/llama.cpp:server');
assert.match(commands('pip', linuxCuda), /GGML_CUDA=on/);
assert.equal(commands('docker', linuxCuda), 'docker pull ghcr.io/ggml-org/llama.cpp:server-cuda');

// A server switch must not reuse CUDA from a scan of the previous target.
for (const target of [localWindows, { ...linuxCuda, host: 'cpu-box' }, {},
                      { ...linuxCuda, platform: '' }]) {
  assert.doesNotMatch(commands('pip', target), /CUDA|METAL|CMAKE_ARGS|\buv\b/);
  assert.equal(commands('docker', target), 'docker pull ghcr.io/ggml-org/llama.cpp:server');
}
assert.match(commands('pip', { platform: 'macos', host: '' }), /GGML_METAL=on/);
assert.match(commands('pip', { host: '', hardware: { _scannedHost: '', system: { backend: 'metal' } } }), /GGML_METAL=on/);
// Ordinary recipes and missing-variant fallback retain their existing commands.
const diffusers = pickRecipe('diffusers', 'example/model');
assert.deepEqual(recipeCommands(diffusers, 'docker', remoteMac), recipeCommands(diffusers, 'pip'));
assert.deepEqual(recipeCommands(null, 'pip', linuxCuda), []);
for (const backend of ['diffusers', 'krea_diffusers']) {
  assert.match(recipeCommands(pickRecipe(backend, ''), 'pip').join(' '), /\btransformers\b/);
}
"""
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, text=True,
        capture_output=True, cwd=RECIPES_JS.parents[2], timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
