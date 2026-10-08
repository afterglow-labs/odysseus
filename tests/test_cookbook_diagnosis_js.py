from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parent.parent
DIAGNOSIS_JS = ROOT / "static" / "js" / "cookbook-diagnosis.js"


def test_repair_kernels_pip_spec_is_shell_quoted():
    source = DIAGNOSIS_JS.read_text(encoding="utf-8")

    assert '"kernels<0.15"' in source
    assert " --break-system-packages kernels<0.15" not in source


def test_sglang_native_dependency_diagnosis_is_exposed_to_browser():
    source = DIAGNOSIS_JS.read_text(encoding="utf-8")

    assert r"Python\.h" in source
    assert r"libnuma\.so\.1" in source
    assert "SGLang native kernel/runtime" in source
    assert "libnuma-dev python3.12-dev build-essential" in source
    assert "sglang-kernel" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")
def test_sglang_install_failure_explains_environment_without_killing_model_servers():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync('./static/js/cookbook-diagnosis.js', 'utf8');
const section = source.slice(source.indexOf('export const ERROR_PATTERNS'), source.indexOf('function _diagnosisCopyBundle'));
const context = vm.createContext({});
vm.runInContext(section.replace(/^export /gm, '') + '\nglobalThis.diagnose = _diagnose;', context);
const failed = 'Collecting sglang[all]\nCollecting flashinfer_python==0.4.1\nERROR: No matching distribution found for apache-tvm-ffi==0.1.0b15\nERROR: Failed to build flashinfer_python';
for (const output of [failed, 'SGLang requires Python 3.10–3.13; selected Python 3.14']) {
  const diagnosis = context.diagnose(output);
  assert.match(diagnosis.message, /SGLang could not install/);
  assert.match(diagnosis.suggestion, /Python 3\.12/);
  assert.match(diagnosis.suggestion, /separate local/);
  assert.equal(diagnosis.fixes.length, 1);
  assert.equal(diagnosis.fixes[0].label, 'Open Dependencies');
}
assert.equal(context.diagnose(failed + '\nUvicorn running on http://127.0.0.1:8000'), null);
const unrelated = context.diagnose('ERROR: Failed to build basicsr');
assert.doesNotMatch(unrelated.message, /SGLang/);
"""
    result = subprocess.run(
        ["node", "--input-type=module"], input=script, text=True,
        capture_output=True, cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
