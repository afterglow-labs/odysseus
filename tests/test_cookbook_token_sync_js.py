"""Exercise the production sync path: token transport, acknowledgement, and retry."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_token_save_waits_for_server_and_keeps_browser_storage_secret_free():
    source = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const storage = new Map();
const env = { servers: [{ name: 'Local', host: '' }], hfToken: '', hfTokenConfigured: false };
let reply = { ok: true };
let httpOk = true;
let release;
let posted;
const context = vm.createContext({
  console, window: {},
  document: { addEventListener() {} },
  localStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  },
  setTimeout() { return 1; }, clearTimeout() {},
  fetch: async (url, options) => {
    assert.equal(url, '/api/cookbook/state');
    posted = JSON.parse(options.body);
    await new Promise(resolve => { release = resolve; });
    return { ok: httpOk, status: httpOk ? 200 : 500, json: async () => reply };
  },
});
const collaborators = {
  './ui.js': { default: { showToast() {} } },
  './cookbook-diagnosis.js': { _diagnose() {}, _showDiagnosis() {}, _clearDiagnosis() {} },
  './escMenuStack.js': { registerMenuDismiss() {} },
  './cookbookProgressSignal.js': { computeProgressSignal() {} },
  './cookbookPorts.js': { portOf() {}, nextFreePort() {} },
  './toolWindowZOrder.js': { topPortalZ() {} },
  './cookbookGpu.js': { clearGpuMemory() {} },
};
const module = new vm.SourceTextModule(fs.readFileSync('static/js/cookbookRunning.js', 'utf8'), { context });
await module.link(specifier => {
  const exports = collaborators[specifier];
  assert.ok(exports, specifier);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await module.evaluate();
module.namespace.initRunning({ _envState: env, _loadPresets: () => [] });
await new Promise(resolve => setImmediate(resolve));
const token = 'hf_testpersistenttoken1234567890';
const tasks = [{ sessionId: 'cookbook-test', type: 'download', status: 'running',
  output: token, payload: { hf_token: token, repo_id: 'example/model' } }];
async function begin() {
  release = null;
  const saving = module.namespace._saveTasks(tasks, { immediate: true });
  while (!release) await Promise.resolve();
  return { saving };
}
function storageHasNoToken() {
  assert.ok([...storage.values()].every(value => !value.includes(token)));
}
env.hfToken = token;
let { saving } = await begin();
assert.equal(posted.env.hfToken, token, 'The token must reach server encryption');
assert.equal(posted.tasks[0].payload.hf_token, undefined);
assert.ok(!posted.tasks[0].output.includes(token));
assert.equal(env.hfTokenConfigured, false, 'No success before acknowledgement');
storageHasNoToken();
release();
await saving;
assert.equal(env.hfTokenConfigured, true);
assert.equal(env.hfToken, '', 'Forget the raw value after confirmed persistence');
storageHasNoToken();

({ saving } = await begin());
assert.equal(posted.env.hfToken, undefined, 'Later sync must preserve the server secret');
release();
await saving;

for (const failure of ['http', 'body']) {
  env.hfToken = token;
  env.hfTokenConfigured = false;
  httpOk = failure !== 'http';
  reply = { ok: false };
  ({ saving } = await begin());
  release();
  await assert.rejects(saving, /could not save|Could not save/);
  assert.equal(env.hfTokenConfigured, false);
  assert.equal(env.hfToken, token, 'Keep the pending value available to retry');
  storageHasNoToken();
}
httpOk = true;
reply = { ok: true };
({ saving } = await begin());
release();
await saving;
assert.equal(env.hfTokenConfigured, true, 'Failed saves must not poison the queue');
storageHasNoToken();
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module", "-e", source],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
