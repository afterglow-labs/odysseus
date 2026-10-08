"""Quick-run must leave SGLang environment selection to the serving backend."""
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_sglang_quick_run_does_not_reject_managed_install_using_global_python():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync('./static/js/cookbook-hwfit.js', 'utf8');
const start = source.indexOf('// ─── Pre-launch install + version check');
assert.ok(start >= 0);
const end = source.indexOf("quickRunBtn.disabled = true;", start);
assert.ok(end > start);
const section = source.slice(start, end);
for (const backend of ['sglang', 'vllm']) {
  const calls = [];
  const errors = [];
  const context = vm.createContext({
    _qrRunBackend: backend,
    _envState: { remoteHost: '' },
    uiModule: { showError: message => errors.push(message) },
    fetch: async (path, options) => {
      calls.push({ path, body: JSON.parse(options.body) });
      return { ok: true, json: async () => ({ stdout: 'MISSING' }) };
    },
  });
  await vm.runInContext('(async () => {\n' + section + '\n})()', context);
  if (backend === 'sglang') {
    assert.deepEqual(calls, []);
    assert.deepEqual(errors, []);
  } else {
    assert.equal(calls.length, 1);
    assert.equal(calls[0].path, '/api/shell/exec');
    assert.match(calls[0].body.command, /command -v vllm/);
    assert.match(errors[0], /vLLM isn't installed/);
  }
}
"""
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, text=True,
        capture_output=True, cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
