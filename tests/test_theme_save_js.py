"""Run the real theme/storage modules to guard explicit preset overrides."""

import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_explicit_theme_defaults_survive_storage_reload_and_server_sync():
    source = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const jsRoot = path.resolve('static/js');
const storage = new Map();
const requests = [];
const realFiles = new Set(['theme.js', 'storage.js', 'color/hex.js']);
// UI collaborators are unused by save/getSaved. Keep their real module imports
// intact and defer DOMContentLoaded rather than extracting function source.
const uiModules = {
  'ui.js': { default: {} },
  'colorPicker.js': { initColorPickers() {}, attachColorPicker() {} },
  'windowDrag.js': { makeWindowDraggable() {} },
  'tileManager.js': { snapModalToZone() {} },
};

async function loadTheme() {
  const context = vm.createContext({
    console,
    document: { readyState: 'loading', addEventListener() {} },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      return { ok: true };
    },
  });
  const modules = new Map();
  function load(filename) {
    if (modules.has(filename)) return modules.get(filename);
    const relative = path.relative(jsRoot, filename).split(path.sep).join('/');
    let module;
    if (realFiles.has(relative)) {
      module = new vm.SourceTextModule(fs.readFileSync(filename, 'utf8'), {
        context, identifier: filename,
      });
    } else {
      const exports = uiModules[relative];
      assert.ok(exports, `Unexpected import: ${relative}`);
      module = new vm.SyntheticModule(Object.keys(exports), function () {
        for (const [key, value] of Object.entries(exports)) this.setExport(key, value);
      }, { context, identifier: filename });
    }
    modules.set(filename, module);
    return module;
  }
  const theme = load(path.join(jsRoot, 'theme.js'));
  await theme.link((specifier, parent) => load(path.resolve(path.dirname(parent.identifier), specifier)));
  await theme.evaluate();
  return theme.namespace;
}

const theme = await loadTheme();
assert.ok(theme.THEMES.afterglow, 'Afterglow preset must be available');
const colors = JSON.parse(JSON.stringify(theme.THEMES.afterglow));
colors.advanced = { ...colors.advanced, inputBg: '#221133' };
theme.save('afterglow', colors, { font: 'sans', bgPattern: 'afterglow', frosted: true });
requests.length = 0;

// These values override preset defaults. Omitting any one makes a later load
// incorrectly restore the preset's font/effect/glass instead of the user's choice.
theme.save('afterglow', colors, { font: 'mono', bgPattern: 'none', frosted: false });
const expected = {
  name: 'afterglow', colors, font: 'mono', bgPattern: 'none', frosted: false,
};
assert.deepEqual(JSON.parse(storage.get('odysseus-theme')), expected);
assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/api/prefs/theme');
assert.equal(requests[0].options.method, 'PUT');
assert.equal(requests[0].options.credentials, 'same-origin');
assert.equal(requests[0].options.headers['Content-Type'], 'application/json');
assert.deepEqual(JSON.parse(requests[0].options.body), { value: expected });

const reloadedTheme = await loadTheme();
assert.deepEqual(JSON.parse(JSON.stringify(reloadedTheme.getSaved())), expected);
assert.equal(requests.length, 1, 'Reading saved preferences must not write them back');
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=source,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=_REPO,
        timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
