// Model preferences live in the user's profile on the connected Odysseus server.
const API = '/api/prefs/model-generation';
const NUMBERS = [
  ['temperature', 'Temperature', 0, 2, 0.05],
  ['max_tokens', 'Max output tokens', 0, 1000000, 1, '0 = server limit'],
  ['top_p', 'Top-p', 0, 1, 0.01],
  ['top_k', 'Top-k', 0, 100000, 1],
  ['min_p', 'Min-p', 0, 1, 0.01],
  ['seed', 'Seed', -1, 4294967295, 1, '-1 = random'],
  ['reasoning_budget', 'Thinking token budget', -1, 1000000, 1, '0 = off, -1 = unlimited'],
  ['repeat_penalty', 'Repetition penalty', 0, 5, 0.05],
  ['presence_penalty', 'Presence penalty', -2, 2, 0.05],
  ['frequency_penalty', 'Frequency penalty', -2, 2, 0.05],
];
const ADVANCED = [
  ['typical_p', 'Typical-p', 0, 1, 0.01],
  ['repeat_last_n', 'Repetition window', -1, 1000000, 1],
  ['mirostat', 'Mirostat (0, 1, 2)', 0, 2, 1],
  ['mirostat_tau', 'Mirostat tau', 0, 100, 0.1],
  ['mirostat_eta', 'Mirostat eta', 0, 1, 0.01],
  ['dynatemp_range', 'Dynamic temperature range', 0, 2, 0.05],
  ['dynatemp_exponent', 'Dynamic temperature exponent', 0.01, 100, 0.01],
  ['xtc_probability', 'XTC probability', 0, 1, 0.01],
  ['xtc_threshold', 'XTC threshold', 0, 1, 0.01],
  ['dry_multiplier', 'DRY multiplier', 0, 100, 0.1],
  ['dry_base', 'DRY base', 0, 100, 0.1],
  ['dry_allowed_length', 'DRY allowed length', 0, 1000000, 1],
  ['dry_penalty_last_n', 'DRY window', -1, 1000000, 1],
];
let getSelection;
let panel;
let button;
let editingKey = '';
let loading = 0;
let saving = false;

export function modelGenerationKey(selection = {}) {
  return JSON.stringify([String(selection.endpoint_url || '').replace(/\/+$/, ''), selection.model || '']);
}

function close() {
  if (!panel || saving) return;
  panel.hidden = true;
  button.setAttribute('aria-expanded', 'false');
  loading++;
}

function fill(options = {}) {
  panel.querySelectorAll('[data-generation]').forEach(input => {
    const value = options[input.dataset.generation];
    input.value = Array.isArray(value) ? value.join('\n') : value ?? '';
  });
}

async function open() {
  const selection = getSelection();
  if (!selection.model) return;
  editingKey = modelGenerationKey(selection);
  const ticket = ++loading;
  panel.hidden = false;
  button.setAttribute('aria-expanded', 'true');
  panel.querySelector('.model-controls-model').textContent = selection.model;
  const status = panel.querySelector('[role="status"]');
  const save = panel.querySelector('[data-action="save"]');
  status.textContent = 'Loading saved controls…';
  save.disabled = true;
  fill();
  try {
    const response = await fetch(API, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok) throw new Error('Could not load saved controls. Close and reopen to retry.');
    const data = await response.json();
    if (ticket !== loading) return;
    fill(data.value?.[editingKey] || {});
    status.textContent = '';
    save.disabled = false;
    panel.querySelector('[data-generation="thinking"]').focus();
  } catch (error) {
    if (ticket === loading) status.textContent = error.message;
  }
}

async function save() {
  if (saving || panel.querySelector('[data-action="save"]').disabled) return;
  const options = {};
  for (const input of panel.querySelectorAll('[data-generation]')) {
    if (!input.reportValidity()) return;
    if (input.value === '') continue;
    const name = input.dataset.generation;
    options[name] = input.type === 'number' ? Number(input.value)
      : ['stop', 'samplers'].includes(name) ? input.value.split('\n').map(s => name === 'samplers' ? s.trim() : s).filter(Boolean)
      : input.value;
  }
  const status = panel.querySelector('[role="status"]');
  saving = true;
  panel.querySelectorAll('input,select,textarea,button').forEach(el => { el.disabled = true; });
  status.textContent = 'Saving…';
  try {
    const response = await fetch(API, { method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_key: editingKey, options }) });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Could not save controls');
    fill(result.value?.[editingKey] || {});
    status.textContent = 'Saved. Applies to the next message in this model.';
  } catch (error) {
    status.textContent = `Save failed: ${error.message}`;
  } finally {
    saving = false;
    panel.querySelectorAll('input,select,textarea,button').forEach(el => { el.disabled = false; });
    updateModelGenerationControls(getSelection());
  }
}

export function updateModelGenerationControls(selection) {
  if (!button) return;
  button.disabled = !selection.model;
  try {
    if (new URL(selection.endpoint_url).hostname === 'chatgpt.com') {
      button.disabled = true;
      button.title = 'This provider uses the Reasoning effort selector next to the model.';
    } else button.title = 'Model controls';
  } catch {}
  if (panel && !panel.hidden && modelGenerationKey(selection) !== editingKey) close();
}

export function initModelGenerationControls(selectionGetter) {
  getSelection = selectionGetter;
  button = document.getElementById('model-controls-btn');
  panel = document.getElementById('model-controls-panel');
  if (!button || !panel || button.dataset.bound) return;
  button.dataset.bound = '1';
  const numeric = ([name, label, min, max, step, hint]) => `<label>${label}<input type="number" data-generation="${name}" min="${min}" max="${max}" step="${step}" placeholder="${hint || 'Default'}" aria-label="${label}" /></label>`;
  panel.innerHTML = `<div class="model-controls-heading"><strong>Model controls</strong><button type="button" data-action="close" aria-label="Close model controls">×</button></div>
    <div class="model-controls-model"></div>
    <p>Blank fields keep existing defaults. Saved for this model in your profile.</p>
    <div class="model-controls-grid">
      <label>Thinking<select data-generation="thinking" aria-label="Thinking"><option value="">Model default</option><option value="off">Off</option><option value="on">On</option></select></label>
      <label>Reasoning effort<select data-generation="reasoning_effort" aria-label="Reasoning effort"><option value="">Model default</option>${['none','minimal','low','medium','high','xhigh','max'].map(x => `<option value="${x}">${x === 'none' ? 'None (thinking off)' : x === 'xhigh' ? 'Extra high' : x[0].toUpperCase() + x.slice(1)}</option>`).join('')}</select></label>
      ${NUMBERS.map(numeric).join('')}
    </div>
    <details><summary>Advanced sampling</summary><p>These controls depend on your serving engine.</p><div class="model-controls-grid">${ADVANCED.map(numeric).join('')}</div>
      <label>Sampler order (one per line)<textarea data-generation="samplers" aria-label="Sampler order" rows="2" placeholder="Default"></textarea></label>
    </details>
    <label>Stop sequences (one per line)<textarea data-generation="stop" aria-label="Stop sequences" rows="2" placeholder="Default"></textarea></label>
    <p>GPU allocation, context capacity and MTP are configured in Cookbook → Launch.</p>
    <div class="model-controls-actions"><button type="button" data-action="reset">Reset fields</button><button type="button" data-action="save">Save controls</button></div>
    <div role="status" aria-live="polite"></div>`;
  button.addEventListener('click', () => panel.hidden ? open() : close());
  panel.querySelector('[data-action="close"]').addEventListener('click', () => { close(); button.focus(); });
  panel.querySelector('[data-action="save"]').addEventListener('click', save);
  panel.querySelector('[data-action="reset"]').addEventListener('click', () => { fill(); panel.querySelector('[role="status"]').textContent = 'Press Save controls to restore defaults.'; });
  panel.addEventListener('keydown', event => {
    event.stopPropagation();
    if (event.key === 'Escape') { event.preventDefault(); close(); button.focus(); }
    else if (event.key === 'Enter' && event.target.tagName === 'INPUT') { event.preventDefault(); save(); }
  });
  document.addEventListener('click', event => { if (!panel.contains(event.target) && !button.contains(event.target)) close(); });
  updateModelGenerationControls(getSelection());
}
