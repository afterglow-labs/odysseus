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
const fileSize = bytes => { const value = Number(bytes) || 0; if (value < 1048576) return `${Math.max(1,Math.round(value / 1024))} KB`; if (value < 1073741824) return `${(value / 1048576).toFixed(1)} MB`; return `${(value / 1073741824).toFixed(2)} GB`; };
const fileIcon = folder => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox','0 0 24 24'); svg.setAttribute('aria-hidden','true'); svg.classList.add('video-server-files-icon');
  const path = document.createElementNS(svg.namespaceURI,'path'); path.setAttribute('d',folder ? 'M3 7V5h6l2 2h10v13H3z' : 'M5 3h10l4 4v14H5z M10 10l5 3-5 3z'); svg.append(path); return svg;
};

export function showVideoServerFiles({ family, kind = 'video', multiple = false, onSelect }, anchor = document.activeElement) {
  if (!['h3', 'bfs'].includes(family)) throw new Error('Unknown video workflow family.');
  if(!['video','image','audio'].includes(kind))throw new Error('Unknown server media type.');
  const plural=kind==='audio'?'audio files':`${kind}s`,singular=kind==='audio'?'audio file':kind;
  const overlay = make('div', 'cookbook-edit-overlay video-server-files-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal h3-video-dialog video-server-files-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Server files');
  const header = make('div', 'h3-video-header'), heading = make('div'), closeButton = action('Close');
  heading.append(make('h2', '', 'Server files'),make('p','h3-video-muted',`Choose ${plural} from your Odysseus computer.`)); header.append(heading, closeButton);
  const controls = make('div', 'video-server-files-controls'), toolbar = make('div', 'video-server-files-nav');
  const driveLabel = make('label', 'h3-video-field'), drive = make('select', 'cookbook-field-input'); drive.setAttribute('aria-label', 'Drive');
  drive.add(new Option('E:', 'e')); drive.add(new Option('F:', 'f')); driveLabel.append(make('span', '', 'Drive'), drive);
  const back = action('Back'), up = action('Up'), refresh = action('Refresh'); toolbar.append(driveLabel,back,up,refresh);
  const location = make('nav', 'video-server-files-location'); location.setAttribute('aria-label','Folder breadcrumb');
  const search = make('input', 'cookbook-field-input video-server-files-search'); search.type = 'search'; search.placeholder = 'Search names in this folder…'; search.setAttribute('aria-label', 'Search server folder');
  const listingToolbar = make('div','video-server-files-list-toolbar');
  const status = make('p', 'h3-video-muted'); status.setAttribute('role', 'status');
  const error = make('p', 'h3-video-error'); error.setAttribute('role', 'alert'); error.hidden = true;
  const selectAll = action(`Select shown ${plural}`); selectAll.hidden = !multiple; listingToolbar.append(status,selectAll);
  controls.append(toolbar,location,search,error,listingToolbar);
  const content = make('div','video-server-files-browser'), columns = make('div','video-server-files-columns'); columns.setAttribute('aria-hidden','true');
  columns.append(make('span','','Name'),make('span','video-server-files-type','Type'),make('span','video-server-files-size','Size'));
  const list = make('ul', 'video-server-files-list'); list.setAttribute('aria-label', `Server folders and ${plural}`);
  const more = action('Load more'); more.classList.add('video-server-files-more'); more.hidden = true; content.append(columns,list,more);
  const footer = make('div', 'video-server-files-footer'), selectedTray = make('details','video-server-files-selected'), selectedSummary = make('summary'), selectedStatus = make('span'), selectedHint = make('small','h3-video-muted','Review selection'), selectedList = make('ul');
  selectedSummary.append(selectedStatus,selectedHint); selectedTray.append(selectedSummary,selectedList);
  const actions = make('div','video-server-files-actions'), help = make('small','h3-video-muted','Selections stay selected when you change folders or drives.'), use = action(`Use ${singular}`); use.className = 'cookbook-btn';
  const clear = action('Clear selection'); actions.append(help,clear,use); footer.append(selectedTray,actions);
  dialog.append(header,controls,content,footer); overlay.append(dialog); document.body.appendChild(overlay);
  let root = 'e', path = '', parent = null, entries = [], selected = new Map(), loading = false, closed = false, nextOffset = null, controller, debounce;
  const history = [];
  const close = bindMenuDismiss(overlay, () => {
    closed = true; controller?.abort(); clearTimeout(debounce); window.removeEventListener('keydown', onKey, true);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, () => false);
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),summary')].filter(node => !node.hidden && node.getClientRects().length);
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
    }
  }
  window.addEventListener('keydown', onKey, true); closeButton.onclick = close;
  function navigate(next, remember = true) {
    if (remember && (root !== next.root || path !== next.path)) history.push({root,path,search:search.value});
    root = next.root; path = next.path; drive.value = root; search.value = next.search || ''; load();
  }
  function renderLocation() {
    location.replaceChildren();
    const folders = path.split('/').filter(Boolean), parts = [{name:`${root.toUpperCase()}:`,path:''},...folders.map((name,index) => ({name,path:folders.slice(0,index+1).join('/')}))];
    parts.forEach((part,index) => {
      if (index) { const divider=make('span','video-server-files-separator','/'); divider.setAttribute('aria-hidden','true'); location.append(divider); }
      const crumb = action(part.name); crumb.title = part.path || `${root.toUpperCase()}:\\`; crumb.disabled = loading; crumb.onclick = () => navigate({root,path:part.path});
      if (index === parts.length-1) crumb.setAttribute('aria-current','page'); location.append(crumb);
    });
  }
  function renderSelection() {
    selectedStatus.textContent = `${selected.size} ${selected.size===1?singular:plural} selected${selected.size ? ` · ${fileSize([...selected.values()].reduce((sum,file) => sum+file.size,0))}` : ''}`;
    use.textContent = multiple ? `Use ${selected.size} ${selected.size===1?singular:plural}` : `Use ${singular}`; use.disabled = !selected.size; clear.disabled = !selected.size;
    selectedHint.hidden = !selected.size;
    if (!selected.size) selectedTray.open = false;
    selectedList.replaceChildren();
    for (const [key,selection] of selected) {
      const row=make('li'), label=make('span'), remove=action('Remove');
      label.append(make('strong','',selection.path.split('/').at(-1)),make('small','h3-video-muted',`${selection.root.toUpperCase()}:/${selection.path} · ${fileSize(selection.size)}`));
      remove.setAttribute('aria-label',`Remove ${selection.path.split('/').at(-1)} from selection`); remove.onclick = () => { selected.delete(key); renderSelection(); }; row.append(label,remove); selectedList.append(row);
    }
    const shown = entries.filter(entry => entry.kind === kind && entry.selection);
    selectAll.textContent = `${shown.length && shown.every(entry => selected.has(selectionKey(entry.selection))) ? 'Deselect' : 'Select'} shown ${plural}`;
    for (const box of list.querySelectorAll('input[data-server-selection]')) { box.checked = selected.has(box.dataset.serverSelection); box.closest('li').classList.toggle('selected',box.checked); }
  }
  function renderEntries() {
    list.replaceChildren(); list.setAttribute('aria-busy',String(loading));
    for (const entry of entries) {
      const row = make('li', 'video-server-files-row');
      if (entry.kind === 'directory') {
        const open = action(''); open.classList.add('video-server-files-entry'); open.setAttribute('aria-label',`Open folder ${entry.name}`); open.disabled = loading;
        const name=make('span','video-server-files-name'); name.append(fileIcon(true),make('span','',entry.name));
        open.append(name,make('small','video-server-files-type','Folder'),make('small','video-server-files-size','—'));
        open.onclick = () => navigate({root,path:entry.path}); row.append(open);
      } else if (entry.kind === kind && entry.selection) {
        const label = make('label','video-server-files-entry'), box = make('input'); box.type = multiple ? 'checkbox' : 'radio'; box.name = 'server-video'; box.disabled = loading;
        box.dataset.serverSelection = selectionKey(entry.selection);
        box.onchange = () => {
          if (!multiple) selected.clear();
          if (box.checked) selected.set(selectionKey(entry.selection), entry.selection); else selected.delete(selectionKey(entry.selection));
          renderSelection();
        };
        const name=make('span','video-server-files-name'); name.append(box,fileIcon(false),make('span','',entry.name));
        label.append(name,make('small','video-server-files-type',(entry.name.split('.').at(-1) || 'Video').toUpperCase()),make('small','video-server-files-size',fileSize(entry.size))); row.append(label);
      }
      list.append(row);
    }
    renderSelection();
  }
  async function load(append = false) {
    controller?.abort(); const current = new AbortController(); controller = current; loading = true;
    clearTimeout(debounce); error.hidden = true; status.textContent = 'Loading server folder…';
    if (!append) { entries = []; nextOffset = null; content.scrollTop = 0; }
    back.disabled = up.disabled = refresh.disabled = selectAll.disabled = more.disabled = true;
    renderEntries(); renderLocation();
    try {
      const query = new URLSearchParams({ root, path, kind, search: search.value, offset: String(append ? nextOffset || 0 : 0), limit: '200' });
      const response = await fetch(`/api/video/${family}/server-files?${query}`, { credentials: 'same-origin', signal: current.signal });
      let data = {}; try { data = await response.json(); } catch {}
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Could not browse server files (${response.status}).`);
      if (closed || current !== controller) return;
      root = data.root || root;
      if (Array.isArray(data.roots) && data.roots.length) drive.replaceChildren(...data.roots.map(item => new Option(`${item.name}${item.available ? '' : ' · unavailable'}`, item.id)));
      drive.value = root; path = data.path || ''; parent = data.parent_path;
      entries = append ? [...entries, ...(data.entries || [])] : data.entries || [];
      nextOffset = data.has_more ? data.next_offset : null;
      status.textContent = entries.length ? `${entries.length} of ${data.total ?? entries.length} items${search.value ? ' matching this search' : ''}` : search.value ? `No matching ${plural} or folders in this folder.` : `No ${plural} or folders found here.`;
    } catch (reason) {
      if (reason.name === 'AbortError' || closed || current !== controller) return;
      error.textContent = reason.message; error.hidden = false; status.textContent = ''; parent = path ? path.split('/').slice(0, -1).join('/') : null;
    } finally {
      if (!closed && current === controller) {
        loading = false; back.disabled = !history.length; up.disabled = parent === null; refresh.disabled = false;
        selectAll.disabled = !entries.some(item => item.kind === kind); more.hidden = nextOffset === null; more.disabled = false; renderEntries(); renderLocation();
      }
    }
  }
  drive.onchange = () => navigate({root:drive.value,path:''});
  back.onclick = () => { if (history.length) navigate(history.pop(),false); };
  up.onclick = () => { if (parent !== null) navigate({root,path:parent}); };
  refresh.onclick = () => load(); more.onclick = () => load(true);
  search.oninput = () => { clearTimeout(debounce); debounce = setTimeout(() => load(), 250); };
  selectAll.onclick = () => {
    const shown = entries.filter(entry => entry.kind === kind && entry.selection), remove = shown.every(entry => selected.has(selectionKey(entry.selection)));
    for (const entry of shown) if (remove) selected.delete(selectionKey(entry.selection)); else selected.set(selectionKey(entry.selection),entry.selection);
    renderSelection();
  };
  clear.onclick = () => { selected.clear(); renderSelection(); };
  use.onclick = () => {
    try { const files = [...selected.values()].map(serverVideoInput); onSelect(files); close(); }
    catch (reason) { error.textContent = reason.message; error.hidden = false; }
  };
  async function discoverRoots() {
    drive.disabled = search.disabled = refresh.disabled = up.disabled = back.disabled = true;
    const discovery = new AbortController(); controller = discovery;
    try {
      const response = await fetch(`/api/video/${family}/server-file-roots`, { credentials: 'same-origin', signal: discovery.signal });
      if (response.ok) {
        const data = await response.json();
        if (closed) return;
        if (Array.isArray(data.roots) && data.roots.length) {
          drive.replaceChildren(...data.roots.map(item => new Option(`${item.name}${item.available ? '' : ' · unavailable'}`, item.id)));
          root = (data.roots.find(item => item.available) || data.roots[0]).id; drive.value = root;
        }
      }
    } catch {}
    if (!closed) { drive.disabled = search.disabled = false; load(); }
  }
  renderSelection(); discoverRoots(); closeButton.focus();
  return { close };
}
