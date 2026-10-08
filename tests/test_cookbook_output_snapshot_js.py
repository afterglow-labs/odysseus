"""Cookbook status polls return overlapping snapshots, never append-only deltas."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")

HARNESS = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const storage = new Map();
let liveTask;
let statusRequests = 0;
const context = vm.createContext({
  console, window: {},
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
// Exercise the complete production module with browser collaborators stubbed.
const module = new vm.SourceTextModule(fs.readFileSync('static/js/cookbookRunning.js', 'utf8') +
  '\nexport { _mergeOutputTail, _redactStoredText, _pollBackgroundStatus };', { context });
await module.link(specifier => {
  const exports = collaborators[specifier];
  assert.ok(exports, `Unexpected import: ${specifier}`);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
  }, { context });
});
await module.evaluate();
const { _mergeOutputTail: merge, _redactStoredText: redact } = module.namespace;
"""


def run_node(source):
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=HARNESS + source, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


@pytest.mark.parametrize(
    "previous,incoming,expected",
    [
        ("", "loading model\nwarning", "loading model\nwarning"),
        ("Launched via agent — waiting for tmux output…", "loading model", "loading model"),
        ("loading model\nwarning", "loading model\nwarning", "loading model\nwarning"),
        ("boot\nloading model\nwarning", "loading model\nwarning", "boot\nloading model\nwarning"),
        ("loading model\nwarning", "loading model\nwarning\nready", "loading model\nwarning\nready"),
        ("boot\nloading model\nwarning", "loading model\nwarning\nready", "boot\nloading model\nwarning\nready"),
        ("boot\nload", "loading model\nready", "boot\nloading model\nready"),
        ("boot\nloading\nprogress 10%", "loading\nprogress 20%", "boot\nloading\nprogress 20%"),
        ("boot\nloading\nprogress 10%", "loading\nprogress 20%\nready", "boot\nloading\nprogress 20%\nready"),
        ("loading model\r\nwarning\r\n", "loading model\nwarning", "loading model\nwarning"),
        ("loading model\nwarning", "", "loading model\nwarning"),
        ("boot\nolder output", "new output\nready", "new output\nready"),
        ("unrelated t", "tail snapshot", "tail snapshot"),
        ("boot\nwarning\nwarning", "warning\nwarning\nwarning", "boot\nwarning\nwarning\nwarning"),
        ("[odysseus] HF token: [redacted]\nloading model", "[odysseus] HF token: applied\nloading model", "[odysseus] HF token: [redacted]\nloading model"),
    ],
    ids=[
        "first-snapshot", "placeholder", "identical", "shorter-tail", "growing", "rolling",
        "partial-last-line", "rewritten-last-line", "rewritten-and-new-lines", "line-endings",
        "empty-preserves-history", "gap-uses-authoritative-tail", "no-character-join",
        "genuine-repeated-lines", "raw-versus-redacted",
    ],
)
def test_snapshot_merge(previous, incoming, expected):
    run_node(f"assert.equal(merge({json.dumps(previous)}, {json.dumps(incoming)}), {json.dumps(expected)});")


def test_identical_polls_and_secret_redaction_remain_idempotent():
    run_node(r"""
const secret = 'hf_testsyntheticsecret1234567890';
const raw = `[odysseus] HF token: applied\nAuthorization=${secret}\npassword=synthetic-password\nloading model Gemma\nwarning CPU_REPACK`;
for (const initial of ['', raw, redact(raw)]) {
  let output = initial;
  for (let i = 0; i < 20; i++) output = merge(redact(output), raw);
  assert.equal(output, redact(raw));
  assert.equal(output.split('loading model Gemma').length - 1, 1);
  assert.ok(!output.includes(secret));
  assert.ok(!output.includes('synthetic-password'));
}
""")


def test_history_and_new_snapshots_are_bounded_after_redaction():
    run_node(r"""
const long = Array.from({length: 700}, (_, i) => `line ${i}`).join('\n');
assert.equal(merge('', long), long.slice(-5000));
assert.equal(merge(long, 'line 699\nready'), (long + '\nready').slice(-5000));
assert.equal(merge(long, ''), long.slice(-5000));
assert.equal(merge(long, 'line 699'), long.slice(-5000));
const next = merge(long, 'line 699\nready');
assert.equal(merge(next, 'line 699\nready'), next);
""")


def test_real_background_polls_do_not_accumulate_full_log_blocks():
    run_node(r"""
const task = {
  id: 'serve-gemma', sessionId: 'serve-gemma', name: 'Gemma', type: 'serve',
  status: 'running', output: 'Launched via agent — waiting for tmux output',
  payload: { repo_id: 'example/Gemma', _cmd: 'llama-server --model Gemma.gguf' },
};
storage.set('cookbook-tasks', JSON.stringify([task]));
const first = '[odysseus] HF token: applied\nloading model Gemma\nwarning CPU_REPACK';
async function poll(snapshot) {
  liveTask = { session_id: task.sessionId, type: 'serve', status: 'running', output_tail: snapshot };
  const before = statusRequests;
  await module.namespace._pollBackgroundStatus();
  assert.equal(statusRequests, before + 1, 'Real background polling must execute');
  return JSON.parse(storage.get('cookbook-tasks'))[0].output;
}
for (let i = 0; i < 7; i++) assert.equal(await poll(first), redact(first));
assert.equal(await poll(first + '\nloading weights 10%'), redact(first + '\nloading weights 10%'));
assert.equal(await poll('loading model Gemma\nwarning CPU_REPACK\nloading weights 20%'), redact(first + '\nloading weights 20%'));
assert.equal(await poll('warning CPU_REPACK\nloading weights 20%\nweights loaded'), redact(first + '\nloading weights 20%\nweights loaded'));
assert.equal(JSON.parse(storage.get('cookbook-tasks'))[0].status, 'running');
""")
