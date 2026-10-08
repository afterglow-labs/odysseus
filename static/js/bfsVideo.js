import { bindMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { showVideoWorkflowExportOptions } from './h3Video.js';
import * as videoWorkflow from './videoWorkflow.js';
import { createVideoBatchQueue } from './videoBatch.js';
import { createVideoJobEditor, videoJobEditFormData } from './videoJobEdit.js';
import { createVideoQueueControls, videoQueueField } from './videoQueue.js';
import { createVideoJobReruns } from './videoJobRerun.js';

const API = '/api/video/bfs';
const STORAGE = 'odysseus-bfs-video-settings-v1';
const ACTIVE = new Set(['queued', 'running']);
const TERMINAL = new Set(['completed', 'failed', 'stopped']);
const RESERVED = new Set(['workflow_id', 'components', 'gpu', 'prompt']);
const FAMILIES = { h3: 'MiniMax H3', ltx2: 'LTX-2', ltx23: 'LTX-2.3', ltx25: 'LTX-2.5', wan22: 'Wan 2.2' };
const el = (tag, className = '', text) => {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = text => { const node = el('button', 'memory-toolbar-btn', text); node.type = 'button'; return node; };
const optionId = value => String(value ?? '');

function jobOrder(a, b) {
  const rank = job => job.status === 'running' ? 0 : job.status === 'queued' ? 1 : 2;
  const group = rank(a) - rank(b);
  if (group) return group;
  if (a.status === 'queued' && a.queue_position > 0 && b.queue_position > 0) return a.queue_position - b.queue_position;
  const created = job => typeof job.created_at === 'number' ? job.created_at * 1000 : Date.parse(job.created_at) || 0;
  return rank(a) === 2 ? created(b) - created(a) : created(a) - created(b);
}

export function isBfsVideoModel(model) {
  return /(?:^|[/\\])BFS-Best-Face-Swap-Video$/i.test(model?.repo_id || '')
    || /^BFS-Best-Face-Swap-Video$/i.test(model?.name || '');
}

async function request(path, options = {}) {
  const response = await fetch(API + path, { credentials: 'same-origin', ...options });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw Object.assign(new Error(typeof (data.detail || data.error) === 'string' ? data.detail || data.error : `Request failed (${response.status}).`), { status: response.status });
  return data;
}

export function showBfsVideo({ preferred = null } = {}, anchor = document.activeElement) {
  document.querySelectorAll('.bfs-video-overlay').forEach(dismissOrRemove);
  for (const [id, href] of [['h3-video-styles', '/static/h3-video.css'], ['bfs-video-styles', '/static/bfs-video.css']]) {
    if (!document.getElementById(id)) {
      const link = el('link'); link.id = id; link.rel = 'stylesheet'; link.href = href; document.head.appendChild(link);
    }
  }
  let closed = false, inventory = null, workflow = null, loading = false, submitting = false, polling = false;
  let jobs = [], pollTimer, saved = {}, pendingPreferred = preferred, preferredAdapter = '', refreshFailed = false;
  let transferBusy = false, editRevision = 0, batchQueue = null, jobEditor = null, queueControls = null;
  const controls = new Map(), slots = new Map(), files = new Map(), drafts = new Map(), jobNodes = new Map(), stopping = new Set(), deleting = new Set(), deletedJobs = new Set();
  try { saved = JSON.parse(localStorage.getItem(STORAGE)) || {}; } catch {}
  const overlay = el('div', 'cookbook-edit-overlay bfs-video-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = el('section', 'cookbook-edit-modal h3-video-dialog bfs-video-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-labelledby', 'bfs-video-title');
  const header = el('div', 'h3-video-header');
  const title = el('h2', '', 'BFS Video Workflows'); title.id = 'bfs-video-title';
  const closeButton = button('Close'); header.append(title, closeButton); dialog.appendChild(header);
  const scroll = el('div', 'h3-video-scroll'); dialog.appendChild(scroll);
  scroll.appendChild(el('p', 'h3-video-target', `Local Odysseus server · ${window.location.host}`));
  scroll.appendChild(el('p', 'h3-video-description', 'Apply an identity image to a target video using a matching BFS workflow. Choose the workflow and its required models here; Odysseus runs the processing and saves the result.'));
  if (preferred?.adapter_path) scroll.appendChild(el('p', 'h3-video-muted bfs-video-origin', `Opened from adapter: ${preferred.adapter_path}`));
  const toolbar = el('div', 'h3-video-toolbar');
  const availability = el('p', 'h3-video-muted', 'Checking video workflows and cached models…'); availability.setAttribute('role', 'status');
  const refresh = button('Refresh components'); toolbar.append(availability, refresh); scroll.appendChild(toolbar);
  const transferBar = el('div', 'h3-video-toolbar h3-video-transfer-toolbar');
  const transferActions = el('div', 'h3-video-job-actions'), exportWorkflow = button('Export workflow'), importWorkflow = button('Import workflow');
  exportWorkflow.disabled = importWorkflow.disabled = true;
  const importFile = el('input'); importFile.type = 'file'; importFile.accept = '.json,.zip,application/json,application/zip'; importFile.hidden = true;
  const transferStatus = el('p', 'h3-video-muted'); transferStatus.setAttribute('role', 'status'); transferStatus.hidden = true;
  transferActions.append(exportWorkflow, importWorkflow, importFile); transferBar.append(transferActions, transferStatus); scroll.appendChild(transferBar);
  const error = el('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert'); scroll.appendChild(error);
  const form = el('form'); scroll.appendChild(form);
  function field(parent, name, label, control) {
    const wrap = el('label', 'h3-video-field'); wrap.appendChild(el('span', '', label));
    control.id = 'bfs-' + name; control.name = name; control.classList.add('cookbook-field-input'); wrap.appendChild(control); parent.appendChild(wrap);
    return control;
  }
  const workflowPicker = field(form, 'workflow', 'Workflow', el('select')); workflowPicker.disabled = true;
  const workflowNote = el('p', 'h3-video-muted'); form.appendChild(workflowNote);
  const adapterNotice = el('p', 'h3-video-error'); adapterNotice.hidden = true; form.appendChild(adapterNotice);
  const batchContainer = el('div'); batchContainer.hidden = true; form.appendChild(batchContainer);
  const inputGrid = el('div', 'bfs-video-inputs'); form.appendChild(inputGrid);
  function addUpload(key, label, accept, mediaType, help) {
    const wrap = el('section', 'h3-video-upload bfs-video-upload');
    const labelNode = el('label', 'h3-video-upload-label', label); labelNode.htmlFor = 'bfs-' + key;
    const input = el('input'); input.id = 'bfs-' + key; input.type = 'file'; input.accept = accept;
    const hint = el('p', 'h3-video-muted', help), name = el('p', 'h3-video-muted bfs-video-file-name');
    const preview = el(mediaType, 'bfs-video-preview'); preview.hidden = true;
    if (mediaType === 'video') { preview.controls = true; preview.playsInline = true; preview.preload = 'metadata'; }
    else preview.alt = label;
    const remove = button('Remove file'); remove.hidden = true; remove.setAttribute('aria-label', `Remove ${label.toLowerCase()}`);
    const state = { file: null, retained: null, url: null, input, wrap, preview, remove, name, labelNode, hint, label, help, allowed: false, required: false };
    function clear(clearInput = true) {
      editRevision++;
      if (state.url) URL.revokeObjectURL(state.url);
      state.url = null; state.file = null; state.retained = null; preview.removeAttribute('src'); preview.hidden = true; remove.hidden = true; name.textContent = '';
      if (clearInput) input.value = '';
      updateReady();
    }
    function setFile(selected) {
      clear(); if (!selected) return;
      state.file = selected; state.url = URL.createObjectURL(selected); preview.src = state.url;
      const transfer = new DataTransfer(); transfer.items.add(selected); input.files = transfer.files;
      preview.hidden = false; remove.hidden = false; name.textContent = `${selected.name} · ${(selected.size / 1048576).toFixed(1)} MB`;
      setError(''); updateReady();
    }
    state.clear = clear; state.setFile = setFile;
    state.setRetained = value => {
      clear(); if (!value) return;
      state.retained = value; remove.hidden = false;
      name.textContent = `${value.name} · ${(value.size / 1048576).toFixed(1)} MB · Saved on server`;
      updateReady();
    };
    input.addEventListener('change', () => { const selected = input.files?.[0]; if (selected) setFile(selected); });
    remove.onclick = () => clear(); wrap.append(labelNode, hint, input, name, preview, remove); inputGrid.appendChild(wrap); files.set(key, state);
  }
  addUpload('identity_image', 'Identity image', 'image/jpeg,image/png,image/webp', 'img', 'The face or identity to apply to the video.');
  addUpload('source_video', 'Target video', 'video/*', 'video', 'The video whose movement and background you want to keep.');
  addUpload('last_frame', 'Last frame', 'image/jpeg,image/png,image/webp', 'img', 'An ending frame for workflows that support it.');
  addUpload('mask_video', 'Mask video', 'video/*', 'video', 'The replacement region, aligned with the target video.');
  const sourceNote = el('p', 'h3-video-muted'); form.appendChild(sourceNote);
  const prompt = field(form, 'prompt', 'Prompt', el('textarea')); prompt.rows = 4; prompt.maxLength = 15000;
  prompt.placeholder = 'Describe the intended face replacement and any details to preserve…';
  const componentsTitle = el('h3', 'bfs-video-section-title', 'Workflow models'); form.appendChild(componentsTitle);
  const componentGrid = el('div', 'h3-video-grid'); form.appendChild(componentGrid);
  const missing = el('p', 'h3-video-error'); missing.setAttribute('role', 'status'); form.appendChild(missing);
  const parameterGrid = el('div', 'h3-video-grid h3-video-numbers'); form.appendChild(parameterGrid);
  const gpu = field(parameterGrid, 'gpu', 'GPU', el('select'));
  const dynamicControls = el('div', 'h3-video-grid h3-video-numbers'); form.appendChild(dynamicControls);
  const inputStatus = el('p', 'h3-video-muted'); inputStatus.setAttribute('role', 'status'); form.appendChild(inputStatus);
  const actions = el('div', 'h3-video-actions');
  const generate = button('Generate video'); generate.className = 'cookbook-btn'; generate.type = 'submit'; generate.disabled = true; actions.appendChild(generate); form.appendChild(actions);
  form.appendChild(el('p', 'h3-video-muted', 'Jobs render one at a time in submission order, shared with MiniMax H3 workflows. Keep editing this draft to add more jobs; each submission saves its own prompt, settings, and files.'));
  const submissionStatus = el('p', 'h3-video-muted h3-video-submission-status');
  submissionStatus.setAttribute('role', 'status'); submissionStatus.setAttribute('aria-live', 'polite'); submissionStatus.hidden = true; form.appendChild(submissionStatus);
  const jobsHeader = el('div', 'h3-video-toolbar'); jobsHeader.appendChild(el('h3', '', 'Recent BFS jobs'));
  const refreshJobs = button('Refresh jobs'); jobsHeader.appendChild(refreshJobs); scroll.appendChild(jobsHeader);
  const jobsStatus = el('p', 'h3-video-muted', 'Loading jobs…'); jobsStatus.setAttribute('role', 'status'); scroll.appendChild(jobsStatus);
  const queueContainer = el('div'); scroll.appendChild(queueContainer);
  const jobList = el('div', 'h3-video-jobs'); scroll.appendChild(jobList);
  overlay.appendChild(dialog); document.body.appendChild(overlay);
  const reruns = createVideoJobReruns({ family: 'bfs', request, isClosed: () => closed,
    isBlocked: () => !!queueControls?.busy || !!queueControls?.editing, onChange: () => renderJobs(),
    onQueued: async result => { queueControls?.update(result.queue); await loadJobs(); },
  });

  function setError(message) { error.textContent = message || ''; error.hidden = !message; if (message) error.scrollIntoView({ block: 'nearest' }); }
  function config() {
    const values = { workflow_id: workflow?.id || '', components: {}, gpu: gpu.value, prompt: prompt.value };
    // Persist explicit missing/None selections as empty values. Omitting a key
    // would let a refresh replace an unresolved import with the slot default.
    for (const [key, slot] of slots) values.components[key] = slot.control.value;
    for (const [key, state] of controls) values[key] = state.type === 'boolean' ? state.control.checked
      : state.numeric ? Number(state.control.value) : state.control.value;
    return values;
  }
  function save() {
    if (!workflow || jobEditor?.active) return;
    const { prompt: draft, ...values } = config(); drafts.set(workflow.id, draft);
    saved = { ...saved, workflow_id: workflow.id, by_workflow: { ...(saved.by_workflow || {}), [workflow.id]: values } };
    try { localStorage.setItem(STORAGE, JSON.stringify(saved)); } catch {}
  }
  function componentIssues() {
    const issues = [];
    for (const state of slots.values()) {
      if (state.required && !state.control.value) issues.push(state.label);
      else if (state.control.value && !state.ids.has(state.control.value)) issues.push(`${state.label} (selected file unavailable)`);
    }
    return issues;
  }
  const batchActive = () => !jobEditor?.active && !!workflow && files.get('source_video').allowed && !!batchQueue?.active;
  function batchIssue() {
    if (jobEditor?.active || jobEditor?.loading) return 'Finish editing the queued job before adding a batch.';
    if (inventory && inventory.batch_jobs !== true) return 'Batch jobs need the updated Odysseus server. Restart the server after active transfers finish.';
    if (queueControls?.busy || transferBusy || loading || submitting) return 'Wait for the current operation to finish.';
    if (refreshFailed || !inventory?.runtime_ready) return inventory?.runtime_error || 'The BFS runtime is not ready.';
    if (!workflow?.ready || !files.get('source_video').allowed) return workflow?.unavailable_reason || 'Choose a ready workflow with a target video input.';
    if (componentIssues().length) return 'Choose the required workflow models.';
    if (!inventory.gpus?.some(item => optionId(item.id) === gpu.value)) return 'Choose an available GPU.';
    const primary = inventory.gpus.find(item => optionId(item.id) === gpu.value);
    if (!primary.nvfp4 && [...slots.values()].some(slot => inventory.components?.some(item => optionId(item.id) === slot.control.value && item.nvfp4))) return 'The selected NVFP4 components require a Blackwell GPU, such as the RTX 5090.';
    const needed = [...files].filter(([key, state]) => key !== 'source_video' && state.allowed && state.required && !state.file);
    if (needed.length) return `Add ${needed.map(([, state]) => state.labelNode.textContent.toLowerCase()).join(' and ')} for every video in the batch.`;
    if (workflow.id === 'ltx2_v1') {
      const adapter = inventory.components?.find(item => optionId(item.id) === slots.get('lora')?.control.value);
      const wantsLast = /8750/.test(adapter?.path || adapter?.name || '');
      if (wantsLast !== !!files.get('last_frame').file) return wantsLast ? 'Add a last frame for the selected first-and-last-frame adapter.' : 'Remove the last frame for the selected first-frame-only adapter.';
    }
    const values = config();
    if (values.width * values.height > 1032192) return 'Choose an output size of at most 1,032,192 pixels.';
    if (!form.checkValidity()) return 'Check the workflow settings before adding the batch.';
    return '';
  }
  function batchSnapshot() {
    form.reportValidity();
    const issue = batchIssue(); if (issue) throw new Error(issue);
    save(); setError('');
    return {
      config: config(), videoField: 'source_video',
      uploads: Object.fromEntries([...files].filter(([key, state]) => key !== 'source_video' && state.allowed && state.file)
        .map(([key, state]) => [key, [state.file]])),
    };
  }
  function updateReady() {
    jobEditor?.observe(jobs);
    for (const job of jobs) {
      const nodes = jobNodes.get(job.id); if (!nodes) continue;
      const state = reruns.state(job); nodes.rerun.disabled = state.disabled;
      nodes.editRerun.disabled = state.disabled || state.pending || !inventory || !!jobEditor?.active || !!jobEditor?.loading;
    }
    const batching = batchActive(), batchBusy = !!batchQueue?.busy;
    const editing = !!jobEditor?.active, editLoading = !!jobEditor?.loading;
    const queueBusy = !!queueControls?.busy;
    const issues = componentIssues();
    missing.textContent = issues.length ? `Missing or incompatible components: ${issues.join(', ')}.` : '';
    missing.hidden = !issues.length;
    const neededFiles = [...files].filter(([key, state]) => !(batching && key === 'source_video') && state.allowed && state.required && !state.file && !state.retained).map(([, state]) => state);
    const missingInput = neededFiles.length > 0;
    inputStatus.textContent = workflow && missingInput ? `Add ${neededFiles.map(state => state.labelNode.textContent.toLowerCase()).join(' and ')} to generate.` : '';
    generate.hidden = batching;
    files.get('source_video').wrap.hidden = !files.get('source_video').allowed || batching;
    workflowPicker.disabled = batchBusy || loading || !inventory?.workflows?.length;
    refresh.disabled = editing || editLoading || batchBusy || loading;
    generate.disabled = queueBusy || batching || batchBusy || editLoading || !!jobEditor?.blocked || transferBusy || loading || submitting || refreshFailed || !inventory?.runtime_ready || !workflow?.ready || issues.length > 0 || !gpu.value || missingInput;
    exportWorkflow.disabled = queueBusy || editing || editLoading || batchBusy || transferBusy || loading || submitting || !workflow;
    importWorkflow.disabled = queueBusy || editing || editLoading || batchBusy || transferBusy || loading || submitting || !inventory;
    importWorkflow.textContent = transferBusy ? 'Importing workflow…' : 'Import workflow';
    generate.textContent = submitting ? editing ? 'Saving changes…' : 'Uploading and adding job…' : editing ? 'Save changes' : jobs.some(job => ACTIVE.has(job.status)) ? 'Add to queue' : 'Generate video';
    const issue = batchIssue();
    const snapshotReason = editing || editLoading ? 'Finish editing the queued job before adding a batch.' : inventory && inventory.batch_jobs !== true ? 'Batch jobs need the updated Odysseus server. Restart the server after active transfers finish.'
      : queueBusy || transferBusy || loading || submitting ? 'Wait for the current operation to finish.'
      : refreshFailed || !inventory?.runtime_ready ? inventory?.runtime_error || 'The BFS runtime is not ready.' : '';
    batchQueue?.setEnabled(!issue, issue, { snapshotEnabled: !snapshotReason, snapshotReason });
    batchQueue?.setAvailable(!editing && !!workflow && files.get('source_video').allowed);
    batchContainer.hidden = editing || !workflow || !files.get('source_video').allowed;
    for (const [id, nodes] of jobNodes) nodes.edit.disabled = queueBusy || !!queueControls?.editing || editing || editLoading || batchBusy || transferBusy || loading || submitting || !inventory || stopping.has(id);
    queueControls?.setBlocked(editing || editLoading || batchBusy || transferBusy || loading || submitting || stopping.size > 0 || deleting.size > 0);
  }
  function sourceRequirements() {
    const req = workflow?.source_requirements || {};
    for (const [key, state] of files) {
      const value = req[key], primary = key === 'identity_image' || key === 'source_video';
      state.allowed = primary ? value !== false && value?.allowed !== false : value === true || value?.allowed === true || value?.required === true;
      state.required = state.allowed && (primary ? value?.required !== false : value === true || value?.required === true);
      state.wrap.hidden = !state.allowed; state.input.disabled = !state.allowed;
      const preparedFrame = key === 'identity_image' && workflow?.id === 'ltx2_v1';
      state.labelNode.textContent = value?.label || (preparedFrame ? 'Prepared first frame' : state.label);
      state.hint.textContent = value?.description || (preparedFrame ? 'A first frame already edited with the desired face. This workflow continues that identity through the video.' : state.help);
    }
    const notes = Array.isArray(req.notes) ? req.notes : req.notes ? [req.notes] : [];
    sourceNote.textContent = notes.join(' ');
    batchQueue?.setAvailable(!!workflow && files.get('source_video').allowed);
    batchContainer.hidden = !workflow || !files.get('source_video').allowed;
  }
  function selectWorkflow(id, keepDraft = true) {
    if (keepDraft && workflow) save();
    workflow = (inventory?.workflows || []).find(item => item.id === id) || null;
    workflowPicker.value = workflow?.id || '';
    slots.clear(); controls.clear(); componentGrid.replaceChildren(); dynamicControls.replaceChildren();
    adapterNotice.hidden = true;
    const previous = saved.by_workflow?.[workflow?.id] || {};
    const defaults = workflow?.defaults || {};
    prompt.value = workflow ? drafts.get(workflow.id) ?? defaults.prompt ?? '' : '';
    if (!workflow) {
      workflowNote.textContent = 'Choose a workflow to see its inputs and required models.';
      for (const state of files.values()) { state.allowed = false; state.wrap.hidden = true; }
      sourceNote.textContent = ''; batchQueue?.setAvailable(false); batchContainer.hidden = true; updateReady(); return;
    }
    const familyLabel = FAMILIES[workflow.family] || workflow.family;
    workflowNote.textContent = workflow.ready ? `${familyLabel} · Workflow components ready.` : workflow.unavailable_reason || `${familyLabel} · Required models or runtime are not ready.`;
    workflowNote.classList.toggle('h3-video-error', !workflow.ready);
    const available = inventory.components || [];
    const incompatibleAdapter = preferredAdapter && !workflow.adapter_paths?.includes(preferredAdapter);
    if (incompatibleAdapter) {
      adapterNotice.textContent = 'The adapter you opened does not match this workflow. Choose a compatible head-swap adapter below.';
      adapterNotice.hidden = false;
    }
    for (const slot of workflow.slots || []) {
      const ids = new Set((slot.component_ids || []).map(optionId));
      const candidates = available.filter(item => ids.has(optionId(item.id)));
      const control = field(componentGrid, 'component-' + slot.key, slot.label || slot.key, el('select'));
      control.add(new Option(slot.required ? 'Choose component…' : 'None', ''));
      candidates.forEach(item => control.add(new Option(item.name, optionId(item.id))));
      // A deliberate adapter-file choice may override its slot. Match the
      // repository-relative file, never a shared cache directory or basename.
      const selectedAdapter = preferredAdapter && candidates.find(item => String(item.path || '').replaceAll('\\', '/').endsWith('/' + preferredAdapter));
      const wanted = incompatibleAdapter && slot.key === 'lora' ? '' : optionId(selectedAdapter?.id ?? previous.components?.[slot.key] ?? slot.default_id);
      if (wanted && !candidates.some(item => optionId(item.id) === wanted)) control.add(new Option('Previously selected component is unavailable', wanted));
      if ([...control.options].some(option => option.value === wanted)) control.value = wanted;
      slots.set(slot.key, { control, ids: new Set(candidates.map(item => optionId(item.id))), required: !!slot.required, label: slot.label || slot.key });
    }
    for (const spec of workflow.controls || []) {
      if (!/^[a-z][a-z0-9_]*$/.test(spec.key) || RESERVED.has(spec.key)) continue;
      const type = spec.type || 'number', numeric = ['number', 'integer', 'int', 'float'].includes(type);
      let control;
      if (Array.isArray(spec.options) && spec.options.length) {
        control = el('select');
        for (const entry of spec.options) {
          const value = Array.isArray(entry) ? entry[0] : typeof entry === 'object' ? entry.value : entry;
          const label = Array.isArray(entry) ? entry[1] : typeof entry === 'object' ? entry.label ?? entry.value : entry;
          control.add(new Option(String(label), optionId(value)));
        }
      } else {
        control = el('input'); control.type = type === 'boolean' ? 'checkbox' : numeric ? 'number' : 'text';
        if (numeric) {
          if (spec.min !== undefined) control.min = spec.min;
          if (spec.max !== undefined) control.max = spec.max;
          // The API enforces integer grids (dimensions/frames), while decimal
          // settings are continuous within their advertised range.
          control.step = Number(spec.step) >= 1 ? spec.step : 'any'; control.required = true;
        }
      }
      field(dynamicControls, 'control-' + spec.key, spec.label || spec.key, control);
      const value = previous[spec.key] ?? defaults[spec.key] ?? spec.default;
      if (type === 'boolean') control.checked = !!value;
      else if (value !== undefined) control.value = String(value);
      controls.set(spec.key, { control, type, numeric });
    }
    const selectedGpu = optionId(previous.gpu ?? defaults.gpu ?? inventory.defaults?.gpu);
    if ([...gpu.options].some(option => option.value === selectedGpu)) gpu.value = selectedGpu;
    preferredAdapter = '';
    sourceRequirements(); updateReady();
  }
  async function loadInventory(providedInventory = null) {
    if (loading) return;
    if (workflow) save();
    const current = workflow?.id || saved.workflow_id;
    loading = true; refresh.disabled = true; availability.textContent = 'Checking video workflows and cached models…'; updateReady();
    try {
      const data = providedInventory || await request('/inventory'); if (closed) return;
      inventory = data; refreshFailed = false; workflowPicker.replaceChildren(new Option('Choose a BFS workflow…', ''));
      const groups = new Map();
      for (const item of data.workflows || []) {
        const family = item.family || 'Other workflows';
        if (!groups.has(family)) { const group = el('optgroup'); group.label = FAMILIES[family] || family; groups.set(family, group); workflowPicker.appendChild(group); }
        groups.get(family).appendChild(new Option(`${item.label}${item.ready ? '' : ' — needs setup'}`, item.id));
      }
      const oldGpu = gpu.value; gpu.replaceChildren();
      for (const item of data.gpus || []) gpu.add(new Option(item.name, optionId(item.id)));
      if (!gpu.options.length) gpu.add(new Option('No available GPU', ''));
      if ([...gpu.options].some(option => option.value === oldGpu)) gpu.value = oldGpu;
      let chosen = current;
      if (pendingPreferred) {
        const exact = (data.workflows || []).filter(item => item.id === pendingPreferred.workflow_id
          || (pendingPreferred.workflow_path && item.source_workflow === pendingPreferred.workflow_path));
        const adapter = (data.workflows || []).filter(item => pendingPreferred.adapter_path && item.adapter_paths?.includes(pendingPreferred.adapter_path));
        const family = (data.workflows || []).filter(item => item.family === pendingPreferred.family || FAMILIES[item.family] === pendingPreferred.family);
        chosen = exact.length === 1 ? exact[0].id : adapter.length === 1 ? adapter[0].id : family.length === 1 ? family[0].id : '';
        preferredAdapter = pendingPreferred.adapter_path || '';
        pendingPreferred = null;
      }
      workflowPicker.disabled = !(data.workflows || []).length;
      availability.textContent = !data.runtime_ready ? `Runtime unavailable: ${data.runtime_error || 'BFS runtime is not ready.'}`
        : data.workflows?.length ? 'Choose a workflow; readiness is checked for its required files.' : 'No supported BFS workflows were found.';
      availability.classList.toggle('h3-video-error', !data.runtime_ready);
      selectWorkflow(chosen, false);
    } catch (e) { if (!closed) { refreshFailed = true; availability.textContent = e.message; availability.classList.add('h3-video-error'); } }
    finally { if (!closed) { loading = false; refresh.disabled = false; updateReady(); } }
  }
  function renderJobs() {
    const running = jobs.filter(job => job.status === 'running').length, queued = jobs.filter(job => job.status === 'queued').length;
    jobsStatus.textContent = jobs.length ? `${running} rendering · ${queued} queued here. Jobs and their prompts are saved on the server. Closing this panel does not change the queue.` : 'No BFS video jobs yet.';
    const ids = new Set(jobs.map(job => job.id));
    for (const [id, nodes] of jobNodes) if (!ids.has(id)) { nodes.card.remove(); jobNodes.delete(id); }
    for (const [index, job] of [...jobs].sort(jobOrder).entries()) {
      let nodes = jobNodes.get(job.id);
      if (!nodes) {
        const card = el('article', 'h3-video-job'); card.dataset.jobId = job.id;
        const row = el('div', 'h3-video-toolbar'), heading = el('strong'), stop = button('Stop'), remove = button('Delete job'), exportJob = button('Export workflow'), edit = button('Edit'), rerun = button('Retry'), editRerun = button('Edit & rerun');
        const jobActions = el('div', 'h3-video-job-actions'); jobActions.append(edit, rerun, editRerun, exportJob, stop, remove); row.append(heading, jobActions);
        const status = el('p', 'h3-video-muted'); status.setAttribute('role', 'status');
        const progress = el('progress'); progress.setAttribute('aria-label', 'BFS video progress');
        const failure = el('p', 'h3-video-error'); failure.setAttribute('role', 'alert');
        const output = el('div', 'h3-video-output');
        const promptDetails = el('details', 'h3-video-job-prompt'); promptDetails.appendChild(el('summary', '', 'Prompt used'));
        const usedPrompt = el('pre', 'h3-video-prompt-used'), copy = button('Copy prompt'), copied = el('p', 'h3-video-muted'); copied.setAttribute('role', 'status');
        promptDetails.append(usedPrompt, copy, copied);
        copy.onclick = async () => {
          try { if (!navigator.clipboard?.writeText) throw new Error(); await navigator.clipboard.writeText(usedPrompt.textContent); if (!closed) copied.textContent = 'Prompt copied.'; }
          catch { if (!closed) copied.textContent = 'Copy is unavailable here. Select the prompt text to copy it.'; }
        };
        const details = el('details'); details.appendChild(el('summary', '', 'Runtime log'));
        const log = el('pre', 'h3-video-log'); details.appendChild(log);
        const rerunStatus = el('p', 'h3-video-muted'); rerunStatus.hidden = true; rerunStatus.setAttribute('role', 'status');
        const rerunError = el('p', 'h3-video-error'); rerunError.hidden = true; rerunError.setAttribute('role', 'alert');
        const deletion = el('div', 'h3-video-delete-confirmation'); deletion.hidden = true;
        deletion.appendChild(el('p', '', 'Permanently delete this job, its output, uploads, logs and saved prompt, including its linked Gallery output? This cannot be undone.'));
        const deleteActions = el('div', 'h3-video-job-actions'), keep = button('Keep job'), confirmDelete = button('Delete permanently');
        confirmDelete.classList.add('h3-video-delete-button'); deleteActions.append(keep, confirmDelete); deletion.appendChild(deleteActions);
        const deleteError = el('p', 'h3-video-error'); deleteError.hidden = true; deleteError.setAttribute('role', 'alert'); deletion.appendChild(deleteError);
        card.append(row, status, rerunStatus, rerunError, deletion, progress, failure, output, promptDetails, details); jobList.appendChild(card);
        nodes = { card, heading, edit, rerun, editRerun, rerunStatus, rerunError, stop, remove, deletion, keep, confirmDelete, deleteError, status, progress, failure, output, usedPrompt, copy, log }; jobNodes.set(job.id, nodes);
        edit.onclick = () => jobEditor.open(job.id);
        rerun.title = 'Queue a new copy using this job’s saved prompt, settings and inputs; keep the original.';
        rerun.onclick = () => reruns.run(jobs.find(item => item.id === job.id) || job);
        editRerun.onclick = () => queueControls.openRerun(job.id, async (source, patch) => {
          const result = await reruns.run(source, patch, true);
          return { result, ...reruns.state(source) };
        });
        stop.onclick = () => stopJob(job.id);
        remove.onclick = () => { deletion.hidden = false; deleteError.hidden = true; keep.focus(); };
        keep.onclick = () => { deletion.hidden = true; remove.focus(); };
        confirmDelete.onclick = () => deleteJob(job.id);
        exportJob.onclick = () => showVideoWorkflowExportOptions(options => videoWorkflow.downloadJobWorkflow('bfs', job.id, options), exportJob);
      }
      if (jobList.children[index] !== nodes.card) jobList.insertBefore(nodes.card, jobList.children[index] || null);
      const queued = job.status === 'queued';
      nodes.edit.hidden = !queued;
      const rerunState = reruns.state(job);
      nodes.rerun.hidden = nodes.editRerun.hidden = rerunState.hidden; nodes.rerun.disabled = rerunState.disabled; nodes.rerun.textContent = rerunState.label;
      nodes.editRerun.disabled = rerunState.disabled || rerunState.pending || !inventory || !!jobEditor?.active || !!jobEditor?.loading;
      nodes.rerunStatus.textContent = rerunState.message; nodes.rerunStatus.hidden = !rerunState.message;
      nodes.rerunError.textContent = rerunState.error; nodes.rerunError.hidden = !rerunState.error;
      const position = queued && Number.isInteger(job.queue_position) && job.queue_position > 0 ? ` · Position ${job.queue_position}` : '';
      nodes.heading.textContent = `${job.source_name ? `${job.source_name} · ` : ''}${job.workflow_label || 'BFS video'} · ${job.status}${position}`;
      const started = queued ? job.created_at : job.started_at || job.created_at;
      let start = typeof started === 'number' ? started * 1000 : Date.parse(started);
      const end = job.finished_at ? (typeof job.finished_at === 'number' ? job.finished_at * 1000 : Date.parse(job.finished_at)) : Date.now();
      const seconds = Number.isFinite(start) ? Math.max(0, Math.floor((end - start) / 1000)) : null;
      nodes.status.textContent = [queued ? 'Waiting for its turn in the shared video queue' : job.phase, !queued && job.total_steps ? `Step ${job.step || 0} / ${job.total_steps}` : '', seconds !== null ? `${Math.floor(seconds / 60)}m ${seconds % 60}s` : '', job.id].filter(Boolean).join(' · ');
      nodes.progress.hidden = job.status !== 'running';
      if (job.total_steps) { nodes.progress.max = job.total_steps; nodes.progress.value = job.step || 0; } else nodes.progress.removeAttribute('value');
      nodes.failure.textContent = job.error || ''; nodes.failure.hidden = !job.error;
      nodes.stop.hidden = !ACTIVE.has(job.status); nodes.stop.disabled = !!queueControls?.busy || stopping.has(job.id);
      nodes.stop.textContent = stopping.has(job.id) ? (queued ? 'Canceling…' : 'Stopping and verifying…') : queued ? 'Cancel queued job' : 'Stop';
      nodes.remove.hidden = !TERMINAL.has(job.status); nodes.remove.disabled = !!queueControls?.busy || reruns.busy(job.id) || deleting.has(job.id);
      nodes.keep.disabled = !!queueControls?.busy || reruns.busy(job.id) || deleting.has(job.id); nodes.confirmDelete.disabled = !!queueControls?.busy || reruns.busy(job.id) || deleting.has(job.id);
      nodes.confirmDelete.textContent = deleting.has(job.id) ? 'Deleting…' : 'Delete permanently';
      if (!TERMINAL.has(job.status)) nodes.deletion.hidden = true;
      const used = typeof job.prompt === 'string' && job.prompt.length ? job.prompt : 'Prompt unavailable for this older job.';
      if (nodes.usedPrompt.textContent !== used) nodes.usedPrompt.textContent = used;
      nodes.copy.hidden = !(typeof job.prompt === 'string' && job.prompt.length);
      nodes.log.textContent = Array.isArray(job.log_tail) ? job.log_tail.join('\n') : job.log_tail || 'No runtime output yet.';
      if (job.status === 'completed' && !nodes.output.childElementCount) {
        const url = `${API}/jobs/${encodeURIComponent(job.id)}/video`;
        const video = el('video'); video.src = url; video.controls = true; video.playsInline = true; video.preload = 'metadata';
        const download = el('a', 'memory-toolbar-btn', 'Download MP4'); download.href = url; download.download = job.filename || 'odysseus-bfs.mp4';
        nodes.output.append(video, download);
      }
    }
    updateReady();
  }
  async function loadJobs() {
    if (closed || polling) return; polling = true; refreshJobs.disabled = true;
    try { const data = await request('/jobs'); if (!closed) { queueControls?.update(data.queue); jobs = Array.isArray(data.jobs) ? data.jobs.filter(job => !deletedJobs.has(job.id)) : []; renderJobs(); } }
    catch (e) { if (!closed) jobsStatus.textContent = `Could not refresh jobs: ${e.message}`; }
    finally { polling = false; if (!closed) refreshJobs.disabled = false; }
  }
  async function stopJob(id) {
    if (queueControls?.busy || stopping.has(id)) return; stopping.add(id); setError(''); renderJobs();
    try {
      const result = await request(`/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' }); if (closed) return;
      const stopped = result.job || result;
      if (!['stopped', 'completed', 'failed'].includes(stopped.status)) throw new Error('The server has not confirmed that this job stopped. Refresh and retry.');
      jobs = jobs.map(job => job.id === id ? stopped : job);
    } catch (e) { if (!closed) setError(`Could not stop BFS video: ${e.message}`); }
    finally { stopping.delete(id); if (!closed) { renderJobs(); await loadJobs(); } }
  }
  async function deleteJob(id) {
    const nodes = jobNodes.get(id);
    if (queueControls?.busy || !nodes || deleting.has(id) || !TERMINAL.has(jobs.find(job => job.id === id)?.status)) return;
    deleting.add(id); nodes.deleteError.hidden = true; renderJobs();
    try {
      const result = await request(`/jobs/${encodeURIComponent(id)}/record`, { method: 'DELETE' }); if (closed) return;
      if (result.deleted !== true) throw new Error('The server has not confirmed deletion. Refresh jobs and retry.');
      deletedJobs.add(id); jobs = jobs.filter(job => job.id !== id); renderJobs(); refreshJobs.focus();
    } catch (e) {
      if (!closed) { nodes.deleteError.textContent = `Could not delete job: ${e.message}`; nodes.deleteError.hidden = false; }
    } finally { deleting.delete(id); if (!closed) renderJobs(); }
  }
  exportWorkflow.onclick = () => {
    const snapshot = config(), currentInventory = inventory;
    const attached = Object.fromEntries([...files].filter(([, state]) => state.allowed && state.file).map(([key, state]) => [key, [state.file]]));
    showVideoWorkflowExportOptions(options => videoWorkflow.exportVideoWorkflow({ family: 'bfs', config: snapshot, inventory: currentInventory, uploads: attached, ...options }), exportWorkflow);
  };
  importWorkflow.onclick = () => importFile.click();
  importFile.onchange = async () => {
    const file = importFile.files?.[0]; importFile.value = ''; if (!file || transferBusy) return;
    const revision = editRevision, localGpu = gpu.value;
    transferBusy = true; transferStatus.hidden = false; transferStatus.classList.remove('h3-video-error');
    transferStatus.textContent = 'Importing workflow… Large bundles may take time.'; updateReady();
    try {
      const { importVideoWorkflow } = await import('./videoWorkflow.js');
      const result = await importVideoWorkflow(file, { family: 'bfs', inventory, onProgress: ({ phase, loaded, total }) => {
        if (closed || !transferBusy) return;
        transferStatus.textContent = phase === 'installing' ? 'Upload complete; checking and installing workflow files…'
          : total > 0 ? `Uploading workflow… ${Math.min(100, Math.round(loaded / total * 100))}% · ${(loaded / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MB`
            : 'Uploading workflow…';
      } }); if (closed) return;
      if (revision !== editRevision) throw new Error('The draft changed while importing. Import again to apply this workflow.');
      if (result.inventory) await loadInventory(result.inventory);
      if (closed) return;
      if (revision !== editRevision) throw new Error('The draft changed while importing. Import again to apply this workflow.');
      const imported = { ...result.config, gpu: localGpu };
      saved = { ...saved, workflow_id: imported.workflow_id, by_workflow: { ...(saved.by_workflow || {}), [imported.workflow_id]: imported } };
      drafts.set(imported.workflow_id, imported.prompt || ''); selectWorkflow(imported.workflow_id, false);
      for (const [key, state] of files) {
        state.clear(); if (state.allowed && result.uploads?.[key]?.[0]) state.setFile(result.uploads[key][0]);
      }
      editRevision++; save();
      transferStatus.textContent = ['Workflow imported into this draft. Review settings and inputs before generating.', ...(result.warnings || [])].join(' ');
    } catch (e) { if (!closed) { transferStatus.textContent = e.message || 'Could not import this workflow.'; transferStatus.classList.add('h3-video-error'); } }
    finally { transferBusy = false; if (!closed) updateReady(); }
  };
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (batchActive() || generate.disabled || !form.reportValidity()) return;
    const values = config();
    if (values.width * values.height > 1032192) { setError('Choose an output size of at most 1,032,192 pixels (for example, 960 × 544).'); return; }
    setError(''); save();
    const editing = jobEditor.active;
    const body = editing ? videoJobEditFormData(editing, values,
      Object.fromEntries([...files].filter(([, state]) => state.allowed && state.file).map(([key, state]) => [key, [state.file]])),
      Object.fromEntries([...files].filter(([, state]) => state.allowed && state.retained).map(([key, state]) => [key, [state.retained]]))) : new FormData();
    if (!editing) {
      body.append('config', JSON.stringify(values));
      for (const [key, state] of files) if (state.allowed && state.file) body.append(key, state.file, state.file.name);
    }
    submitting = true; submissionStatus.hidden = true; updateReady();
    try {
      const result = await request(editing ? `/jobs/${encodeURIComponent(editing.id)}` : '/jobs', { method: editing ? 'PATCH' : 'POST', body }); if (closed) return;
      const job = result.job || result;
      if (editing) jobEditor.finish();
      jobs = [job, ...jobs.filter(row => row.id !== job.id)]; renderJobs();
      submissionStatus.textContent = editing ? `Changes saved to job ${job.id}. Its place in the queue is unchanged. Your previous draft has been restored.` : `Job ${job.id} ${job.status === 'queued' ? 'added to the queue' : 'accepted'}. Its prompt, settings, and files are saved. This draft is ready to edit and submit again.`;
      submissionStatus.hidden = false; submissionStatus.scrollIntoView({ block: 'nearest' });
    } catch (e) { if (!closed) { if (editing) jobEditor.failed(e); else setError(e.message); } }
    finally { submitting = false; if (!closed) updateReady(); }
  });
  workflowPicker.onchange = () => selectWorkflow(workflowPicker.value);
  form.addEventListener('input', () => { editRevision++; updateReady(); });
  form.addEventListener('change', () => { editRevision++; });
  form.addEventListener('change', () => { save(); updateReady(); });
  refresh.onclick = () => loadInventory(); refreshJobs.onclick = loadJobs;
  const onKey = event => {
    if (document.querySelector('.video-workflow-export-overlay')) return;
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation();
      const confirming = [...jobNodes.values()].find(nodes => !nodes.deletion.hidden);
      if (confirming) { if (!confirming.keep.disabled) { confirming.deletion.hidden = true; confirming.remove.focus(); } }
      // Keep the draft (including selected files and batch state) open until
      // Close is clicked. Do not pass Escape to the parent Cookbook window.
    }
    else if (event.key === 'Tab') {
      const focusable = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,a[href]')].filter(node => node.getClientRects().length);
      if (event.shiftKey && document.activeElement === focusable[0]) { event.preventDefault(); focusable.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === focusable.at(-1)) { event.preventDefault(); focusable[0]?.focus(); }
    }
  };
  // A selection drag can synthesize a click on the backdrop. Only the Close
  // button (or explicit programmatic teardown) may discard this live draft.
  const close = bindMenuDismiss(overlay, () => {
    closed = true; clearInterval(pollTimer); window.removeEventListener('keydown', onKey, true);
    batchQueue?.destroy(); queueControls?.destroy();
    for (const state of files.values()) if (state.url) URL.revokeObjectURL(state.url);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, () => false);
  closeButton.onclick = close; window.addEventListener('keydown', onKey, true); closeButton.focus();
  for (const state of files.values()) state.wrap.hidden = true;
  function applyEditorDraft(values, attached, retained = {}) {
    selectWorkflow(values.workflow_id, false);
    if (!workflow && values.workflow_id) throw new Error('This job’s workflow is unavailable. Refresh components before editing it.');
    const assign = (control, value) => {
      if (value === undefined) return;
      value = String(value);
      if (control.tagName === 'SELECT' && value && ![...control.options].some(option => option.value === value)) control.add(new Option('Previously selected value is unavailable', value));
      control.value = value;
    };
    prompt.value = values.prompt || ''; assign(gpu, values.gpu);
    for (const [key, slot] of slots) assign(slot.control, values.components?.[key]);
    for (const [key, state] of controls) {
      if (state.type === 'boolean') state.control.checked = !!values[key];
      else assign(state.control, values[key]);
    }
    for (const [key, state] of files) {
      state.clear();
      if (attached[key]) state.setFile(attached[key]);
      else if (retained[key]?.[0]) state.setRetained(retained[key][0]);
    }
    editRevision++; if (workflow) sourceRequirements(); updateReady();
  }
  jobEditor = createVideoJobEditor({
    form, request, isClosed: () => closed,
    isBusy: () => !!queueControls?.busy || !!queueControls?.editing || submitting || loading || transferBusy || !!batchQueue?.busy,
    getDraft: () => ({ config: config(), uploads: Object.fromEntries([...files].map(([key, state]) => [key, state.file])) }),
    applyEdit: value => { submissionStatus.hidden = true; applyEditorDraft(value.config, {}, value.inputs); },
    restoreDraft: value => applyEditorDraft(value.config, value.uploads),
    onChange: updateReady, onError: setError,
  });
  batchQueue = createVideoBatchQueue({
    family: 'bfs', container: batchContainer, getSnapshot: batchSnapshot,
    onJob: job => { if (!closed) { jobs = [job, ...jobs.filter(item => item.id !== job.id)]; renderJobs(); } },
    onChange: () => { if (!closed) { editRevision++; updateReady(); } },
  });
  queueControls = createVideoQueueControls({
    container: queueContainer, family: 'bfs', request, reload: loadJobs, onChange: updateReady, isClosed: () => closed,
    getFields: savedConfig => {
      const descriptors = [videoQueueField('prompt', 'Prompt', prompt), videoQueueField('gpu', 'GPU', gpu)];
      const definitions = new Map(), componentDefinitions = new Map();
      // Include every installed BFS family; a draft selection must not hide
      // parameters used by jobs from another workflow in the same queue.
      for (const candidate of inventory?.workflows || []) {
        if (savedConfig?.workflow_id && candidate.id !== savedConfig.workflow_id) continue;
        for (const spec of candidate.controls || []) if (!RESERVED.has(spec.key) && !definitions.has(spec.key)) definitions.set(spec.key, { ...spec, default: spec.default ?? candidate.defaults?.[spec.key] });
        for (const slot of candidate.slots || []) {
          const previous = componentDefinitions.get(slot.key);
          if (previous) slot.component_ids?.forEach(id => previous.ids.add(optionId(id)));
          else componentDefinitions.set(slot.key, { label: slot.label || slot.key, ids: new Set((slot.component_ids || []).map(optionId)), default_id: slot.default_id });
        }
      }
      for (const [key, spec] of definitions) {
        const current = savedConfig?.workflow_id && savedConfig.workflow_id !== workflow?.id ? null : controls.get(key), type = spec.type || 'number', numeric = ['number', 'integer', 'int', 'float'].includes(type);
        let control = current?.control;
        if (!control) {
          control = el(Array.isArray(spec.options) && spec.options.length ? 'select' : 'input');
          if (control.tagName === 'SELECT') for (const entry of spec.options) {
            const value = Array.isArray(entry) ? entry[0] : typeof entry === 'object' ? entry.value : entry;
            const label = Array.isArray(entry) ? entry[1] : typeof entry === 'object' ? entry.label ?? entry.value : entry;
            control.add(new Option(String(label), optionId(value)));
          }
          else {
            control.type = type === 'boolean' ? 'checkbox' : numeric ? 'number' : 'text';
            if (numeric) { control.step = Number(spec.step) >= 1 ? spec.step : 'any'; if (spec.min !== undefined) control.min = spec.min; if (spec.max !== undefined) control.max = spec.max; }
          }
          if (type === 'boolean') control.checked = !!spec.default;
          else control.value = String(spec.default ?? '');
        }
        descriptors.push(videoQueueField(key, spec.label || key.replaceAll('_', ' '), control, current?.numeric ?? numeric));
      }
      for (const [key, slot] of componentDefinitions) {
        const control = el('select'); control.add(new Option('None / clear component', ''));
        for (const item of inventory.components || []) if (slot.ids.has(optionId(item.id))) control.add(new Option(item.name, optionId(item.id)));
        control.value = slots.get(key)?.control.value ?? optionId(slot.default_id);
        descriptors.push(videoQueueField('components.' + key, slot.label, control));
      }
      return descriptors;
    },
  });
  loadInventory(); loadJobs(); pollTimer = setInterval(() => { if (!document.hidden) loadJobs(); }, 2000);
  return { overlay, close };
}
