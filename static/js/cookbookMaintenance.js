import { bindMenuDismiss, dismissOrRemove, dismissTopMenu } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';

const element = (tag, className = '', text) => {
  const el = document.createElement(tag);
  el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
};
const button = text => {
  const el = element('button', 'memory-toolbar-btn', text);
  el.type = 'button';
  return el;
};

function maintenanceDialog(title, anchor, onClose = () => {}) {
  document.querySelectorAll('.cookbook-maintenance').forEach(dismissOrRemove);
  const overlay = element('div', 'cookbook-edit-overlay cookbook-maintenance');
  overlay.style.zIndex = String(topPortalZ());
  const dialog = element('div', 'cookbook-edit-modal');
  dialog.style.cssText = 'width:min(760px,calc(100vw - 32px));max-height:85vh;overflow:auto;';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-label', title);
  dialog.appendChild(element('div', 'cookbook-edit-title', title));
  const content = element('div');
  dialog.appendChild(content);
  const actions = element('div', 'cookbook-edit-actions');
  const done = button('Close');
  actions.appendChild(done);
  dialog.appendChild(actions);
  overlay.appendChild(dialog);
  document.body.appendChild(overlay);
  const onKey = event => {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); dismissTopMenu();
    } else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled)')];
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  const close = bindMenuDismiss(overlay, () => {
    onClose(); window.removeEventListener('keydown', onKey, true);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, event => event.target === overlay);
  done.addEventListener('click', close);
  window.addEventListener('keydown', onKey, true);
  done.focus();
  return { overlay, content };
}

const normalize = value => String(value || '').toLowerCase().replace(/[-_.]+/g, '-');

// The target is captured when opened. Changing Cookbook's server selector
// must never redirect an update from an already-open package inventory.
export function showEnvironmentPackages(target, onInstall, anchor = document.activeElement) {
  target = { ...target };
  let closed = false, busy = false, inventory = [], checked = false, refreshQueued = false, completionNote = '';
  const pending = new Set();
  const { overlay, content } = maintenanceDialog('Manage packages', anchor, () => {
    closed = true;
    window.removeEventListener('cookbook:dependency-finished', onFinished);
  });
  content.appendChild(element('p', 'memory-desc', target.host ? `Python environment on ${target.host}` : 'Odysseus Python environment on this PC'));
  const python = element('p', 'memory-item-meta');
  python.style.cssText = 'overflow-wrap:anywhere;user-select:text;';
  content.appendChild(python);
  const toolbar = element('div');
  toolbar.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:12px 0;';
  const search = element('input', 'memory-search-input');
  search.type = 'search'; search.placeholder = 'Search installed packages';
  search.setAttribute('aria-label', 'Search installed packages');
  search.style.cssText = 'flex:1;min-width:140px;';
  const refresh = button('Refresh');
  const check = button('Check for updates');
  toolbar.appendChild(search); toolbar.appendChild(refresh); toolbar.appendChild(check);
  content.appendChild(toolbar);
  const status = element('p', 'memory-item-meta');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  content.appendChild(status);
  const list = element('div');
  list.style.cssText = 'max-height:48vh;overflow-y:auto;display:flex;flex-direction:column;gap:8px;';
  content.appendChild(list);
  const render = () => {
    list.innerHTML = '';
    const query = normalize(search.value).trim();
    const shown = inventory.filter(pkg => normalize(pkg.name).includes(query));
    for (const pkg of shown) {
      const row = element('div');
      row.dataset.package = pkg.name;
      row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--border);';
      const detail = element('div'); detail.style.cssText = 'flex:1;min-width:0;overflow-wrap:anywhere;';
      detail.appendChild(element('div', 'memory-item-title', pkg.name));
      detail.appendChild(element('div', 'memory-item-meta', pkg.version
        ? `${pkg.version}${pkg.update_available ? ` → ${pkg.latest_version}` : checked ? ' · Up to date' : ''}`
        : 'Not installed'));
      row.appendChild(detail);
      const update = button(pkg.version ? 'Update' : 'Install');
      update.setAttribute('aria-label', `${pkg.version ? 'Update' : 'Install'} ${pkg.name}`);
      const repair = button('Repair');
      repair.setAttribute('aria-label', `Repair ${pkg.name}`);
      repair.title = 'Reinstall this version to restore missing or damaged package files';
      const active = pending.has(normalize(pkg.name));
      update.disabled = active; repair.disabled = active;
      if (active) update.textContent = 'Working…';
      const run = async reinstall => {
        if (pending.size) { status.textContent = 'Wait for the current package operation to finish. Progress is in Cookbook → Active.'; return; }
        completionNote = '';
        pending.add(normalize(pkg.name)); render();
        try {
          const started = await onInstall(pkg, reinstall, target);
          if (!started) { pending.delete(normalize(pkg.name)); status.textContent = 'The package operation could not start. You can retry.'; render(); }
          else status.textContent = `${reinstall ? 'Repairing' : 'Updating'} ${pkg.name}. Progress is in Cookbook → Active; this list refreshes when it finishes.`;
        } catch (error) {
          pending.delete(normalize(pkg.name)); status.textContent = error.message; render();
        }
      };
      update.addEventListener('click', () => run(false));
      repair.addEventListener('click', () => run(true));
      row.appendChild(update);
      if (pkg.version) row.appendChild(repair);
      list.appendChild(row);
    }
    if (!shown.length) list.appendChild(element('p', 'memory-item-meta', 'No matching packages.'));
  };
  const load = async (checkUpdates = false) => {
    if (closed) return;
    if (busy) { refreshQueued = true; return; }
    busy = true; refresh.disabled = true; check.disabled = true;
    status.textContent = checkUpdates ? 'Checking available updates…' : 'Reading this Python environment…';
    const params = new URLSearchParams({ check_updates: String(checkUpdates) });
    if (target.host) {
      params.set('host', target.host);
      params.set('ssh_port', target.sshPort || '');
      params.set('env', target.env || 'none');
      params.set('env_path', target.envPath || '');
      params.set('platform', target.platform || '');
    }
    try {
      const response = await fetch('/api/cookbook/environment-packages?' + params, { credentials: 'same-origin' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || data.error || `Environment check failed (${response.status})`);
      if (closed) return;
      checked = !!data.updates_checked;
      inventory = [...data.packages];
      for (const name of ['pip', 'setuptools', 'wheel']) {
        if (!inventory.some(pkg => normalize(pkg.name) === name)) inventory.push({ name, version: '' });
      }
      const priority = name => ['pip', 'setuptools', 'wheel'].includes(normalize(name)) ? 0 : 1;
      inventory.sort((a, b) => priority(a.name) - priority(b.name) || a.name.localeCompare(b.name));
      python.textContent = `Python ${data.python_version} · ${data.executable}`;
      status.textContent = data.check_error ? `Update check incomplete: ${data.check_error}`
        : checked ? `${data.packages.length} installed packages · ${inventory.filter(pkg => pkg.update_available).length} updates available`
        : `${data.packages.length} installed packages. Update checks for the newest compatible release; Repair reinstalls the current version.`;
      if (completionNote) status.textContent = completionNote + ' ' + status.textContent;
      render();
    } catch (error) { if (!closed) status.textContent = error.message; }
    finally {
      busy = false; refresh.disabled = false; check.disabled = false;
      if (refreshQueued && !closed) { refreshQueued = false; void load(false); }
    }
  };
  function onFinished(event) {
    const task = event.detail || {};
    const payload = task.payload || {};
    if ((task.remoteHost || payload.remote_host || '') !== (target.host || '')) return;
    if (target.host && (String(task.sshPort || payload.ssh_port || '22') !== String(target.sshPort || '22') || (payload.env_path || '') !== (target.envPath || ''))) return;
    if (pending.delete(normalize(payload.repo_id))) {
      completionNote = ['error', 'crashed', 'stopped', 'killed'].includes(task.status)
        ? `${payload.repo_id} did not finish successfully. See Cookbook → Active for details; Update or Repair can retry it.`
        : `${payload.repo_id} finished. Restart Odysseus when convenient to use updates to packages that were already loaded.`;
    }
    void load(false);
  }
  window.addEventListener('cookbook:dependency-finished', onFinished);
  search.addEventListener('input', render);
  refresh.addEventListener('click', () => load(false));
  check.addEventListener('click', () => load(true));
  void load();
  return overlay;
}

export function showClearVram({ target, models, stopModels, refresh }, anchor = document.activeElement) {
  const { overlay, content } = maintenanceDialog('Clear VRAM', anchor);
  const local = !target.host;
  content.appendChild(element('p', 'memory-desc', local ? 'GPU memory on this PC' : `GPU memory on ${target.host}`));
  content.appendChild(element('p', 'memory-item-meta', 'Release unused cache, or unload Odysseus models to free their memory. Other applications keep running.'));
  if (models.length) content.appendChild(element('p', 'memory-item-meta', `Running models: ${models.map(model => model.name).join(', ')}`));
  const status = element('p', 'memory-item-meta');
  status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  content.appendChild(status);
  const cache = button('Clear unused cache');
  cache.title = local ? 'Release unused GPU cache in Odysseus; loaded models remain in memory' : 'Cache clearing is available for the local Odysseus app. Unload remote models to release their memory.';
  cache.disabled = !local;
  const unload = button('Unload models and clear');
  unload.title = 'Stops the listed model servers and releases cached editor models. Current generations on these servers stop.';
  unload.disabled = !local && !models.length;
  const controls = element('div'); controls.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;';
  controls.appendChild(cache); controls.appendChild(unload); content.appendChild(controls);
  content.appendChild(element('p', 'memory-item-meta', 'Unloading stops current generations. Editor models load again when next used.'));
  let busy = false;
  const run = async unloadModels => {
    if (busy) return;
    busy = true; cache.disabled = true; unload.disabled = true;
    status.textContent = unloadModels ? 'Unloading models…' : 'Clearing unused GPU cache…';
    try {
      const stopped = unloadModels ? await stopModels(models) : { stopped: [], errors: [] };
      let data = { message: `${stopped.stopped.length} model servers stopped.` };
      if (local) {
        const response = await fetch('/api/cookbook/clear-vram', {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ unload_models: unloadModels }),
        });
        data = await response.json();
        if (!response.ok) throw new Error(data.detail || data.error || 'Could not clear GPU memory');
      }
      status.textContent = [stopped.stopped.length ? `Stopped ${stopped.stopped.length} model servers.` : '', data.message, ...(data.errors || []), ...(stopped.errors || [])].filter(Boolean).join(' ');
      await refresh?.();
    } catch (error) { status.textContent = error.message; }
    finally { busy = false; cache.disabled = !local; unload.disabled = !local && !models.length; }
  };
  cache.addEventListener('click', () => run(false));
  unload.addEventListener('click', () => run(true));
  return overlay;
}
