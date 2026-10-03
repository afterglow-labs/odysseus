// Account/model options for the ChatGPT subscription picker and request route.
const PREFERENCE_KEY = 'odysseus-model-daybreak';
const REASONING_PREFERENCE_KEY = 'odysseus-model-reasoning-effort';
const patches = new Map();
const routePatches = new Map();
const selectionRevisions = new WeakMap();
const rejectedPrograms = new Map();

// A rollback belongs to one edit, even if a later account/model pick happens
// to have the same visible values. Keep revisions off serialized session data.
export function beginChatSelection(session, field = 'route') {
  if (!session) return () => false;
  const state = selectionRevisions.get(session) || { route: 0, all: 0, fields: {} };
  selectionRevisions.set(session, state);
  const all = ++state.all;
  if (field === 'route') {
    state.route++;
    return () => state.all === all;
  }
  const routeRevision = state.route;
  state.fields[field] = all;
  return () => state.route === routeRevision && state.fields[field] === all;
}

export function isDaybreakSupported(url) {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname.replace(/\/+$/, '');
    return parsed.hostname.toLowerCase().replace(/\.$/, '') === 'chatgpt.com'
      && (path === '/backend-api/codex' || path.startsWith('/backend-api/codex/'));
  } catch { return false; }
}

function route(selection = {}) {
  const value = {
    model: selection.model || selection.modelId || selection.mid || '',
    endpoint_url: selection.endpoint_url || selection.url || '',
    endpoint_id: selection.endpoint_id || selection.endpointId || '',
  };
  return value;
}

function matchingEndpoints(value) {
  const url = value.endpoint_url.replace(/\/+$/, '');
  return (window.modelsModule?.getCachedItems?.() || []).filter(item =>
    String(item.url || '').replace(/\/+$/, '') === url
      && [...(item.models || []), ...(item.models_extra || [])].includes(value.model));
}

function capabilityEndpoint(selection) {
  const value = route(selection);
  if (value.endpoint_id) return (window.modelsModule?.getCachedItems?.() || []).find(item =>
    String(item.endpoint_id || '') === String(value.endpoint_id)
      && String(item.url || '').replace(/\/+$/, '') === value.endpoint_url.replace(/\/+$/, ''));
  const matches = matchingEndpoints(value);
  return matches.length === 1 ? matches[0] : null;
}

function rejectionKey(selection) {
  return preferenceKey(selection) ?? (selection.session_id
    ? JSON.stringify(['session', selection.session_id, route(selection).model]) : null);
}

export function daybreakCapability(selection) {
  const value = route(selection);
  const endpoint = capabilityEndpoint(selection);
  const key = rejectionKey(selection);
  const rejection = key && rejectedPrograms.get(key);
  if (rejection) {
    const revision = endpoint?.daybreak_revision;
    if (revision !== undefined && revision !== null && revision !== rejection.revision) rejectedPrograms.delete(key);
    else return { supported: false, reason: rejection.reason, source: 'upstream' };
  }
  const known = endpoint?.daybreak_models?.[value.model];
  return known && typeof known.supported === 'boolean' ? known
    : { supported: null, reason: known?.reason || '', source: 'unknown' };
}

export function recordDaybreakRejection(selection, payload) {
  if (!selection.daybreak_enabled || payload?.daybreak_supported !== false) return;
  let failed = selection;
  if (payload.endpoint_id) {
    const endpoint = (window.modelsModule?.getCachedItems?.() || []).find(item =>
      String(item.endpoint_id || '') === String(payload.endpoint_id));
    failed = { ...selection, endpoint_id: payload.endpoint_id,
      endpoint_url: endpoint?.url || payload.endpoint_url || selection.endpoint_url,
      model: payload.model || selection.model, session_id: '' };
  } else if (Number(payload.candidate_index) > 0 || (payload.model && payload.model !== selection.model)) {
    // A fallback may use another account on exactly the same URL. Without its
    // identity, leave the backend's account-bound capability cache authoritative.
    return;
  }
  const key = rejectionKey(failed);
  if (!key) return;
  rejectedPrograms.set(key, {
    revision: capabilityEndpoint(failed)?.daybreak_revision ?? null,
    reason: payload.text || (typeof payload.error === 'string' ? payload.error : payload.error?.message) || 'The provider rejected Daybreak for this model/account.',
  });
  updateDaybreakPicker();
}

export function sameChatRoute(a, b) {
  const left = route(a), right = route(b);
  return left.model === right.model && left.endpoint_url === right.endpoint_url
    && (String(left.endpoint_id) === String(right.endpoint_id)
      || (preferenceKey(a) !== null && preferenceKey(a) === preferenceKey(b)));
}

function preferenceKey(selection) {
  const value = route(selection);
  if (!value.endpoint_id) {
    const matches = matchingEndpoints(value);
    if (matches.length === 1) value.endpoint_id = matches[0].endpoint_id || '';
  }
  // Restored sessions do not carry an endpoint ID. Never guess between two
  // accounts exposing the same URL/model when remembering browser defaults.
  if (!value.endpoint_id && matchingEndpoints(value).length > 1) return null;
  return JSON.stringify([value.endpoint_id || value.endpoint_url.replace(/\/+$/, ''), value.model]);
}

export function daybreakForSelection(selection, { session = false } = {}) {
  if (!selection || !isDaybreakSupported(route(selection).endpoint_url)) return false;
  if (daybreakCapability(selection).supported === false) return false;
  if (session || typeof selection.daybreak_enabled === 'boolean') return selection.daybreak_enabled === true;
  try {
    const key = preferenceKey(selection);
    return key !== null && JSON.parse(localStorage.getItem(PREFERENCE_KEY) || '{}')[key] === true;
  } catch { return false; }
}

export function reasoningCapability(selection) {
  const known = capabilityEndpoint(selection)?.reasoning_models?.[route(selection).model];
  const levels = [...new Set((Array.isArray(known?.levels) ? known.levels : [])
    .filter(level => typeof level === 'string' && level.length > 0))];
  return { supported: known?.supported === true && levels.length ? true
    : known?.supported === false ? false : null, levels, default: known?.default || null };
}

export function reasoningForSelection(selection, { session = false, preserveUnknown = false } = {}) {
  if (!selection || !isDaybreakSupported(route(selection).endpoint_url)) return '';
  const capability = reasoningCapability(selection);
  if (capability.supported !== true) return capability.supported === null && preserveUnknown
    && typeof selection.reasoning_effort === 'string' ? selection.reasoning_effort : '';
  let effort = selection.reasoning_effort;
  if (!session && effort === undefined) {
    try {
      const key = preferenceKey(selection);
      effort = key === null ? '' : JSON.parse(localStorage.getItem(REASONING_PREFERENCE_KEY) || '{}')[key];
    } catch { effort = ''; }
  }
  return capability.levels.includes(effort) ? effort : '';
}

function rememberReasoning(selection, effort) {
  try {
    const key = preferenceKey(selection);
    if (key === null) return;
    const stored = JSON.parse(localStorage.getItem(REASONING_PREFERENCE_KEY) || '{}');
    const values = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
    values[key] = effort;
    localStorage.setItem(REASONING_PREFERENCE_KEY, JSON.stringify(values));
  } catch { /* private browsing or storage unavailable */ }
}

function remember(selection, enabled) {
  try {
    const key = preferenceKey(selection);
    if (key === null) return;
    const stored = JSON.parse(localStorage.getItem(PREFERENCE_KEY) || '{}');
    const values = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
    values[key] = enabled === true;
    localStorage.setItem(PREFERENCE_KEY, JSON.stringify(values));
  } catch { /* private browsing or storage unavailable */ }
}

// Serialize picker writes for each session so a slow toggle cannot overtake
// a later model/provider selection. Request options themselves are snapshots.
export function patchChatSelection(sessionId, fields) {
  const captured = { ...fields };
  const changesRoute = ['model', 'endpoint_url', 'endpoint_id'].some(key => key in captured);
  if (changesRoute) {
    routePatches.set(sessionId, (routePatches.get(sessionId) || 0) + 1);
    updateDaybreakPicker();
  }
  const previous = patches.get(sessionId) || Promise.resolve();
  const work = previous.catch(() => {}).then(async () => {
    const body = new FormData();
    Object.entries(captured).forEach(([key, value]) => body.append(key, String(value)));
    const response = await fetch(`${window.location.origin}/api/session/${encodeURIComponent(sessionId)}`, { method: 'PATCH', body });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || 'Failed to save model options');
    }
  });
  patches.set(sessionId, work);
  work.finally(() => {
    if (patches.get(sessionId) === work) patches.delete(sessionId);
    if (changesRoute) {
      const remaining = (routePatches.get(sessionId) || 1) - 1;
      if (remaining) routePatches.set(sessionId, remaining);
      else routePatches.delete(sessionId);
      updateDaybreakPicker();
    }
  }).catch(() => {});
  return work;
}

export function captureChatRoute(deps) {
  const id = deps.getCurrentSessionId?.();
  const session = (deps.getSessions?.() || []).find(s => s.id === id);
  const pending = !id && deps.getPendingChat?.();
  // Restored sessions beat the last model chosen in a different conversation.
  const selection = session || pending || (id ? {
    model: deps.getCurrentModel?.(), endpoint_url: deps.getCurrentEndpointUrl?.(),
  } : null) || {};
  const selected = { ...selection, session_id: id || '' };
  return Object.freeze({ ...route(selection), session_id: id || '',
    daybreak_enabled: daybreakForSelection(selected, { session: !!id }),
    reasoning_effort: reasoningForSelection(selected, { session: !!id, preserveUnknown: true }),
    source: selection.source || '',
  });
}

export function appendChatRoute(form, selection) {
  if (selection.model) form.append('selected_model', selection.model);
  if (selection.endpoint_url) form.append('selected_endpoint_url', selection.endpoint_url);
  if (selection.endpoint_id) form.append('selected_endpoint_id', selection.endpoint_id);
  form.append('daybreak_enabled', String(selection.daybreak_enabled));
  form.append('reasoning_effort', selection.reasoning_effort || '');
}

let pickerDeps;
let saving = false;
let savingReasoning = false;

function updateReasoningPicker(selection) {
  const row = document.getElementById('model-picker-reasoning-row');
  const select = document.getElementById('model-picker-reasoning');
  if (!row || !select) return;
  const capability = reasoningCapability(selection);
  row.hidden = !selection.model || !isDaybreakSupported(selection.endpoint_url);
  select.innerHTML = '';
  const option = (value, label, disabled = false) => {
    const element = document.createElement('option');
    element.value = value; element.textContent = label; element.disabled = disabled; select.appendChild(element);
  };
  const label = level => level === 'xhigh' ? 'Extra high' : level.charAt(0).toUpperCase() + level.slice(1);
  const knownDefault = capability.supported === true && capability.levels.includes(capability.default);
  option('', knownDefault ? `Provider default (${label(capability.default)})` : 'Provider default');
  if (capability.supported === true) capability.levels.forEach(level =>
    option(level, label(level)));
  const savedUnknown = capability.supported === null && !!selection.reasoning_effort;
  if (savedUnknown) option(selection.reasoning_effort, `${label(selection.reasoning_effort)} (saved)`, true);
  select.value = selection.reasoning_effort;
  select.disabled = savingReasoning || routePatches.has(selection.session_id)
    || (capability.supported !== true && !savedUnknown);
  const help = document.getElementById('model-picker-reasoning-help');
  if (help) help.textContent = capability.supported === true
    ? 'Choose how much reasoning this model uses. Higher effort may take longer.'
    : savedUnknown ? 'Keeping the saved effort while model capabilities are unavailable. You can reset it to provider default.'
    : capability.supported === false ? 'This model does not offer adjustable reasoning effort. Uses provider default.'
    : 'Reasoning levels are not advertised for this model. Uses provider default; refresh models to check again.';
}

function initReasoningPicker(deps, showError) {
  const select = document.getElementById('model-picker-reasoning');
  if (!select || select.dataset.reasoningBound) return;
  select.dataset.reasoningBound = '1';
  select.addEventListener('change', async () => {
    const selection = captureChatRoute(deps);
    const capability = reasoningCapability(selection);
    const effort = select.value;
    if (savingReasoning || routePatches.has(selection.session_id) || !isDaybreakSupported(selection.endpoint_url)
        || (capability.supported !== true && !(capability.supported === null && selection.reasoning_effort && effort === ''))
        || (effort !== '' && !capability.levels.includes(effort))) { updateDaybreakPicker(); return; }
    const id = deps.getCurrentSessionId?.();
    const session = (deps.getSessions?.() || []).find(s => s.id === id);
    if (!id) {
      const pending = deps.getPendingChat?.();
      if (pending) deps.setPendingChat({ ...pending, reasoning_effort: effort, source: 'manual' });
      rememberReasoning(selection, effort);
      updateDaybreakPicker();
      return;
    }
    if (!session) { updateDaybreakPicker(); return; }
    const isCurrentEdit = beginChatSelection(session, 'reasoning_effort');
    session.reasoning_effort = effort;
    savingReasoning = true;
    updateDaybreakPicker();
    try {
      await patchChatSelection(id, { reasoning_effort: effort });
      rememberReasoning(selection, effort);
    } catch (error) {
      if (isCurrentEdit()) session.reasoning_effort = selection.reasoning_effort;
      showError?.(`Failed to save reasoning effort: ${error.message}`);
    } finally {
      savingReasoning = false;
      updateDaybreakPicker();
    }
  });
}

export function updateDaybreakPicker() {
  if (!pickerDeps) return;
  const row = document.getElementById('model-picker-daybreak-row');
  const checkbox = document.getElementById('model-picker-daybreak');
  if (!row || !checkbox) return;
  const selection = captureChatRoute(pickerDeps);
  updateReasoningPicker(selection);
  const capability = daybreakCapability(selection);
  row.hidden = !selection.model || !isDaybreakSupported(selection.endpoint_url);
  checkbox.checked = selection.daybreak_enabled;
  checkbox.disabled = saving || routePatches.has(selection.session_id) || capability.supported === false;
  const help = document.getElementById('model-picker-daybreak-help');
  if (help) help.textContent = capability.supported === false
    ? 'Daybreak is unavailable for this model on your account. New messages use standard access.'
    : 'Uses your account’s approved access for this model.';
  const model = document.getElementById('model-picker-daybreak-model');
  if (model) model.textContent = selection.model;
}

export function initDaybreakPicker(deps, showError) {
  pickerDeps = deps;
  initReasoningPicker(deps, showError);
  const checkbox = document.getElementById('model-picker-daybreak');
  if (!checkbox || checkbox.dataset.daybreakBound) return;
  checkbox.dataset.daybreakBound = '1';
  checkbox.addEventListener('change', async () => {
    const selection = captureChatRoute(deps);
    if (saving || routePatches.has(selection.session_id) || !selection.model || !isDaybreakSupported(selection.endpoint_url)
        || daybreakCapability(selection).supported === false) { updateDaybreakPicker(); return; }
    const enabled = checkbox.checked;
    const id = deps.getCurrentSessionId?.();
    const session = (deps.getSessions?.() || []).find(s => s.id === id);
    if (!id) {
      const pending = deps.getPendingChat?.();
      if (pending) deps.setPendingChat({ ...pending, daybreak_enabled: enabled, source: 'manual' });
      remember(selection, enabled);
      updateDaybreakPicker();
      return;
    }
    if (!session) { updateDaybreakPicker(); return; }
    const isCurrentEdit = beginChatSelection(session, 'daybreak_enabled');
    saving = true;
    session.daybreak_enabled = enabled;
    updateDaybreakPicker();
    try {
      await patchChatSelection(id, { daybreak_enabled: enabled });
      remember(selection, enabled);
    } catch (error) {
      // Do not roll an old failed write into a newly selected model.
      if (isCurrentEdit()) session.daybreak_enabled = selection.daybreak_enabled;
      showError?.(`Failed to save Daybreak: ${error.message}`);
    } finally {
      saving = false;
      updateDaybreakPicker();
    }
  });
  updateDaybreakPicker();
}
