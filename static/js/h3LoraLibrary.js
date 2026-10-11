import { bindMenuDismiss } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { h3LoraStrengthControl, H3_LORA_RECOMMENDED_FIELDS } from './h3Loras.js';

const API = '/api/video/h3/lora-library';
const activeDownload = row => ['queued', 'downloading'].includes(row.status);
const displayName = row => row.display_name || row.lora_profile?.display_name || row.name;
const make = (tag, className = '', text) => {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const action = text => { const node = make('button', 'memory-toolbar-btn', text); node.type = 'button'; return node; };
const bytes = value => value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : `${(Math.max(0, value || 0) / 1048576).toFixed(1)} MiB`;

export function loraProfileFromComponent(component = {}) {
  component ||= {};
  const saved = component.lora_profile || {}, strength = h3LoraStrengthControl(component);
  return {
    custom_strength: !!saved.strength_control,
    display_name: saved.display_name || component.display_name || '',
    trigger_words: saved.trigger_words || '', trigger_position: saved.trigger_position || 'manual',
    notes: saved.notes || '', variant: saved.variant || component.variant || 'ref2va',
    strength_control: { kind: 'custom', label: strength.label, min: strength.min, max: strength.max,
      step: strength.step, default: strength.default, labels: strength.labels || {} },
    recommended_settings: { ...(saved.recommended_settings || {}) },
  };
}

async function libraryRequest(path, options = {}) {
  const response = await fetch(API + path, { credentials: 'same-origin', ...options });
  let value = {}; try { value = await response.json(); } catch {}
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : typeof value.error === 'string' ? value.error : `LoRA library request failed (${response.status}).`);
  return value;
}

export function showH3LoraLibrary({ components = [], selectedId = '', onChange = async () => {} } = {}, anchor = document.activeElement) {
  const overlay = make('div', 'cookbook-edit-overlay h3-lora-library-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal h3-video-dialog h3-lora-library-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Manage LoRAs');
  const header = make('div', 'h3-video-header'), closeButton = action('Close'); header.append(make('h2', '', 'Manage LoRAs'), closeButton);
  const scroll = make('div', 'h3-video-scroll'), form = make('form');
  scroll.append(make('p', 'h3-video-muted', 'Add a LoRA file and save its trigger, controls, and recommended settings. Select it in the workflow afterward. Triggers and recommended settings are applied only when you choose their buttons.'), form);
  const fields = {}, recommends = {};
  function field(parent, key, title, { type = 'text', options, value = '', help = '', min, max, step, rows = 3 } = {}) {
    const label = make('label', 'h3-video-field'); label.append(make('span', '', title));
    const input = make(options ? 'select' : type === 'textarea' ? 'textarea' : 'input', 'cookbook-field-input');
    input.id = `h3-library-${key}`; input.name = key;
    if (options) options.forEach(([value, text]) => input.add(new Option(text, value)));
    else if (type === 'textarea') input.rows = rows;
    else input.type = type;
    for (const [key, value] of Object.entries({ min, max, step })) if (value !== undefined) input[key] = value;
    input.value = value; label.append(input); if (help) label.append(make('small', 'h3-video-muted', help));
    parent.append(label); fields[key] = input; return input;
  }
  const source = field(form, 'source', 'LoRA file', { value: 'installed', options: [['installed', 'Installed LoRA'], ['upload', 'Upload a file'], ['hf', 'Download from Hugging Face']] });
  const installed = make('div', 'h3-lora-library-source'); form.append(installed);
  const componentSelect = field(installed, 'component', 'Installed LoRA', { options: [] });
  const refresh = action('Refresh installed files'); installed.append(refresh);
  const componentPath = make('p', 'h3-video-muted h3-video-component-path'); installed.append(componentPath);
  const upload = make('div', 'h3-lora-library-source'); form.append(upload);
  const file = field(upload, 'file', 'LoRA file to upload', { type: 'file', help: 'Safetensors only, up to 8 GiB. The file is saved in the server’s managed model folder.' }); file.accept = '.safetensors';
  const hf = make('div', 'h3-lora-library-source'); form.append(hf);
  const url = field(hf, 'url', 'Hugging Face file URL', { help: 'Paste the page for the exact .safetensors file, or enter its repository and filename below.' });
  url.placeholder = 'https://huggingface.co/author/repository/blob/main/adapter.safetensors';
  const hfGrid = make('div', 'h3-video-grid'); hf.append(hfGrid);
  field(hfGrid, 'repository', 'Repository', { help: 'author/repository' });
  field(hfGrid, 'filename', 'Filename', { help: 'Include subfolders, if any.' });
  field(hf, 'revision', 'Revision (optional)', { help: 'Branch, tag, or commit. Leave blank to use the repository’s default.' });
  const settings = make('div', 'h3-lora-profile-form'); form.append(settings);
  field(settings, 'display_name', 'Display name', { help: 'Optional. The original filename remains available in the library.' });
  field(settings, 'variant', 'H3 workflow family', { options: [['ref2va', 'References (Ref2VA)'], ['fl2va', 'Text / first and last frame (FL2VA)'], ['shared', 'Both H3 families']] });
  field(settings, 'trigger_words', 'Trigger words', { type: 'textarea', help: 'The exact word or phrase specified by the LoRA’s author.' });
  field(settings, 'trigger_position', 'Trigger placement', { options: [['manual', 'Manual — insert at the prompt cursor'], ['prefix', 'Beginning of prompt'], ['suffix', 'End of prompt']] });
  field(settings, 'notes', 'Instructions and other parameter notes', { type: 'textarea', rows: 4,
    help: 'Record required inputs, compatibility details, or settings that are not supported below. Notes are shown with the LoRA and are not applied automatically.' });
  const strength = make('fieldset', 'h3-lora-library-fieldset'); strength.append(make('legend', '', 'Strength control')); settings.append(strength);
  const customLabel = make('label', 'video-batch-toggle'), customSlider = make('input'); customSlider.type = 'checkbox'; customSlider.id = 'h3-library-custom-strength'; fields.custom_strength = customSlider;
  customLabel.append(customSlider, make('span', '', 'Use a custom strength slider')); strength.append(customLabel);
  strength.append(make('p', 'h3-video-muted', 'Leave this off to keep the built-in strength control, including the Age slider.'));
  const strengthSettings = make('div'); strength.append(strengthSettings);
  field(strengthSettings, 'strength_label', 'Control label', { value: 'Strength' });
  const strengthGrid = make('div', 'h3-video-grid h3-video-numbers'); strengthSettings.append(strengthGrid);
  for (const [key, title, value] of [['min', 'Minimum', -4], ['max', 'Maximum', 4], ['step', 'Step', 0.05], ['default', 'Default', 1]]) {
    field(strengthGrid, `strength_${key}`, title, { type: 'number', value, min: key === 'step' ? 0.0001 : -100, max: 100, step: 'any' });
  }
  const labelsGrid = make('div', 'h3-video-grid'); strengthSettings.append(labelsGrid);
  for (const [key, title] of [['min', 'Minimum label'], ['zero', 'Zero label'], ['max', 'Maximum label']]) field(labelsGrid, `label_${key}`, title);
  const recommended = make('fieldset', 'h3-lora-library-fieldset'); recommended.append(make('legend', '', 'Recommended workflow settings')); settings.append(recommended);
  recommended.append(make('p', 'h3-video-muted', 'Leave unspecified settings blank. “Apply recommended settings” updates only the values you save here and is always an explicit action in the workflow.'));
  const grid = make('div', 'h3-video-grid h3-video-numbers'); recommended.append(grid);
  const specs = {
    steps: ['Steps', { type: 'number', min: 1, max: 100, step: 1 }],
    sampler: ['Sampler', { options: [['', 'Unspecified'], ['euler', 'Euler'], ['res_multistep', 'Multistep']] }],
    scheduler: ['Scheduler', { options: [['', 'Unspecified'], ['simple', 'Simple'], ['normal', 'Normal']] }],
    shift_video: ['Video shift', { type: 'number', min: 0.01, max: 100, step: 'any' }],
    shift_audio: ['Audio shift', { type: 'number', min: 0.01, max: 100, step: 'any' }],
    width: ['Width', { type: 'number', min: 256, max: 1920, step: 32 }],
    height: ['Height', { type: 'number', min: 256, max: 1920, step: 32 }],
    frames: ['Length at 24 fps', { options: [['', 'Unspecified'], ...Array.from({ length: 18 }, (_, i) => 73 + i * 17).map(n => [String(n), `${(n / 24).toFixed(2)}s · ${n} frames`])] }],
    reference_size: ['Reference sizing', { options: [['', 'Unspecified'], ['match', 'Match'], ['max', 'Maximum']] }],
    seed: ['Seed', { type: 'number', min: 0, max: Number.MAX_SAFE_INTEGER, step: 1 }],
  };
  for (const key of H3_LORA_RECOMMENDED_FIELDS) { const [title, spec] = specs[key]; recommends[key] = field(grid, `recommended_${key}`, title, spec); }
  const message = make('p', 'h3-video-muted'); message.setAttribute('role', 'status');
  const error = make('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert');
  const progress = make('progress', 'h3-video-export-progress'); progress.hidden = true; progress.setAttribute('aria-label', 'LoRA file upload');
  const submit = action('Save profile'); submit.type = 'submit'; submit.className = 'cookbook-btn';
  const actions = make('div', 'h3-video-actions'); actions.append(submit); form.append(message, error, progress, actions);
  const downloads = make('section', 'h3-lora-library-downloads'); downloads.append(make('h3', '', 'Downloads'));
  const downloadList = make('div'); downloads.append(downloadList); scroll.append(downloads);
  dialog.append(header, scroll); overlay.append(dialog); document.body.append(overlay);
  let closed = false, busy = false, loading = false, xhr = null, timer = null, library = components.filter(row => row.role === 'lora'), downloadRows = [], initial = true;
  let savedSelection = selectedId, sourceValue = 'installed', currentSelection = '', fileAfterUpload = null, selectAfterLoad = false;
  const drafts = new Map(), announced = new Set();
  function rawDraft() { return Object.fromEntries(Object.entries(fields).filter(([key]) => !['source', 'component', 'file'].includes(key)).map(([key, input]) => [key, input.type === 'checkbox' ? input.checked : input.value])); }
  function restoreRaw(values) { for (const [key, value] of Object.entries(values || {})) if (fields[key]) { if (fields[key].type === 'checkbox') fields[key].checked = value; else fields[key].value = value; } }
  function saveDraft() { drafts.set(`${sourceValue}:${currentSelection}`, rawDraft()); }
  function fill(profile) {
    customSlider.checked = !!profile.custom_strength;
    for (const key of ['display_name', 'variant', 'trigger_words', 'trigger_position', 'notes']) fields[key].value = profile[key] || '';
    const control = profile.strength_control;
    fields.strength_label.value = control.label;
    for (const key of ['min', 'max', 'step', 'default']) fields[`strength_${key}`].value = control[key];
    for (const key of ['min', 'zero', 'max']) fields[`label_${key}`].value = control.labels?.[key] || '';
    for (const [key, input] of Object.entries(recommends)) input.value = profile.recommended_settings?.[key] ?? '';
  }
  function readProfile() {
    const control = { kind: 'custom', label: fields.strength_label.value.trim() || 'Strength', labels: {} };
    for (const key of ['min', 'max', 'step', 'default']) {
      const value = fields[`strength_${key}`].value;
      if (customSlider.checked && (!value.trim() || !Number.isFinite(Number(value)))) throw new Error(`Enter a valid strength ${key}.`);
      control[key] = Number(value);
    }
    if (customSlider.checked && (control.min >= control.max || control.min < -100 || control.max > 100 || control.step <= 0 || control.default < control.min || control.default > control.max)) throw new Error('Choose a valid strength range within −100…100, a positive step, and a default inside that range.');
    for (const key of ['min', 'zero', 'max']) if (fields[`label_${key}`].value.trim()) control.labels[key] = fields[`label_${key}`].value.trim();
    const recommended_settings = {};
    for (const [key, input] of Object.entries(recommends)) if (input.value !== '') recommended_settings[key] = ['sampler', 'scheduler', 'reference_size'].includes(key) ? input.value : Number(input.value);
    return { display_name: fields.display_name.value.trim(), trigger_words: fields.trigger_words.value.trim(), trigger_position: fields.trigger_position.value,
      variant: fields.variant.value, notes: fields.notes.value, ...(customSlider.checked ? { strength_control: control } : {}), recommended_settings };
  }
  function renderSource() {
    installed.hidden = source.value !== 'installed'; upload.hidden = source.value !== 'upload'; hf.hidden = source.value !== 'hf';
    submit.textContent = busy ? 'Saving…' : source.value === 'hf' ? 'Download and save profile' : source.value === 'upload' ? 'Upload and save profile' : 'Save profile';
    componentPath.textContent = library.find(row => row.id === componentSelect.value)?.path || '';
    submit.disabled = busy || loading || source.value === 'installed' && !componentSelect.value;
    for (const input of form.querySelectorAll('input,select,textarea')) input.disabled = busy || loading && initial;
    strengthSettings.hidden = !customSlider.checked;
    for (const input of strengthSettings.querySelectorAll('input')) input.disabled = busy || loading && initial || !customSlider.checked;
    refresh.disabled = busy || loading;
  }
  function chooseDraft() {
    const component = source.value === 'installed' ? library.find(row => row.id === componentSelect.value) : null;
    sourceValue = source.value; currentSelection = sourceValue === 'installed' ? componentSelect.value : '';
    fill(loraProfileFromComponent(component)); restoreRaw(drafts.get(`${sourceValue}:${currentSelection}`)); renderSource();
  }
  function showError(reason) { error.textContent = reason.message || String(reason); error.hidden = false; }
  function renderDownloads() {
    downloads.hidden = !downloadRows.length; downloadList.replaceChildren();
    for (const row of downloadRows) {
      const card = make('div', 'h3-lora-library-download');
      card.append(make('strong', '', row.component ? displayName(row.component) : row.filename || row.repository || row.id));
      card.append(make('p', 'h3-video-muted', `${row.status}${row.downloaded_bytes ? ` · ${bytes(row.downloaded_bytes)}${row.total_bytes ? ` / ${bytes(row.total_bytes)}` : ''}` : ''}`));
      if (row.error) card.append(make('p', 'h3-video-error', row.error));
      if (activeDownload(row)) {
        const cancel = action('Cancel download'); cancel.onclick = async () => {
          cancel.disabled = true;
          try { await libraryRequest(`/downloads/${encodeURIComponent(row.id)}`, { method: 'DELETE' }); await load(); }
          catch (reason) { showError(reason); cancel.disabled = false; }
        }; card.append(cancel);
      }
      downloadList.append(card);
    }
  }
  async function changed() { try { await onChange(); } catch (reason) { if (!closed) showError(reason); } }
  async function load() {
    if (closed || loading) return;
    loading = true; renderSource();
    try {
      const data = await libraryRequest(''); if (closed) return;
      library = data.components || []; downloadRows = data.downloads || [];
      const selected = componentSelect.value || savedSelection;
      componentSelect.replaceChildren(new Option('Choose an installed LoRA…', ''), ...library.map(row => new Option(`${displayName(row)}${row.display_name ? ` · ${row.name}` : ''}`, row.id)));
      componentSelect.value = library.some(row => row.id === selected) ? selected : '';
      const firstLoad = initial;
      if (initial || selectAfterLoad) { initial = false; selectAfterLoad = false; chooseDraft(); }
      renderDownloads();
      let notify = false;
      for (const row of downloadRows) if (row.status === 'completed' && !announced.has(row.id)) { announced.add(row.id); notify ||= !firstLoad; }
      if (notify) await changed();
    } catch (reason) { if (!closed) showError(reason); }
    finally {
      loading = false;
      if (!closed) { renderSource(); clearTimeout(timer); if (downloadRows.some(activeDownload)) timer = setTimeout(load, 1500); }
    }
  }
  function uploadFile(selected) {
    return new Promise((resolve, reject) => {
      xhr = new XMLHttpRequest(); xhr.open('POST', `${API}/upload?${new URLSearchParams({ filename: selected.name })}`); xhr.withCredentials = true;
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      xhr.upload.onprogress = event => { if (!closed) { progress.hidden = false; if (event.lengthComputable) { progress.max = event.total; progress.value = event.loaded; } message.textContent = `Uploading ${bytes(event.loaded)} / ${bytes(selected.size)}…`; } };
      xhr.onerror = () => reject(new Error('Upload connection lost. Refresh installed files before trying again.'));
      xhr.onabort = () => reject(new Error('Upload cancelled.'));
      xhr.onload = () => {
        let value = {}; try { value = JSON.parse(xhr.responseText); } catch {}
        if (xhr.status >= 200 && xhr.status < 300 && value.component) resolve(value.component);
        else reject(new Error(typeof value.detail === 'string' ? value.detail : `LoRA upload failed (${xhr.status}).`));
      };
      xhr.send(selected);
    });
  }
  source.onchange = () => { saveDraft(); chooseDraft(); error.hidden = true; message.textContent = ''; };
  componentSelect.onchange = () => { saveDraft(); chooseDraft(); error.hidden = true; message.textContent = ''; };
  file.onchange = () => { fileAfterUpload = null; };
  customSlider.onchange = renderSource;
  refresh.onclick = () => load();
  form.onsubmit = async event => {
    event.preventDefault(); if (busy || !form.reportValidity()) return;
    error.hidden = true; message.textContent = '';
    try {
      const profile = readProfile(); busy = true; renderSource();
      if (source.value === 'hf') {
        const urlValue = url.value.trim();
        const target = urlValue ? { url: urlValue } : { repository: fields.repository.value.trim(), filename: fields.filename.value.trim() };
        if (!urlValue && (!target.repository || !target.filename)) throw new Error('Enter a Hugging Face file URL, or both repository and filename.');
        if (fields.revision.value.trim()) target.revision = fields.revision.value.trim();
        const status = await libraryRequest('/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...target, profile }) });
        downloadRows = [status, ...downloadRows.filter(row => row.id !== status.id)]; renderDownloads();
        message.textContent = 'Download started on the server. Its saved profile will be available when the file finishes downloading. You can close this panel.';
      } else {
        let id = componentSelect.value;
        if (source.value === 'upload') {
          const selected = file.files?.[0];
          if (!fileAfterUpload) {
            if (!selected || !/\.safetensors$/i.test(selected.name)) throw new Error('Choose a .safetensors LoRA file.');
            if (!selected.size || selected.size > 8 * 1024 ** 3) throw new Error('Choose a nonempty LoRA file no larger than 8 GiB.');
            fileAfterUpload = await uploadFile(selected);
          }
          id = fileAfterUpload.id;
        }
        if (!id) throw new Error('Choose an installed LoRA.');
        const result = await libraryRequest(`/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(profile) });
        savedSelection = result.component?.id || id;
        source.value = 'installed'; componentSelect.value = ''; drafts.set(`installed:${savedSelection}`, rawDraft()); selectAfterLoad = true;
        fileAfterUpload = null; message.textContent = 'Profile saved. Refreshing workflow components…';
        await changed();
        if (!closed) message.textContent = 'LoRA profile saved. Select the LoRA in your workflow; its trigger and recommended settings remain explicit actions.';
      }
    } catch (reason) { if (!closed) showError(fileAfterUpload ? new Error(`File uploaded. ${reason.message} Retry saving this profile; the file will not be uploaded again.`) : reason); }
    finally { busy = false; xhr = null; if (!closed) { progress.hidden = true; await load(); renderSource(); } }
  };
  function onKey(event) {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled)')].filter(node => node.getClientRects().length);
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
    }
  }
  const close = bindMenuDismiss(overlay, () => { closed = true; xhr?.abort(); clearTimeout(timer); window.removeEventListener('keydown', onKey, true); overlay.remove(); if (anchor?.isConnected) anchor.focus(); }, () => false);
  closeButton.onclick = close; window.addEventListener('keydown', onKey, true);
  fill(loraProfileFromComponent()); renderSource(); load(); closeButton.focus();
  return { close };
}
