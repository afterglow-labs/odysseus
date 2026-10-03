"""Exercise the real picker and Daybreak state without calling a model."""

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_daybreak_picker_preserves_model_session_and_request_state():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const ids = new Map(), store = new Map(), requests = [], errors = [];
class Element {
  constructor() { this.events = {}; this.children = []; this.dataset = {}; this.style = {}; this.className = ''; this.value = ''; }
  get classList() { return {
    contains: n => this.className.split(' ').includes(n),
    add: (...ns) => { this.className += ' ' + ns.join(' '); },
    remove: (...ns) => { this.className = this.className.split(' ').filter(n => !ns.includes(n)).join(' '); },
    toggle: (n, on) => on ? this.classList.add(n) : this.classList.remove(n),
  }; }
  addEventListener(n, fn) { this.events[n] = fn; }
  removeEventListener() {}
  appendChild(e) { this.children.push(e); return e; }
  set innerHTML(v) { this.html = v; this.children = []; }
  get innerHTML() { return this.html || ''; }
  setAttribute(n, v) { this[n] = v; }
  querySelectorAll(selector) { return this.children.filter(c => c.classList.contains(selector.slice(1))); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {} blur() {} contains() { return false; }
}
for (const id of ['model-picker-wrap', 'model-picker-btn', 'model-picker-menu', 'model-picker-search',
  'model-picker-list', 'model-picker-label', 'model-picker-daybreak-row', 'model-picker-daybreak', 'model-picker-daybreak-model', 'model-picker-daybreak-help',
  'model-picker-reasoning-row', 'model-picker-reasoning', 'model-picker-reasoning-help']) ids.set(id, new Element());
const events = {};
const document = { readyState: 'interactive', getElementById: id => ids.get(id) || null, createElement: () => new Element(),
  addEventListener: (n, fn) => { events[n] = fn; }, dispatchEvent() {}, activeElement: null };
const url = 'https://chatgpt.com/backend-api/codex';
const items = [
  { endpoint_id: 'sub', url, category: 'api', endpoint_name: 'ChatGPT Subscription', models: ['gpt-test', 'gpt-other'] },
  { endpoint_id: 'api', url: 'https://api.openai.com/v1', category: 'api', endpoint_name: 'OpenAI', models: ['gpt-test'] },
];
let pending = { url, modelId: 'gpt-test', endpointId: 'sub', source: 'manual' }, sid = null, sessions = [], rejectPatch = false;
let releasePatch = null;
let rejectOnce = false;
const window = { location: { origin: 'http://localhost' }, innerWidth: 900,
  modelsModule: { getCachedItems: () => items } };
const context = vm.createContext({ window, document, URL, FormData, console, Date, Object,
  setTimeout() {}, clearTimeout() {}, CustomEvent: class { constructor(n, o) { this.detail = o.detail; } },
  localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v) },
  fetch: async (url, options) => {
    requests.push({ url, body: Object.fromEntries(options.body) });
    const rejected = rejectPatch || rejectOnce;
    rejectOnce = false;
    if (releasePatch) await new Promise(resolve => { releasePatch = resolve; });
    return { ok: !rejected, json: async () => ({ detail: 'Rejected test option' }) };
  },
});
const root = path.resolve('static/js'), modules = new Map();
function load(file) {
  if (modules.has(file)) return modules.get(file);
  let module;
  const name = path.basename(file);
  if (['modelPicker.js', 'daybreak.js', 'modelSort.js'].includes(name)) {
    module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: file });
  } else {
    const exports = name === 'providers.js' ? { providerLogo: () => null }
      : { default: { showToast() {}, showError: m => errors.push(m) } };
    module = new vm.SyntheticModule(Object.keys(exports), function() {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context, identifier: file });
  }
  modules.set(file, module); return module;
}
const picker = load(path.join(root, 'modelPicker.js'));
await picker.link((s, p) => load(path.resolve(path.dirname(p.identifier), s)));
await picker.evaluate();
const daybreak = modules.get(path.join(root, 'daybreak.js')).namespace;
const deps = { getCurrentSessionId: () => sid, getSessions: () => sessions,
  getPendingChat: () => pending, setPendingChat: value => { pending = value; },
  createDirectChat: async (url, modelId, endpointId, opts) => { pending = { url, modelId, endpointId, ...opts }; },
};
// Inline reasoning must load its catalog without opening the model popover.
// The sessions script runs before the separate models script. Flush its
// microtasks before catalog registration, as the browser does between scripts.
const cachedModels = window.modelsModule;
window.modelsModule = undefined;
ids.get('model-picker-menu').className = 'hidden';
picker.namespace.initModelPicker(deps);
await new Promise(setImmediate);
const catalogLoads = [];
let finishCatalogLoad, catalogReady = false;
const catalogLoad = new Promise(resolve => { finishCatalogLoad = resolve; });
window.modelsModule = {
  getCachedItems: () => catalogReady ? items : [],
  refreshModels: async (force, options) => {
    catalogLoads.push({ force, options });
    await catalogLoad;
    items[0].reasoning_models = { 'gpt-test': { supported: true, levels: ['low', 'high'], default: 'low' } };
    catalogReady = true;
  },
};
assert.equal(typeof events.DOMContentLoaded, 'function', 'Wait for later module scripts to register');
events.DOMContentLoaded();
await Promise.resolve();
assert.equal(catalogLoads.length, 1, 'Startup loads the catalog without a model-picker click');
events.DOMContentLoaded();
await Promise.resolve();
assert.equal(catalogLoads.length, 1, 'Repeated startup signals reuse the same catalog load');
assert.equal(catalogLoads[0].force, false);
assert.equal(catalogLoads[0].options.cacheOnly, true);
assert.equal(ids.get('model-picker-reasoning').disabled, true);
finishCatalogLoad();
await new Promise(setImmediate);
assert.equal(ids.get('model-picker-reasoning').disabled, false);
assert.deepEqual(ids.get('model-picker-reasoning').children.map(x => x.value), ['', 'low', 'high']);
assert.equal(ids.get('model-picker-menu').classList.contains('hidden'), true);
assert.equal(requests.length, 0, 'Startup must not run a separate endpoint health probe');
window.modelsModule = cachedModels;
const checkbox = ids.get('model-picker-daybreak'), row = ids.get('model-picker-daybreak-row');
const refresh = () => picker.namespace.updateModelPicker();
const toggle = async on => { checkbox.checked = on; await checkbox.events.change(); };
refresh();
assert.equal(row.hidden, false);
assert.equal(checkbox.checked, false);
await toggle(true);
assert.equal(pending.modelId, 'gpt-test');
assert.equal(pending.daybreak_enabled, true);
assert.equal(requests.length, 0, 'Pending picks stay local until first send');
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-test', endpointId: 'sub' }), true);
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-other', endpointId: 'sub' }), false);
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-test', endpointId: 'another-sub' }), false);

// Known model capability, not the provider name alone, gates the control.
const alreadyCaptured = daybreak.captureChatRoute(deps);
items[0].daybreak_revision = 1;
items[0].daybreak_models = { 'gpt-test': { supported: false, source: 'catalog', reason: 'Model does not support Daybreak.' } };
refresh();
assert.equal(checkbox.disabled, true);
assert.equal(checkbox.checked, false);
assert.match(ids.get('model-picker-daybreak-help').textContent, /New messages use standard access/);
assert.equal(daybreak.captureChatRoute(deps).daybreak_enabled, false);
assert.equal(alreadyCaptured.daybreak_enabled, true, 'An in-flight request never changes when capability refreshes');
const beforeDisabledToggle = requests.length;
await toggle(true);
assert.equal(checkbox.checked, false);
assert.equal(requests.length, beforeDisabledToggle);
items[0].daybreak_models['gpt-test'] = { supported: null, source: 'unknown' };
refresh();
assert.equal(checkbox.disabled, false, 'Unknown support must remain selectable');
assert.equal(checkbox.checked, true);

// A denial is scoped to the captured account/model and survives inventory
// refreshes; only a successful capability refresh advances the revision.
daybreak.recordDaybreakRejection(alreadyCaptured, { daybreak_supported: false, model: 'gpt-test', text: 'Program rejected for this model.' });
assert.equal(checkbox.disabled, true);
assert.equal(daybreak.captureChatRoute(deps).daybreak_enabled, false);
assert.equal(daybreak.daybreakCapability({ url, modelId: 'gpt-other', endpointId: 'sub' }).supported, null);
assert.equal(daybreak.daybreakCapability({ url, modelId: 'gpt-test', endpointId: 'another-sub' }).supported, null);
items[0] = { ...items[0], daybreak_models: { 'gpt-test': { supported: null, source: 'unknown' } } };
refresh();
assert.equal(checkbox.disabled, true, 'Failed or inventory-only refresh must not clear a denial');
items[0].daybreak_revision = 2;
refresh();
assert.equal(checkbox.disabled, false);
assert.equal(checkbox.checked, true);
daybreak.recordDaybreakRejection(alreadyCaptured, { status: 401, code: 'token_expired', text: 'Reconnect' });
assert.equal(checkbox.disabled, false, 'An authentication problem is not a model capability denial');
items.push({ endpoint_id: 'fallback-account', url, models: ['gpt-test'], daybreak_revision: 1 });
daybreak.recordDaybreakRejection(alreadyCaptured, { daybreak_supported: false, candidate_index: 1, model: 'gpt-test' });
assert.equal(checkbox.disabled, false, 'An unidentified fallback must not disable the captured primary account');
daybreak.recordDaybreakRejection(alreadyCaptured, { daybreak_supported: false, candidate_index: 1,
  model: 'gpt-test', endpoint_id: 'fallback-account', endpoint_url: url });
assert.equal(checkbox.disabled, false);
assert.equal(daybreak.daybreakCapability({ url, modelId: 'gpt-test', endpointId: 'fallback-account' }).supported, false);
items.pop();

// Pick the same actual model through the real row handler; no fake model IDs.
ids.get('model-picker-menu').className = 'hidden';
ids.get('model-picker-btn').events.click({ stopPropagation() {} });
let modelRows = ids.get('model-picker-list').querySelectorAll('.model-switch-item');
assert.equal(modelRows.length, 3);
const findRow = (model, ep) => modelRows.find(r => r.children.some(c => c.textContent === model) && r.children.some(c => c.textContent === ep));
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
assert.equal(pending.daybreak_enabled, true);
await toggle(false);
assert.equal(pending.modelId, 'gpt-test');
assert.equal(pending.daybreak_enabled, false);
await findRow('gpt-test', 'OpenAI').events.click();
assert.equal(row.hidden, true);
assert.equal(pending.daybreak_enabled, false);
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
assert.equal(row.hidden, false);
assert.equal(checkbox.checked, false);
await toggle(true);

// Reopened session is authoritative even when a different local pick was on.
pending = null; sid = 'restored';
sessions = [{ id: sid, model: 'gpt-test', endpoint_url: url, daybreak_enabled: false }];
refresh();
assert.equal(checkbox.checked, false);
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
assert.equal(checkbox.checked, false, 'Picking the same restored model must not reapply a browser default');
await toggle(true);
assert.equal(requests.at(-1).url, 'http://localhost/api/session/restored');
assert.equal(requests.at(-1).body.daybreak_enabled, 'true');
assert.equal(sessions[0].daybreak_enabled, true);
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-test', endpointId: 'sub' }), true,
  'A restored session maps to its unique registered endpoint for local defaults');
const captured = daybreak.captureChatRoute(deps);
items[0].daybreak_models['gpt-test'] = { supported: false, source: 'catalog' };
refresh();
assert.equal(checkbox.checked, false, 'A saved ON option cannot trap the user after support is denied');
assert.equal(checkbox.disabled, true);
const nextForm = new FormData();
daybreak.appendChatRoute(nextForm, daybreak.captureChatRoute(deps));
assert.equal(nextForm.get('daybreak_enabled'), 'false', 'The next explicit send clears the server choice');
assert.equal(captured.daybreak_enabled, true, 'Never downgrade an already-captured request');
items[0].daybreak_models['gpt-test'] = { supported: null, source: 'unknown' };
refresh();
await toggle(false);
assert.equal(captured.daybreak_enabled, true, 'The active request holds a value snapshot');
assert.equal(daybreak.captureChatRoute(deps).daybreak_enabled, false);
const form = new FormData();
daybreak.appendChatRoute(form, captured);
assert.deepEqual(Object.fromEntries(form), { selected_model: 'gpt-test', selected_endpoint_url: url,
  selected_endpoint_id: 'sub', daybreak_enabled: 'true', reasoning_effort: '' });

// A provider switch clears the setting and sends it with the actual model.
await findRow('gpt-test', 'OpenAI').events.click();
assert.equal(requests.at(-1).body.daybreak_enabled, 'false');
assert.equal(requests.at(-1).body.model, 'gpt-test');
assert.equal(row.hidden, true);
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
rejectPatch = true;
await toggle(true);
assert.equal(sessions[0].daybreak_enabled, false, 'A failed PATCH rolls back the checkbox');
assert.equal(checkbox.checked, false);
assert.ok(errors.length);
rejectPatch = false;

// A different session beats the prior pick when capturing request options.
sessions = [{ id: 'other', model: 'gpt-other', endpoint_url: url, endpoint_id: 'sub', daybreak_enabled: true }];
sid = 'other'; refresh();
assert.equal(checkbox.checked, true);
assert.equal(daybreak.captureChatRoute(deps).model, 'gpt-other');
assert.equal(daybreak.captureChatRoute(deps).daybreak_enabled, true);
assert.equal(daybreak.isDaybreakSupported('https://chatgpt.com.evil.invalid/backend-api/codex'), false);
assert.equal(daybreak.isDaybreakSupported('https://chatgpt.com/other'), false);

// Writes serialize per session: the model change cannot commit before an
// earlier toggle and then be overwritten by that stale toggle finishing.
let unblock;
releasePatch = true;
const oldToggle = toggle(false);
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch;
releasePatch = null;
const beforeSwitch = requests.length;
const switchProvider = findRow('gpt-test', 'OpenAI').events.click();
await Promise.resolve(); await Promise.resolve();
assert.equal(requests.length, beforeSwitch);
unblock();
await oldToggle; await switchProvider;
assert.equal(requests.at(-1).body.endpoint_url, 'https://api.openai.com/v1');
assert.equal(requests.at(-1).body.daybreak_enabled, 'false');

// Restored sessions lack endpoint_id in the API. Resolve only a unique
// catalog match for preferences, never guessing between subscription accounts.
sessions = [{ id: 'restored-no-id', model: 'gpt-other', endpoint_url: url, daybreak_enabled: false }];
sid = 'restored-no-id'; refresh();
await toggle(true);
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-other', endpointId: 'sub' }), true);
assert.equal(daybreak.captureChatRoute(deps).endpoint_id, '', 'Preference matching must not redirect a restored session to another account');
items.push({ endpoint_id: 'second-account', url, category: 'api', models: ['gpt-other'] });
await toggle(false);
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-other', endpointId: 'sub' }), true,
  'An ambiguous restored session must not overwrite either account preference');
assert.equal(daybreak.daybreakForSelection({ url, modelId: 'gpt-other', endpointId: 'second-account' }), false);

// Two accounts can expose the same model and URL. A failed older pick must
// not roll the cache back after the newer account was already selected.
items.at(-1).models = ['gpt-other', 'gpt-test'];
items.at(-1).endpoint_name = 'Second account';
ids.get('model-picker-menu').className = 'hidden';
ids.get('model-picker-btn').events.click({ stopPropagation() {} });
modelRows = ids.get('model-picker-list').querySelectorAll('.model-switch-item');
sessions = [{ id: 'account-race', model: 'old-model', endpoint_url: url, endpoint_id: 'sub', daybreak_enabled: false }];
sid = 'account-race'; refresh();
releasePatch = true; rejectOnce = true;
const failedFirstPick = findRow('gpt-test', 'ChatGPT Subscription').events.click();
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch; releasePatch = null;
const newAccountPick = findRow('gpt-test', 'Second account').events.click();
assert.equal(sessions[0].endpoint_id, 'second-account');
unblock();
await failedFirstPick; await newAccountPick;
assert.equal(sessions[0].endpoint_id, 'second-account');
assert.equal(sessions[0].model, 'gpt-test');
assert.equal(requests.at(-1).body.endpoint_id, 'second-account');

// The same protection applies to a failed Daybreak toggle followed by an
// account pick with the same URL, model, and checkbox value.
sessions[0].endpoint_id = 'sub'; sessions[0].daybreak_enabled = true; refresh();
releasePatch = true; rejectOnce = true;
const failedToggle = toggle(false);
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch; releasePatch = null;
const accountAfterToggle = findRow('gpt-test', 'Second account').events.click();
unblock();
await failedToggle; await accountAfterToggle;
assert.equal(sessions[0].endpoint_id, 'second-account');
assert.equal(sessions[0].daybreak_enabled, false);

// A late rejection belongs to the captured account, not whichever account
// is now selected in the picker.
sessions[0].endpoint_id = 'sub'; sessions[0].daybreak_enabled = true;
const oldAccountRequest = daybreak.captureChatRoute(deps);
sessions[0].endpoint_id = 'second-account'; refresh();
daybreak.recordDaybreakRejection(oldAccountRequest, { daybreak_supported: false, model: 'gpt-test', text: 'Unsupported program' });
assert.equal(checkbox.disabled, false);
assert.equal(checkbox.checked, true);
sessions[0].endpoint_id = 'sub'; refresh();
assert.equal(checkbox.disabled, true);
assert.equal(checkbox.checked, false);

// Reasoning uses only this account/model's advertised options, including
// future effort values, and keeps Provider default distinct from a level.
items[0].reasoning_models = {
  'gpt-test': { supported: true, levels: ['low', 'high', 'ultra'], default: 'high' },
  'gpt-other': { supported: true, levels: ['low'], default: 'low' },
};
items[0].daybreak_revision = 3;
const effort = ids.get('model-picker-reasoning');
const changeEffort = async value => { effort.value = value; await effort.events.change(); };
sid = null; sessions = []; pending = { url, modelId: 'gpt-test', endpointId: 'sub', source: 'manual' };
refresh();
assert.equal(effort.disabled, false);
assert.deepEqual(effort.children.map(option => option.value), ['', 'low', 'high', 'ultra']);
assert.equal(effort.children[0].textContent, 'Provider default (High)');
assert.equal(effort.value, '');
await changeEffort('high');
assert.equal(pending.reasoning_effort, 'high');
const pendingEffortSnapshot = daybreak.captureChatRoute(deps);
await findRow('gpt-other', 'ChatGPT Subscription').events.click();
assert.equal(pending.reasoning_effort, '');
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
assert.equal(pending.reasoning_effort, 'high', 'New-chat preference is per endpoint and model');
assert.equal(pendingEffortSnapshot.reasoning_effort, 'high');
await findRow('gpt-test', 'Second account').events.click();
assert.equal(effort.disabled, true, 'Missing capability data uses safe default');
assert.equal(pending.reasoning_effort, '');
assert.match(ids.get('model-picker-reasoning-help').textContent, /not advertised|Refresh/i);
items.at(-1).reasoning_models = { 'gpt-test': { supported: false, levels: [], default: null } };
refresh();
assert.equal(effort.disabled, true);
assert.match(ids.get('model-picker-reasoning-help').textContent, /does not offer/);
await changeEffort('ultra');
assert.equal(pending.reasoning_effort, '', 'An unadvertised value cannot be submitted');
await findRow('gpt-test', 'OpenAI').events.click();
assert.equal(ids.get('model-picker-reasoning-row').hidden, true);

pending = null; sid = 'reasoning-session';
sessions = [{ id: sid, model: 'gpt-test', endpoint_url: url, endpoint_id: 'sub',
  daybreak_enabled: false, reasoning_effort: 'low' }];
refresh();
assert.equal(effort.value, 'low', 'Restored choice beats browser default high');
const knownReasoning = items[0].reasoning_models;
sessions[0].reasoning_effort = 'high';
delete items[0].reasoning_models;
const requestCountBeforeDiscovery = requests.length;
refresh();
assert.equal(effort.value, 'high', 'Loading the catalog must not erase a saved effort');
assert.equal(effort.children.at(-1).textContent, 'High (saved)');
assert.equal(effort.children.at(-1).disabled, true, 'Unverified effort is displayed, not offered as a new choice');
assert.equal(effort.disabled, false, 'The user can explicitly reset a saved unverified choice');
const unknownForm = new FormData(); daybreak.appendChatRoute(unknownForm, daybreak.captureChatRoute(deps));
assert.equal(unknownForm.get('reasoning_effort'), 'high');
assert.equal(requests.length, requestCountBeforeDiscovery, 'Metadata loading must not issue a clearing PATCH');
await changeEffort('');
assert.equal(requests.at(-1).body.reasoning_effort, '');
assert.equal(sessions[0].reasoning_effort, '');
sessions[0].reasoning_effort = 'high';
await findRow('gpt-other', 'ChatGPT Subscription').events.click();
assert.equal(sessions[0].reasoning_effort, '', 'An unknown new route must not inherit another model’s saved effort');
items[0].reasoning_models = knownReasoning;
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
await changeEffort('ultra');
assert.equal(requests.at(-1).body.reasoning_effort, 'ultra');
const effortSnapshot = daybreak.captureChatRoute(deps);
await changeEffort('high');
const effortForm = new FormData(); daybreak.appendChatRoute(effortForm, effortSnapshot);
assert.equal(effortForm.get('reasoning_effort'), 'ultra', 'In-flight requests keep their captured level');
rejectPatch = true;
await changeEffort('low');
assert.equal(effort.value, 'high', 'A rejected effort save rolls back');
rejectPatch = false;
await findRow('gpt-other', 'ChatGPT Subscription').events.click();
assert.equal(sessions[0].reasoning_effort, '');
assert.equal(requests.at(-1).body.reasoning_effort, '');
await changeEffort('low');
await findRow('gpt-test', 'ChatGPT Subscription').events.click();
assert.equal(sessions[0].reasoning_effort, 'low', 'A conversation carries a level only when valid for the next model');

// A rejected effort write must roll itself back even if a different option
// was changed meanwhile; it must never roll back a newer model/account pick.
releasePatch = true; rejectOnce = true;
const failedEffort = changeEffort('high');
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch; releasePatch = null;
const laterDaybreak = toggle(true);
unblock(); await failedEffort; await laterDaybreak;
assert.equal(sessions[0].reasoning_effort, 'low');
assert.equal(sessions[0].daybreak_enabled, true);
releasePatch = true; rejectOnce = true;
const effortBeforeAccount = changeEffort('ultra');
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch; releasePatch = null;
const latestAccount = findRow('gpt-test', 'Second account').events.click();
unblock(); await effortBeforeAccount; await latestAccount;
assert.equal(sessions[0].endpoint_id, 'second-account');
assert.equal(sessions[0].reasoning_effort, '');

// While a route write is pending, option edits cannot be queued against a
// model that the server may reject. A failed route restores the old state.
sessions[0].endpoint_id = 'sub'; sessions[0].model = 'gpt-test'; sessions[0].reasoning_effort = 'high'; refresh();
releasePatch = true; rejectOnce = true;
const rejectedModelPick = findRow('gpt-other', 'ChatGPT Subscription').events.click();
await Promise.resolve(); await Promise.resolve();
unblock = releasePatch; releasePatch = null;
assert.equal(effort.disabled, true);
assert.equal(checkbox.disabled, true);
const pendingRouteRequestCount = requests.length;
await changeEffort('low'); await toggle(false);
assert.equal(requests.length, pendingRouteRequestCount);
unblock(); await rejectedModelPick;
assert.equal(sessions[0].model, 'gpt-test');
assert.equal(sessions[0].reasoning_effort, 'high');
assert.equal(effort.disabled, false);
"""
    result = subprocess.run(['node', '--experimental-vm-modules', '--input-type=module'],
                            input=script, text=True, capture_output=True, cwd=ROOT, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')
def test_pending_daybreak_is_sent_and_preserved_when_session_materializes():
    script = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const requests = [], store = new Map(), preferences = new Map(), modules = new Map();
let response;
let historyResponse = null;
const modelItems = [{ endpoint_id: 'sub', url: 'https://chatgpt.com/backend-api/codex',
  models: ['gpt-test', 'fresh-model'], reasoning_models: {
    'gpt-test': { supported: true, levels: ['low', 'high'], default: 'low' },
    'fresh-model': { supported: true, levels: ['low', 'high'], default: 'low' },
  } }];
let catalogReady = true;
const historyElement = { style: {}, classList: { remove() {}, add() {} }, querySelector: () => null,
  querySelectorAll: selector => selector === '.msg' ? [{}] : [] };
const storage = { get: k => store.get(k), set: (k, v) => store.set(k, v), remove: k => store.delete(k) };
const document = { readyState: 'loading', getElementById: id => historyResponse && id === 'chat-history' ? historyElement : null, querySelectorAll: () => [],
  querySelector: () => null, addEventListener() {} };
const context = vm.createContext({ console, document, URL, URLSearchParams, FormData, Date,
  window: { location: { origin: 'http://localhost', pathname: '/' }, addEventListener() {},
    modelsModule: { getCachedItems: () => catalogReady ? modelItems : [] } },
  navigator: { platform: 'Mac' }, history: { replaceState() {} }, setTimeout() {},
  sessionStorage: { getItem: () => null, removeItem() {}, setItem() {} },
  localStorage: { getItem: k => preferences.get(k) ?? null, setItem: (k, v) => preferences.set(k, v) },
  fetch: async (url, options) => {
    if (url.endsWith('/api/sessions')) return new Promise(() => {}); // background sidebar outside this test
    if (url.includes('/api/history/')) return { ok: true, json: async () => historyResponse };
    requests.push({ url, body: Object.fromEntries(options.body) });
    return new Promise(resolve => { response = payload => resolve({ ok: true, json: async () => payload }); });
  },
});
const root = path.resolve('static/js');
function load(file) {
  file = file.split('?')[0];
  if (modules.has(file)) return modules.get(file);
  const name = path.basename(file);
  let module;
  if (['sessions.js', 'daybreak.js'].includes(name)) {
    module = new vm.SourceTextModule(fs.readFileSync(file, 'utf8'), { context, identifier: file });
  } else {
    const exports = name === 'storage.js' ? { default: storage }
      : name === 'ui.js' ? { default: { el: id => document.getElementById(id), showError: m => { throw Error(m); } }, autoResize() {}, styledPrompt() {} }
      : name === 'modelPicker.js' ? { initModelPicker() {}, updateModelPicker() {} }
      : name === 'providers.js' ? { providerLogo: () => null } : { default: {} };
    module = new vm.SyntheticModule(Object.keys(exports), function() {
      for (const [k, v] of Object.entries(exports)) this.setExport(k, v);
    }, { context, identifier: file });
  }
  modules.set(file, module); return module;
}
const module = load(path.join(root, 'sessions.js'));
await module.link((s, p) => load(path.resolve(path.dirname(p.identifier), s)));
await module.evaluate();
const sessions = module.namespace;
const url = 'https://chatgpt.com/backend-api/codex';
const options = modules.get(path.join(root, 'daybreak.js')).namespace;
preferences.set('odysseus-model-reasoning-effort', JSON.stringify({
  [JSON.stringify(['sub', 'gpt-test'])]: 'high',
}));
// Reload creates the default blank chat before discovery is ready. An omitted
// choice must still resolve its stored per-model preference after discovery.
for (const initiallyReady of [false, true]) {
  catalogReady = initiallyReady;
  sessions.createDirectChat(url, 'gpt-test', 'sub', { source: 'default' });
  if (!initiallyReady) assert.equal(options.captureChatRoute(sessions).reasoning_effort, '');
  catalogReady = true;
  assert.equal(options.captureChatRoute(sessions).reasoning_effort, 'high',
    'Loading model metadata must restore the blank-chat preference');
  const creation = sessions.materializePendingSession();
  assert.equal(requests.at(-1).body.reasoning_effort, 'high');
  response({ id: 'reload-' + initiallyReady, reasoning_effort: 'high' });
  assert.equal(await creation, true);
}
catalogReady = false;
sessions.createDirectChat(url, 'gpt-test', 'sub', { source: 'default', reasoning_effort: '' });
catalogReady = true;
assert.equal(options.captureChatRoute(sessions).reasoning_effort, '',
  'An explicit Provider default must override the stored High preference');
for (const enabled of [true, false]) {
  const effort = enabled ? 'high' : '';
  sessions.createDirectChat(url, 'gpt-test', 'sub', { daybreak_enabled: enabled, reasoning_effort: effort });
  assert.equal(sessions.getPendingChat().daybreak_enabled, enabled);
  assert.equal(sessions.getPendingChat().reasoning_effort, effort);
  const creation = sessions.materializePendingSession();
  assert.equal(requests.at(-1).body.daybreak_enabled, String(enabled));
  assert.equal(requests.at(-1).body.reasoning_effort, effort);
  assert.equal(requests.at(-1).body.model, 'gpt-test');
  response({ id: 'session-' + enabled, daybreak_enabled: enabled, reasoning_effort: effort || null });
  assert.equal(await creation, true);
  assert.equal(sessions.getPendingChat(), null);
  assert.equal(sessions.getSessions().find(s => s.id === 'session-' + enabled).daybreak_enabled, enabled);
  assert.equal(sessions.getSessions().find(s => s.id === 'session-' + enabled).reasoning_effort, effort || null);
}
const restored = sessions.getSessions().find(s => s.id === 'session-false');
restored.endpoint_url = 'https://api.openai.com/v1';
restored.endpoint_id = 'stale-account';
historyResponse = { history: [], model: 'fresh-model', endpoint_url: url, daybreak_enabled: true, reasoning_effort: 'low' };
await sessions.selectSession('session-false', { showLoading: false });
assert.equal(restored.model, 'fresh-model');
assert.equal(restored.endpoint_url, url, 'History restores its authoritative provider URL');
assert.equal(restored.daybreak_enabled, true);
assert.equal(restored.reasoning_effort, 'low');
assert.equal(restored.endpoint_id, undefined, 'A cached account ID must not follow a provider change');
const route = modules.get(path.join(root, 'daybreak.js')).namespace.captureChatRoute(sessions);
assert.equal(route.endpoint_url, url);
assert.equal(route.daybreak_enabled, true);
assert.equal(route.reasoning_effort, 'low');
assert.equal(route.endpoint_id, '');
sessions.createDirectChat('https://api.openai.com/v1', 'gpt-test', 'api', { daybreak_enabled: true, reasoning_effort: 'high' });
assert.equal(sessions.getPendingChat().daybreak_enabled, false);
assert.equal(sessions.getPendingChat().reasoning_effort, '');
"""
    result = subprocess.run(['node', '--experimental-vm-modules', '--input-type=module'],
                            input=script, text=True, capture_output=True, cwd=ROOT, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
