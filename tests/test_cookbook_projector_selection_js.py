"""Exercise main-model selection and execute its fallback against real files."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_projectors_never_become_main_models_even_with_legacy_or_incomplete_scans():
    source = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';

const context = vm.createContext({ console, document: { addEventListener() {} } });
const dependencies = {
  './ui.js': { default: {} }, './spinner.js': { default: {} },
  './providers.js': { providerLogo() {} }, './chatRenderer.js': { modelColor() {} },
  './escMenuStack.js': { bindMenuDismiss() {}, dismissOrRemove() {} },
  './cookbook-diagnosis.js': { openCookbookDependencies() {} },
  './cookbook-hwfit.js': { _hwfitCache: null },
  './toolWindowZOrder.js': { topPortalZ() {} },
  './cookbookGpu.js': { clearGpuMemory() {} },
  './cookbookGpuSelection.js': { gpuVisibility() {}, gpuButtonLabel() {} },
  './h3Video.js': { isH3VideoComponent() {}, showH3Video() {} },
  './bfsVideo.js': { isBfsVideoModel() {}, showBfsVideo() {} },
};
const mod = new vm.SourceTextModule(fs.readFileSync('static/js/cookbookServe.js', 'utf8') + `
_shellQuote = value => "'" + value.replace(/'/g, "'\\\"'\\\"'") + "'";
export { _runnableGgufFiles, _projectorGgufFiles, _mainGgufPathExpr, _localModelPath, _cachedArtifactPath, _artifactRepoURL, _modelForCachedCard };
`, { context });
await mod.link(specifier => {
  const exports = dependencies[specifier];
  assert.ok(exports, 'Unexpected import: ' + specifier);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await mod.evaluate();
const { _runnableGgufFiles: mainFiles, _projectorGgufFiles: projectors, _mainGgufPathExpr: expr } = mod.namespace;
const weights = { rel_path: 'z-model-Q8_0.gguf', role: 'model' };
const projector = { rel_path: 'aaa-mmproj-F32.gguf', role: 'projector' };
assert.equal(mainFiles({ gguf_files: [projector] }).length, 0, 'Projector-only downloads cannot launch');
for (const role of [undefined, 'model', 'projector']) {
  const oldScan = { gguf_files: [{ rel_path: 'rev/MMPROJ-F32.GGUF', role }, weights] };
  assert.deepEqual(Array.from(mainFiles(oldScan), f => f.rel_path), [weights.rel_path]);
  assert.equal(projectors(oldScan).length, 1, 'Legacy projectors remain available for vision');
}
assert.equal(mainFiles({ gguf_files: [{ rel_path: 'weights-F32.gguf' }] }).length, 1, 'F32 alone does not imply a projector');
assert.equal(mainFiles({ gguf_files: [{ rel_path: 'encoder.gguf', role: 'projector' }] }).length, 0);
const gemma = { gguf_files: [
  { rel_path: 'rev/gemma-4-26B-it-mmproj.gguf', role: 'model' },
  { rel_path: 'rev/gemma-4-26B_q4_0-it.gguf', role: 'model' },
] };
assert.deepEqual(Array.from(projectors(gemma), f => f.rel_path), ['rev/gemma-4-26B-it-mmproj.gguf']);
assert.deepEqual(Array.from(mainFiles(gemma), f => f.rel_path), ['rev/gemma-4-26B_q4_0-it.gguf']);

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'odysseus-projector-'));
const repo = 'Vision Model';
const dir = path.join(temp, repo);
fs.mkdirSync(dir);
fs.writeFileSync(path.join(dir, projector.rel_path), 'projector');
const model = { is_local_dir: true, path: temp, gguf_files: [projector] };
const resolve = expression => execFileSync('bash', ['-c', `printf '%s' "${expression}"`], { encoding: 'utf8' });
try {
  assert.equal(resolve(expr(model, repo, projector.rel_path)), '', 'Filesystem fallback excludes projector-only directories');
  fs.writeFileSync(path.join(dir, weights.rel_path), 'model');
  assert.equal(resolve(expr(model, repo, projector.rel_path)), path.join(dir, weights.rel_path), 'Stale projector selection resolves only main weights');
  model.gguf_files.push(weights);
  assert.equal(resolve(expr(model, repo, weights.rel_path)), path.join(dir, weights.rel_path));
  fs.writeFileSync(path.join(dir, 'split-Q4-00001-of-00002.gguf'), 'first part');
  fs.writeFileSync(path.join(dir, 'split-Q4-00002-of-00002.gguf'), 'second part');
  assert.equal(resolve(expr(model, repo, '')), path.join(dir, 'split-Q4-00001-of-00002.gguf'), 'Legacy lookup prefers the first split');
  const moved = {...model, repo_id: 'owner/vision', named_layout: true, model_path: dir, path: '/different/root'};
  assert.equal(resolve(expr(moved, moved.repo_id, weights.rel_path)), path.join(dir, weights.rel_path), 'Named downloads use their exact model_path');
  assert.equal(mod.namespace._localModelPath(moved), dir);
  assert.equal(mod.namespace._cachedArtifactPath(moved, {rel_path:'loras/style.safetensors'}), path.join(dir, 'loras/style.safetensors'));
  assert.equal(mod.namespace._artifactRepoURL(moved, {rel_path:'workflows/a b.json',revision:'rev1'}), 'https://huggingface.co/owner/vision/blob/rev1/workflows/a%20b.json');
  const sibling = {...moved, model_path: dir + '/different'};
  assert.equal(mod.namespace._modelForCachedCard([sibling,moved], moved.repo_id, {dataset:{cachePath:moved.path,modelPath:dir}}), moved, 'Cards for the same repository resolve their own files');
} finally { fs.rmSync(temp, { recursive: true, force: true }); }
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", source],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
