"""Execute dependency checking, issue dialogs, and install actions without installs."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node binary not on PATH")


def test_dependency_check_dialog_and_repairs_keep_the_checked_target():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';

const requests = [], startedTasks = [], toasts = [];
const listeners = new Map();
let failInstall = false;
let doc;
class Element {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {}; this.style = {};
    this.events = {}; this.attributes = {}; this.className = ''; this.textContent = '';
    this.classList = {
      contains: name => this.className.split(/\s+/).includes(name),
      add: name => { this.className += ' ' + name; },
      remove: name => { this.className = this.className.split(/\s+/).filter(v => v !== name).join(' '); },
    };
  }
  set innerHTML(value) { this.html = value; this.children = []; }
  get innerHTML() { return this.html || ''; }
  appendChild(child) { this.children.push(child); child.parent = this; return child; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, fn) { this.events[name] = fn; }
  removeEventListener() {}
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  contains(node) { return node === this || this.children.some(c => c.contains(node)); }
  get isConnected() { return this === doc.body || !!this.parent?.isConnected; }
  focus() { doc.activeElement = this; }
  querySelectorAll(selector) {
    const result = [];
    for (const child of this.children) {
      const matches = selector.startsWith('button')
        ? child.tagName === 'button' && (!selector.includes(':not') || !child.disabled)
        : selector.startsWith('.') && child.classList.contains(selector.slice(1));
      if (matches) result.push(child);
      result.push(...child.querySelectorAll(selector));
    }
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
const ids = new Map();
doc = {
  visibilityState: 'visible', activeElement: null, body: new Element('body'),
  createElement: tag => new Element(tag), addEventListener() {}, removeEventListener() {},
  getElementById: id => ids.get(id) || null,
  querySelectorAll: selector => doc.body.querySelectorAll(selector),
  querySelector: selector => doc.body.querySelector(selector),
};
for (const id of ['cookbook-deps-list', 'cookbook-check-dependencies', 'cookbook-deps-check-status', 'hwfit-deps-server', 'cookbook-manage-packages']) {
  const element = new Element(id === 'cookbook-check-dependencies' ? 'button' : 'div');
  ids.set(id, element); doc.body.appendChild(element);
}
ids.get('hwfit-deps-server').value = 'mac-box';
const broken = {
  name: 'realesrgan', pip: 'realesrgan', category: 'Image', target: 'remote',
  installed: true, needs_repair: true, install_supported: true,
  dependency_issues: [
    { name: 'basicsr', requirement: 'basicsr>=1.4.2', kind: 'missing', message: 'Required package is missing.' },
    { name: 'helper', requirement: 'helper @ https://example.invalid/helper.whl', kind: 'incompatible', message: 'Wrong version.' },
    { name: 'realesrgan runtime', requirement: '', kind: 'incompatible', message: 'Runtime import failed.' },
  ],
};
let packages = [broken,
  { name: 'playwright', pip: 'playwright', category: 'Tools', target: 'remote', installed: false },
  { name: 'unsupported-feature', pip: 'unavailable', category: 'LLM', target: 'remote', installed: false, applicable: false, install_supported: false, install_hint: 'Use a supported server.' },
  { name: 'existing-feature', pip: 'existing', category: 'LLM', target: 'remote', installed: true, install_supported: false },
];
const store = new Map();
let inventory = { python_version: '3.14', executable: 'C:/Odysseus/venv/Scripts/python.exe', packages: [{ name: 'pip', version: '26.2' }, { name: 'setuptools', version: '84.0' }], updates_checked: false };
const context = vm.createContext({
  console, document: doc, URLSearchParams,
  window: {
    addEventListener: (name, fn) => listeners.set(name, fn),
    removeEventListener: (name, fn) => { if (listeners.get(name) === fn) listeners.delete(name); },
  },
  localStorage: { getItem: key => store.get(key) ?? null, setItem: (key, value) => store.set(key, value) },
  setTimeout() { return 1; }, clearTimeout() {}, setInterval() { return 1; },
  fetch: async (url, options) => {
    requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
    if (url.startsWith('/api/cookbook/packages')) return { ok: true, json: async () => ({ packages }) };
    if (url.startsWith('/api/cookbook/environment-packages')) return { ok: true, json: async () => inventory };
    if (url === '/api/cookbook/clear-vram') return { ok: true, json: async () => ({ message: 'Released 15 MB of unused GPU cache.', errors: [] }) };
    if (url === '/api/cookbook/install-system-deps') return { ok: true, json: async () => ({ ok: true }) };
    assert.equal(url, '/api/model/serve');
    return failInstall
      ? { ok: false, status: 400, json: async () => ({ detail: 'test install rejected' }) }
      : { ok: true, json: async () => ({ ok: true, session_id: 'test-install' }) };
  },
});
const jsRoot = path.resolve('static/js');
const real = new Set(['cookbook.js', 'cookbook-deps-recipes.js', 'cookbookDependencyHealth.js', 'cookbookMaintenance.js', 'escMenuStack.js']);
const allowedStubs = new Set(['ui.js', 'spinner.js', 'providers.js', 'windowDrag.js', 'cookbook-diagnosis.js',
  'cookbook-hwfit.js', 'cookbookRunning.js', 'cookbookDownload.js', 'cookbookServe.js', 'toolWindowZOrder.js', 'modalManager.js', 'h3Video.js', 'bfsVideo.js']);
const source = fs.readFileSync(path.join(jsRoot, 'cookbook.js'), 'utf8');
// Declare the collaborators' import surface, while retaining the full real
// coordinator, recipes, dialog, and Escape stack. No production functions are
// extracted or replaced; external render/launch modules are outside this test.
const importNames = new Map();
for (const match of source.matchAll(/import\s+([\s\S]*?)\s+from\s+'\.\/([^']+)'/g)) {
  const names = match[1].trim().startsWith('{')
    ? match[1].replace(/[{}]/g, '').split(',').map(n => n.trim()).filter(Boolean)
    : ['default'];
  importNames.set(match[2], names);
}
const modules = new Map();
function load(file) {
  if (modules.has(file)) return modules.get(file);
  const name = path.relative(jsRoot, file);
  let module;
  if (real.has(name)) {
    const extra = name === 'cookbook.js' ? '\nexport { _fetchDependencies };' : '';
    module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8') + extra, { context, identifier: file });
  } else {
    assert.ok(allowedStubs.has(name), 'Unexpected collaborator: ' + name);
    const values = Object.fromEntries((importNames.get(name) || ['topPortalZ']).map(n => [n, () => {}]));
    if (name === 'ui.js') values.default = { esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;'), showToast: value => toasts.push(value) };
    if (name === 'providers.js') values.providerLogo = () => '';
    if (name === 'cookbook-hwfit.js') {
      values._cachedModelIds = new Set(); values._hwfitCache = { _scannedHost: 'mac-box', system: { backend: 'metal' } };
    }
    if (name === 'cookbookRunning.js') values._addTask = (...args) => startedTasks.push(args);
    if (name === 'toolWindowZOrder.js') values.topPortalZ = () => 2000;
    module = new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context, identifier: file });
  }
  modules.set(file, module); return module;
}
const cookbook = load(path.join(jsRoot, 'cookbook.js'));
await cookbook.link((specifier, parent) => load(path.resolve(path.dirname(parent.identifier), specifier)));
await cookbook.evaluate();
const app = cookbook.namespace;
Object.assign(app._envState, {
  hostPlatform: 'darwin', remoteHost: 'mac-box', env: 'venv', envPath: '/opt/check env', platform: 'darwin',
  servers: [{ host: 'mac-box', name: 'Checked Mac', port: '2224', env: 'venv', envPath: '/opt/check env', platform: 'darwin' }],
});
await app._fetchDependencies();
assert.ok(!new URLSearchParams(requests.at(-1).url.split('?')[1]).has('check_index'), 'Automatic checks must not request index networking');
assert.equal(doc.querySelector('.cookbook-dependency-health'), null, 'Automatic checks stay quiet');
const html = ids.get('cookbook-deps-list').innerHTML;
assert.match(html, /data-dep-issues="realesrgan"/);
assert.match(html, /unsupported-feature/);
assert.match(html, />Unavailable<\/span>/);
assert.match(html, /data-dep-pip="playwright"/);
assert.match(html, />Installed<\/span>/);
assert.match(ids.get('cookbook-deps-check-status').textContent, /3 dependency issues/);
assert.equal(ids.get('cookbook-check-dependencies').disabled, false);

await app._fetchDependencies({ showIssues: true });
assert.equal(new URLSearchParams(requests.at(-1).url.split('?')[1]).get('check_index'), 'true');
const popup = doc.querySelector('.cookbook-dependency-health');
assert.ok(popup);
const actions = popup.querySelectorAll('button');
assert.deepEqual(actions.map(b => b.textContent), ['Install', 'Repair', 'Repair', 'Close']);
// Change the selected server AFTER checking; repairs must still target the Mac.
Object.assign(app._envState, { remoteHost: 'other-server', env: 'none', envPath: '', platform: 'windows' });
ids.get('hwfit-deps-server').value = 'local';
await actions[0].events.click();
let request = requests.at(-1).body;
assert.equal(request.remote_host, 'mac-box');
assert.equal(request.ssh_port, '2224');
assert.equal(request.platform, 'darwin');
assert.equal(request.env_prefix, "source '/opt/check env/bin/activate'");
assert.equal(request.cmd, "'/opt/check env/bin/python3' -m pip install 'basicsr>=1.4.2'");
assert.equal(actions[0].disabled, true);
assert.equal(startedTasks.at(-1)[3].env_prefix, request.env_prefix);
await actions[1].events.click();
assert.equal(requests.at(-1).body.cmd, "'/opt/check env/bin/python3' -m pip install -U 'helper @ https://example.invalid/helper.whl'");
failInstall = true;
await actions[2].events.click();
assert.match(requests.at(-1).body.cmd, /pip install -U 'realesrgan'$/);
assert.equal(actions[2].disabled, false, 'A rejected repair remains retryable');
assert.match(toasts.at(-1), /test install rejected/);
listeners.get('keydown')({ key: 'Escape', preventDefault() {}, stopImmediatePropagation() {} });
assert.equal(doc.querySelector('.cookbook-dependency-health'), null);
assert.equal(modules.get(path.join(jsRoot, 'escMenuStack.js')).namespace._openMenuCount(), 0);

// Explicit Local selection must not inherit the currently selected remote.
packages = [{ ...broken, target: 'local' }]; failInstall = false;
await app._fetchDependencies({ showIssues: true });
const localScan = requests.filter(r => r.url.startsWith('/api/cookbook/packages')).at(-1);
assert.ok(!new URLSearchParams(localScan.url.split('?')[1]).has('host'));
await doc.querySelector('.cookbook-dependency-health').querySelectorAll('button')[0].events.click();
assert.equal(requests.at(-1).body.remote_host, undefined);
assert.equal(requests.at(-1).body.platform, 'darwin');
assert.doesNotMatch(requests.at(-1).body.cmd, /check env/);

// Compatibility-only findings also explain the result in the popup. They
// never offer an install button for a release that cannot run on this target.
packages = [{
  name: 'new-backend', pip: 'new-backend', category: 'LLM', target: 'remote',
  installed: false, install_supported: false, latest_version: '2.0',
  requires_python: '<3.14', has_compatible_artifact: false,
  compatibility_note: 'Latest new-backend 2.0 requires Python <3.14; selected Python is 3.14.7.',
  install_hint: 'Use a supported server or container.',
}];
await app._fetchDependencies({ showIssues: true });
assert.match(ids.get('cookbook-deps-list').innerHTML, /Latest release: 2.0/);
assert.match(ids.get('cookbook-deps-list').innerHTML, /requires Python &lt;3.14/);
const compatibilityPopup = doc.querySelector('.cookbook-dependency-health');
assert.ok(compatibilityPopup, 'A metadata-only compatibility failure must be visible');
assert.deepEqual(compatibilityPopup.querySelectorAll('button').map(b => b.textContent), ['Close']);
const allText = element => element.textContent + element.children.map(allText).join(' ');
assert.match(allText(compatibilityPopup), /requires Python <3.14/);
assert.match(allText(compatibilityPopup), /Latest release: 2.0/);

// A lookup outage is unknown rather than unsupported; preserve the row's
// install choice and explain the inconclusive check without a repair action.
packages = [{ ...packages[0], install_supported: null, latest_version: null,
  has_compatible_artifact: null, compatibility_note: 'Could not verify current PyPI compatibility. Retry Check dependencies.' }];
await app._fetchDependencies({ showIssues: true });
assert.match(ids.get('cookbook-deps-list').innerHTML, /data-dep-pip="new-backend"/);
assert.deepEqual(doc.querySelector('.cookbook-dependency-health').querySelectorAll('button').map(b => b.textContent), ['Close']);

// An import failure can mark a present distribution as not installed. It
// still needs its parent repair even when no subdependency was identified.
packages = [{ name: 'broken-runtime', pip: 'broken-runtime', target: 'local',
  installed: false, needs_repair: true, status_note: 'Runtime import failed.' }];
await app._fetchDependencies({ showIssues: true });
assert.match(ids.get('cookbook-deps-list').innerHTML, /data-dep-issues="broken-runtime"[^>]*>Repair<\/button>/);
assert.deepEqual(doc.querySelector('.cookbook-dependency-health').querySelectorAll('button').map(b => b.textContent), ['Repair', 'Close']);

// A Windows client must not label a remote with an unknown OS as Windows.
// The remote installer detects its own package manager in that case.
packages = [{ name: 'tmux', pip: '', kind: 'system', target: 'remote', installed: false }];
Object.assign(app._envState, { hostPlatform: 'windows', remoteHost: 'linux-box', platform: '',
  servers: [{ host: 'linux-box', name: 'Linux box', platform: '' }] });
ids.get('hwfit-deps-server').value = 'linux-box';
const systemButton = new Element('button');
systemButton.dataset = { depSysdeps: 'tmux', depTarget: 'remote' };
ids.get('cookbook-deps-list').querySelectorAll = selector => selector === '.cookbook-dep-install-sysdeps' ? [systemButton] : [];
await app._fetchDependencies();
await systemButton.events.click({ stopPropagation() {} });
const systemRequest = requests.findLast(r => r.url === '/api/cookbook/install-system-deps').body;
assert.equal(systemRequest.remote_host, 'linux-box');
assert.ok(!systemRequest.platform, 'Unknown remote OS must not inherit the Windows client OS');

// The real package manager uses the install coordinator, including its
// frozen target and the same Running-task completion events as other installs.
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
const findButton = (parent, label) => parent.querySelectorAll('button').find(b => b.attributes['aria-label'] === label || b.textContent === label);
const manager = ids.get('cookbook-manage-packages').onclick();
await settle();
assert.match(requests.at(-1).url, /host=linux-box/);
ids.get('hwfit-deps-server').value = 'local';
Object.assign(app._envState, { remoteHost: '', platform: 'windows' });
await findButton(manager, 'Update pip').events.click();
assert.equal(requests.at(-1).body.remote_host, 'linux-box');
assert.match(requests.at(-1).body.cmd, /pip install -U .*'pip'$/);
assert.ok(startedTasks.at(-1)[3]._dep);
listeners.get('cookbook:dependency-finished')({ detail: { remoteHost: 'linux-box', payload: { repo_id: 'pip' } } });
await settle();
assert.equal(findButton(manager, 'Update pip').disabled, false, 'Completed update releases the action');
await findButton(manager, 'Repair setuptools').events.click();
assert.match(requests.at(-1).body.cmd, /pip install --force-reinstall --no-deps .*'setuptools==84.0'$/);
listeners.get('cookbook:dependency-finished')({ detail: { remoteHost: 'linux-box', payload: { repo_id: 'setuptools' } } });
await settle();
inventory = { ...inventory, updates_checked: true, packages: [{ name: 'pip', version: '26.2', latest_version: '26.3', update_available: true }] };
await findButton(manager, 'Check for updates').events.click();
assert.match(requests.at(-1).url, /check_updates=true/);
assert.match(allText(manager), /26.2 → 26.3/);
assert.ok(findButton(manager, 'Install setuptools'), 'Missing package tools can be restored inside the app');
findButton(manager, 'Close').events.click();
assert.equal(listeners.has('cookbook:dependency-finished'), false);

await app._fetchDependencies();
const localManager = ids.get('cookbook-manage-packages').onclick();
await settle();
assert.ok(!new URLSearchParams(requests.at(-1).url.split('?')[1]).has('host'));
await findButton(localManager, 'Update pip').events.click();
assert.equal(requests.at(-1).body.remote_host, undefined);
assert.equal(requests.at(-1).body.platform, 'windows');
assert.match(requests.at(-1).body.cmd, /^python -m pip install -U 'pip'$/);
findButton(localManager, 'Close').events.click();

const maintenance = modules.get(path.join(jsRoot, 'cookbookMaintenance.js')).namespace;
let stops = 0;
const vram = maintenance.showClearVram({ target: {}, models: [{ name: 'Owned model' }], stopModels: async models => { stops++; return { stopped: models.map(m => m.name), errors: [] }; } });
await findButton(vram, 'Clear unused cache').events.click();
assert.equal(stops, 0, 'Cache-only must not stop a loaded model');
assert.deepEqual(requests.at(-1).body, { unload_models: false });
assert.match(allText(vram), /Released 15 MB/);
await findButton(vram, 'Unload models and clear').events.click();
assert.equal(stops, 1);
assert.deepEqual(requests.at(-1).body, { unload_models: true });
findButton(vram, 'Close').events.click();
const remoteVram = maintenance.showClearVram({ target: { host: 'remote' }, models: [], stopModels: async () => { throw new Error('Must not run'); } });
assert.equal(findButton(remoteVram, 'Clear unused cache').disabled, true);
assert.equal(findButton(remoteVram, 'Unload models and clear').disabled, true);
findButton(remoteVram, 'Close').events.click();
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=script, capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=15,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
