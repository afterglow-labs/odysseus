import { bindMenuDismiss } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { serverVideoInput } from './videoInputs.js';

const make = (tag, className = '', text) => {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const action = text => { const node = make('button', 'memory-toolbar-btn', text); node.type = 'button'; return node; };
const selectionKey = value => `${value.root}:${value.path}`;

export function showVideoServerFiles({ family, multiple = false, onSelect }, anchor = document.activeElement) {
  if (!['h3', 'bfs'].includes(family)) throw new Error('Unknown video workflow family.');
  const overlay = make('div', 'cookbook-edit-overlay video-server-files-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal h3-video-dialog video-server-files-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Server files');
  const header = make('div', 'h3-video-header'), closeButton = action('Close'); header.append(make('h2', '', 'Server files'), closeButton);
  const content = make('div', 'h3-video-scroll'), toolbar = make('div', 'h3-video-toolbar');
  const driveLabel = make('label', 'h3-video-field'), drive = make('select', 'cookbook-field-input'); drive.setAttribute('aria-label', 'Drive');
  drive.add(new Option('E:', 'e')); drive.add(new Option('F:', 'f')); driveLabel.append(make('span', '', 'Drive'), drive);
  const up = action('Up'), refresh = action('Refresh'); toolbar.append(driveLabel, up, refresh);
  const location = make('p', 'h3-video-muted video-server-files-location');
  const search = make('input', 'cookbook-field-input'); search.type = 'search'; search.placeholder = 'Search this folder…'; search.setAttribute('aria-label', 'Search server folder');
  const help = make('p', 'h3-video-muted', 'Browse videos on the Odysseus server. Each submitted job saves its own copy; no video download to this device is needed.');
  const status = make('p', 'h3-video-muted'); status.setAttribute('role', 'status');
  const error = make('p', 'h3-video-error'); error.setAttribute('role', 'alert'); error.hidden = true;
  const selectAll = action('Select shown videos'); selectAll.hidden = !multiple;
  const list = make('ul', 'video-server-files-list'); list.setAttribute('aria-label', 'Server folders and videos');
  const more = action('Load more'); more.hidden = true;
  const footer = make('div', 'h3-video-actions video-server-files-footer'), selectedStatus = make('span', 'h3-video-muted'), use = action('Use video'); use.className = 'cookbook-btn';
  const clear = action('Clear selection'); footer.append(selectedStatus, clear, use);
  content.append(toolbar, location, search, help, status, error, selectAll, list, more);
  dialog.append(header, content, footer); overlay.append(dialog); document.body.appendChild(overlay);
  let path = '', parent = null, entries = [], selected = new Map(), loading = false, closed = false, nextOffset = null, controller, debounce;
  const close = bindMenuDismiss(overlay, () => {
    closed = true; controller?.abort(); clearTimeout(debounce); window.removeEventListener('keydown', onKey, true);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, () => false);
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled)')].filter(node => !node.hidden && node.getClientRects().length);
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
    }
  }
  window.addEventListener('keydown', onKey, true); closeButton.onclick = close;
  function renderSelection() {
    selectedStatus.textContent = `${selected.size} video${selected.size === 1 ? '' : 's'} selected`;
    use.textContent = multiple ? `Use ${selected.size} videos` : 'Use video'; use.disabled = !selected.size; clear.disabled = !selected.size;
    for (const box of list.querySelectorAll('input[data-server-selection]')) box.checked = selected.has(box.dataset.serverSelection);
  }
  function renderEntries() {
    list.replaceChildren();
    for (const entry of entries) {
      const row = make('li', 'video-server-files-row');
      if (entry.kind === 'directory') {
        const open = action(`Folder · ${entry.name}`); open.disabled = loading;
        open.onclick = () => { path = entry.path; search.value = ''; load(); }; row.append(open);
      } else if (entry.kind === 'video' && entry.selection) {
        const label = make('label'), box = make('input'); box.type = multiple ? 'checkbox' : 'radio'; box.name = 'server-video';
        box.dataset.serverSelection = selectionKey(entry.selection);
        box.onchange = () => {
          if (!multiple) selected.clear();
          if (box.checked) selected.set(selectionKey(entry.selection), entry.selection); else selected.delete(selectionKey(entry.selection));
          renderSelection();
        };
        label.append(box, make('span', '', entry.name), make('small', 'h3-video-muted', `${(entry.size / 1048576).toFixed(1)} MB`)); row.append(label);
      }
      list.append(row);
    }
    renderSelection();
  }
  async function load(append = false) {
    controller?.abort(); const current = new AbortController(); controller = current; loading = true;
    clearTimeout(debounce); error.hidden = true; status.textContent = 'Loading server folder…';
    if (!append) { entries = []; nextOffset = null; }
    up.disabled = refresh.disabled = selectAll.disabled = more.disabled = true;
    renderEntries(); location.textContent = `${drive.selectedOptions[0]?.textContent || drive.value.toUpperCase() + ':'}/${path}`;
    try {
      const query = new URLSearchParams({ root: drive.value, path, search: search.value, offset: String(append ? nextOffset || 0 : 0), limit: '200' });
      const response = await fetch(`/api/video/${family}/server-files?${query}`, { credentials: 'same-origin', signal: current.signal });
      let data = {}; try { data = await response.json(); } catch {}
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Could not browse server files (${response.status}).`);
      if (closed || current !== controller) return;
      const root = data.root || drive.value;
      if (Array.isArray(data.roots) && data.roots.length) drive.replaceChildren(...data.roots.map(item => new Option(`${item.name}${item.available ? '' : ' · unavailable'}`, item.id)));
      drive.value = root; path = data.path || ''; parent = data.parent_path;
      entries = append ? [...entries, ...(data.entries || [])] : data.entries || [];
      nextOffset = data.has_more ? data.next_offset : null;
      status.textContent = entries.length ? `${entries.length} of ${data.total} folders and videos` : 'No videos or folders found here.';
      location.textContent = `${drive.selectedOptions[0]?.textContent || root.toUpperCase() + ':'}/${path}`;
    } catch (reason) {
      if (reason.name === 'AbortError' || closed || current !== controller) return;
      error.textContent = reason.message; error.hidden = false; status.textContent = ''; parent = path ? path.split('/').slice(0, -1).join('/') : null;
    } finally {
      if (!closed && current === controller) {
        loading = false; up.disabled = parent === null; refresh.disabled = false;
        selectAll.disabled = !entries.some(item => item.kind === 'video'); more.hidden = nextOffset === null; more.disabled = false; renderEntries();
      }
    }
  }
  drive.onchange = () => { path = ''; search.value = ''; load(); };
  up.onclick = () => { if (parent !== null) { path = parent; search.value = ''; load(); } };
  refresh.onclick = () => load(); more.onclick = () => load(true);
  search.oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => load(), 250); };
  selectAll.onclick = () => { for (const entry of entries) if (entry.kind === 'video' && entry.selection) selected.set(selectionKey(entry.selection), entry.selection); renderSelection(); };
  clear.onclick = () => { selected.clear(); renderSelection(); };
  use.onclick = () => {
    try { const files = [...selected.values()].map(serverVideoInput); onSelect(files); close(); }
    catch (reason) { error.textContent = reason.message; error.hidden = false; }
  };
  async function discoverRoots() {
    drive.disabled = search.disabled = refresh.disabled = up.disabled = true;
    const discovery = new AbortController(); controller = discovery;
    try {
      const response = await fetch(`/api/video/${family}/server-file-roots`, { credentials: 'same-origin', signal: discovery.signal });
      if (response.ok) {
        const data = await response.json();
        if (closed) return;
        if (Array.isArray(data.roots) && data.roots.length) {
          drive.replaceChildren(...data.roots.map(item => new Option(`${item.name}${item.available ? '' : ' · unavailable'}`, item.id)));
          drive.value = (data.roots.find(item => item.available) || data.roots[0]).id;
        }
      }
    } catch {}
    if (!closed) { drive.disabled = search.disabled = false; load(); }
  }
  renderSelection(); discoverRoots(); closeButton.focus();
  return { close };
}
