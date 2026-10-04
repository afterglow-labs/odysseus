"""Exercise real task reconciliation while pip resolves, downloads, and exits."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_dependency_install_waits_for_terminal_success():
    source = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const storage = new Map();
let liveTask;
let statusRequests = 0;
let livenessRequests = 0;
let duringLiveness = null;
const context = vm.createContext({
  console,
  window: {},
  document: {
    visibilityState: 'visible', addEventListener() {},
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll() { return []; },
  },
  localStorage: {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: key => storage.delete(key),
  },
  setTimeout() { return 1; }, clearTimeout() {},
  fetch: async url => {
    if (url === '/api/shell/exec') {
      livenessRequests++;
      duringLiveness?.();
      return { ok: true, json: async () => ({ exit_code: 0 }) };
    }
    if (url === '/api/cookbook/state') return { ok: true, json: async () => ({ tasks: [] }) };
    assert.equal(url, '/api/cookbook/tasks/status');
    statusRequests++;
    return { ok: true, json: async () => ({ tasks: [liveTask] }) };
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
// Load the complete production module. Expose private entry points only to this
// VM so reconciliation and storage normalization execute together unchanged.
const filename = path.resolve('static/js/cookbookRunning.js');
const module = new vm.SourceTextModule(fs.readFileSync(filename, 'utf8') +
  '\n_isWindows = () => false; export { _pollBackgroundStatus, _depInstallSucceeded, _taskBadge };', { context, identifier: filename });
await module.link(specifier => {
  const exports = collaborators[specifier];
  assert.ok(exports, `Unexpected import: ${specifier}`);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await module.evaluate();
const running = module.namespace;
const task = {
  id: 'install-sam', sessionId: 'install-sam', name: 'pip sam_mask',
  type: 'download', status: 'running', output: '',
  payload: { _dep: true, repo_id: 'sam_mask', _cmd: 'python3 -m pip install torch transformers' },
};
const partial = 'Requirement already satisfied: pillow\nDownloading torch.whl (127.3 MB)';
function seed(status = 'running', output = '') {
  storage.set('cookbook-tasks', JSON.stringify([{ ...task, status, output }]));
}
async function poll(status, output, expected, exit_code = null) {
  liveTask = { session_id: task.sessionId, type: 'download', status, output_tail: output, exit_code };
  const before = statusRequests;
  await running._pollBackgroundStatus();
  assert.equal(statusRequests, before + 1, 'Each scenario must execute the real poll');
  assert.equal(JSON.parse(storage.get('cookbook-tasks'))[0].status, expected, output);
  assert.equal(running._loadTasks()[0].status, expected, 'Rendering must agree with persisted status');
}

seed();
await poll('running', partial, 'running');
await poll('running', 'Downloading torch.whl 100%\nBuilding wheel for another-package', 'running');
await poll('running', 'Successfully installed torch\nRunning post-install checks', 'running');
await poll('running', 'DOWNLOAD_OK', 'running');
await poll('completed', 'DOWNLOAD_OK\n=== Process exited with code 0 ===', 'done', 0);

// Recover a previously false-finished card while the authoritative process runs.
seed('done', partial);
await poll('running', partial, 'running');

// All requirements can already exist; completion still comes from the runner.
seed();
await poll('stopped', 'Requirement already satisfied: torch\n=== Process exited with code 0 ===', 'done', 0);

// A successful subprocess or cached dependency must not conceal later failure.
seed();
await poll('error', 'Successfully installed helper\n=== Process exited with code 1 ===', 'error', 1);
seed('done', 'DOWNLOAD_OK\n=== Process exited with code 0 ===');
await poll('error', '=== Process exited with code 1 ===', 'error', 1);
seed('stopped', partial);
assert.equal(running._loadTasks()[0].status, 'stopped');
assert.equal(running._depInstallSucceeded(partial), false);
assert.equal(running._depInstallSucceeded('Successfully installed torch'), false);
assert.equal(running._depInstallSucceeded('=== Process exited with code 0 ===\n=== Process exited with code 1 ==='), false);
assert.equal(running._depInstallSucceeded('=== Process exited with code 1 ===\n=== Process exited with code 0 ==='), true);

// The shell remains alive after pip exits. Its existence must never revive a
// known failure, or show the old wheel-download progress as an active install.
const failedOutput = 'Downloading basicsr-1.4.2.tar.gz (172 kB)\nERROR: build failed\n=== Process exited with code 1 ===';
seed('running', failedOutput);
const failedDisplay = running._loadTasks()[0];
assert.equal(failedDisplay.status, 'error');
assert.equal(running._taskBadge(failedDisplay).text, 'stopped');
await poll('running', failedOutput, 'error');
await running._selfHealStaleTasks({ oneShot: true });
assert.equal(livenessRequests, 0, 'An exited installer needs no shell-liveness probe');
assert.equal(running._loadTasks()[0].status, 'error');

// An exit can arrive while an earlier inconclusive liveness probe is pending.
seed('error', 'Downloading basicsr-1.4.2.tar.gz (172 kB)');
duringLiveness = () => seed('error', failedOutput);
await running._selfHealStaleTasks();
assert.equal(livenessRequests, 1, 'The inconclusive task must actually be probed');
assert.equal(running._loadTasks()[0].status, 'error');
assert.equal(JSON.parse(storage.get('cookbook-tasks'))[0].status, 'error');

// Retained backend exit metadata is conclusive even after the marker scrolls out.
seed('running');
await poll('running', 'old download progress', 'error', 1);
assert.equal(running._taskBadge(running._loadTasks()[0]).text, 'stopped');
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=source, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
