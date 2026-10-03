"""Run the actual retry handler, including task replacement and request routing."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_dependency_retry_preserves_command_target_and_uses_install_runner():
    source = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const storage = new Map();
const requests = [];
const toasts = [];
let failLaunch = false;
const context = vm.createContext({
  console, window: {},
  document: {
    visibilityState: 'hidden', addEventListener() {},
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; },
  },
  localStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: key => storage.delete(key),
  },
  setTimeout() { return 1; }, clearTimeout() {},
  setInterval() { return 1; }, clearInterval() {},
  fetch: async (url, options) => {
    const body = JSON.parse(options.body);
    requests.push({ url, body });
    if (url === '/api/shell/exec') return { ok: true, json: async () => ({ exit_code: 0 }) };
    assert.ok(['/api/model/serve', '/api/model/download'].includes(url));
    return failLaunch
      ? { ok: false, status: 400, json: async () => ({ detail: 'Invalid install target' }) }
      : { ok: true, json: async () => ({ ok: true, session_id: 'retry-new' }) };
  },
});
const collaborators = {
  './ui.js': { default: { showToast: message => toasts.push(message) } },
  './cookbook-diagnosis.js': { _diagnose() {}, _showDiagnosis() {}, _clearDiagnosis() {} },
  './escMenuStack.js': { registerMenuDismiss() {} },
  './cookbookProgressSignal.js': { computeProgressSignal() {} },
  './cookbookPorts.js': { portOf() {}, nextFreePort() {} },
  './toolWindowZOrder.js': { topPortalZ() {} },
};
const filename = path.resolve('static/js/cookbookRunning.js');
// Expose the private click handler and configure only its injected host helpers;
// all retry, request construction, persistence, and rendering code stays real.
const module = new vm.SourceTextModule(fs.readFileSync(filename, 'utf8') + `
_isWindows = () => false;
_getPort = task => task?.sshPort || '';
_sshPrefix = port => port ? '-p ' + port + ' ' : '';
_getPlatform = () => 'darwin';
_envState = { remoteHost: 'unrelated-selected-server', servers: [] };
export { _retryTask };
`, { context, identifier: filename });
await module.link(specifier => {
  const exports = collaborators[specifier];
  assert.ok(exports, 'Unexpected import: ' + specifier);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await module.evaluate();
const running = module.namespace;
const install = {
  id: 'old-install', sessionId: 'old-install', name: 'pip playwright',
  type: 'download', status: 'crashed', output: 'Downloading playwright.whl 100%',
  progress: '100%', exit_code: 1, _backendDiagnosis: { message: 'old failure' },
  payload: { _dep: true, repo_id: 'playwright', _cmd: 'python3 -m pip install playwright', remote_host: '' },
};
async function retry(task) {
  storage.clear(); requests.length = 0; toasts.length = 0;
  storage.set('cookbook-tasks', JSON.stringify([task]));
  await running._retryTask(null, task);
  return JSON.parse(storage.get('cookbook-tasks'));
}
let tasks = await retry(install);
assert.deepEqual(requests.map(r => r.url), ['/api/shell/exec', '/api/model/serve']);
assert.deepEqual(requests[1].body, { repo_id: 'playwright', cmd: install.payload._cmd });
assert.equal(tasks.length, 1, 'Retry replaces the old card rather than creating a duplicate');
assert.equal(tasks[0].sessionId, 'retry-new');
assert.equal(tasks[0].name, 'pip playwright');
assert.equal(tasks[0].type, 'download');
assert.equal(tasks[0].payload._dep, true);
assert.equal(tasks[0].status, 'running');
assert.equal(tasks[0].output, '');
assert.equal(tasks[0].progress, '');
assert.equal(tasks[0].exit_code, null);
assert.equal(tasks[0]._backendDiagnosis, null);
assert.equal(tasks[0]._retrying, false);
assert.equal(tasks[0].remoteHost, '');
assert.ok(toasts.some(t => t.startsWith('Installing')));
assert.ok(toasts.every(t => !/HuggingFace|download|cached/i.test(t)));

// A saved remote conda target must survive a changed current UI selection.
const remote = {
  ...install, remoteHost: 'gpu-box', sshPort: '2224', platform: 'linux',
  payload: { ...install.payload, remote_host: 'gpu-box', env_path: 'video-tools',
    env_prefix: 'eval "$(conda shell.bash hook)" && conda activate video-tools' },
};
tasks = await retry(remote);
assert.match(requests[0].body.command, /ssh -p 2224 gpu-box/);
assert.deepEqual(requests[1].body, {
  repo_id: 'playwright', cmd: remote.payload._cmd, remote_host: 'gpu-box',
  ssh_port: '2224', platform: 'linux', env_prefix: remote.payload.env_prefix,
});
assert.equal(tasks[0].payload.env_prefix, remote.payload.env_prefix);
assert.equal(tasks[0].payload.ssh_port, '2224');
assert.equal(tasks[0].remoteHost, 'gpu-box');

// If the old card disappeared during the request, newly created local cards
// must also ignore whichever remote server is currently selected in the UI.
storage.clear(); requests.length = 0;
await running._retryDownload('pip playwright', install.payload, 'missing-old-card');
tasks = JSON.parse(storage.get('cookbook-tasks'));
assert.equal(tasks[0].remoteHost, '');
assert.equal(requests[0].url, '/api/model/serve');

// Existing venv cards already name their Python; uncertain legacy activation
// must not silently reinstall into an unrelated default interpreter.
await retry({ ...install, payload: { ...install.payload, env_path: '/opt/venv', _cmd: '/opt/venv/bin/python3 -m pip install playwright' } });
assert.equal(requests[1].body.cmd, '/opt/venv/bin/python3 -m pip install playwright');
tasks = await retry({ ...install, payload: { ...install.payload, env_path: 'old-conda-env' } });
assert.equal(requests.length, 1);
assert.equal(tasks[0].status, 'crashed');
assert.match(toasts.at(-1), /Use Install in Dependencies/);
tasks = await retry({ ...install, payload: { ...install.payload, env_path: 'torch', _cmd: 'python3 -m pip install torch' } });
assert.equal(requests.length, 1, 'A package name matching the env name does not select its Python');
assert.match(toasts.at(-1), /Use Install in Dependencies/);

failLaunch = true;
tasks = await retry(install);
assert.equal(tasks[0].sessionId, 'old-install');
assert.equal(tasks[0].status, 'crashed');
assert.equal(tasks[0]._retrying, false);
assert.equal(toasts.at(-1), 'Install failed: Invalid install target');

// Genuine model retries keep the HF endpoint, resume option, and model wording.
failLaunch = false;
await retry({ ...install, name: 'org/model', payload: { repo_id: 'org/model' } });
assert.equal(requests[1].url, '/api/model/download');
assert.equal(requests[1].body.disable_hf_transfer, true);
assert.ok(toasts.some(t => /HuggingFace/.test(t)));
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=source, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
