import { bindMenuDismiss, dismissOrRemove, dismissTopMenu } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import * as videoWorkflow from './videoWorkflow.js';
import { createVideoBatchQueue } from './videoBatch.js';
import { createVideoJobEditor, videoJobEditFormData } from './videoJobEdit.js';
import { createVideoQueueControls, videoQueueField } from './videoQueue.js';
import { h3LoraStack, h3LoraIssue, createH3LoraEditor } from './h3Loras.js';

const API = '/api/video/h3';
const STORAGE = 'odysseus-h3-video-settings-v1';
const ENHANCER_STORAGE = 'odysseus-h3-prompt-enhancer-v1';
const ACTIVE = new Set(['queued', 'running']);
const TERMINAL = new Set(['completed', 'failed', 'stopped']);
const MODES = [ ['t2va', 'Text → video + audio'], ['fl2va', 'First / last frame → video + audio'], ['ref2va', 'References → video + audio'] ];
const SETTINGS = ['mode', 'model', 'encoder', 'video_vae', 'audio_vae', 'gpu', 'vae_gpu', 'width', 'height', 'frames', 'steps', 'seed', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'reference_size'];
const NUMBER_FIELDS = new Set([ 'width', 'height', 'frames', 'steps', 'seed', 'shift_video', 'shift_audio']);
const enhancerName = model => String(model || 'Qwen3.8 27B').split(/[\\/]/).filter(Boolean).at(-1) || 'Qwen3.8 27B';

const el = (tag, className = '', text) => {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (text, className = 'memory-toolbar-btn') => {
  const node = el('button', className, text);
  node.type = 'button';
  return node;
};

// H3 packages and their dedicated Qwen encoder must never go through the
// still-image Diffusers form. Adapter repositories keep their file picker.
export function isH3VideoComponent(model) {
  if (!model || model.adapter_only) return false;
  const name = `${model.repo_id || ''} ${model.name || ''} ${model.path || ''}`;
  return /minimax[\s_.-]*h3|h3[\s_.-]*(?:text|video|audio|qwen|vae)|qwen3[\s_.-]*vl.*(?:h3|minimax)/i.test(name);
}

async function request(path, options = {}) {
  const response = await fetch(API + path, { credentials: 'same-origin', ...options });
  let data;
  try { data = await response.json(); } catch (e) { if (e.name === 'AbortError') throw e; data = {}; }
  if (!response.ok) {
    const detail = data.error || data.detail || data.message;
    throw Object.assign(new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`), { status: response.status });
  }
  return data;
}

function formatElapsed(created, finished) {
  let start = typeof created === 'number' ? created : Date.parse(created);
  let end = finished ? (typeof finished === 'number' ? finished : Date.parse(finished)) : Date.now();
  if (start < 1e12) start *= 1000;
  if (end < 1e12) end *= 1000;
  if (!Number.isFinite(start) || !Number.isFinite(end)) return '';
  const secs = Math.max(0, Math.floor((end - start) / 1000));
  return `${Math.floor(secs / 60)}m ${secs % 60}s`;
}

function jobOrder(a, b) {
  const rank = job => job.status === 'running' ? 0 : job.status === 'queued' ? 1 : 2;
  const group = rank(a) - rank(b);
  if (group) return group;
  if (a.status === 'queued' && a.queue_position > 0 && b.queue_position > 0) return a.queue_position - b.queue_position;
  const created = job => typeof job.created_at === 'number' ? job.created_at * 1000 : Date.parse(job.created_at) || 0;
  return rank(a) === 2 ? created(b) - created(a) : created(a) - created(b);
}

// Shared by H3 and BFS. The callback owns packaging/downloading; the dialog
// only records the user's explicit content choices and reports preparation.
export function showVideoWorkflowExportOptions(onExport, anchor = document.activeElement) {
  const overlay = el('div', 'cookbook-edit-overlay video-workflow-export-overlay');
  overlay.style.zIndex = String(topPortalZ());
  const dialog = el('section', 'cookbook-edit-modal h3-video-dialog h3-video-export-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-labelledby', 'video-workflow-export-title');
  const header = el('div', 'h3-video-header'), title = el('h2', '', 'Export workflow'), cancel = button('Cancel');
  title.id = 'video-workflow-export-title'; header.append(title, cancel); dialog.appendChild(header);
  const body = el('div', 'h3-video-scroll'); dialog.appendChild(body);
  body.appendChild(el('p', 'h3-video-description', 'Choose what to include for another Odysseus installation.'));
  const form = el('form'); body.appendChild(form);
  const fieldset = el('fieldset', 'h3-video-export-choices'); fieldset.appendChild(el('legend', '', 'Models, VAEs and LoRAs'));
  function choice(value, label, description, checked = false) {
    const wrap = el('label', 'h3-video-export-choice'), input = el('input');
    input.type = 'radio'; input.name = 'export_model_content'; input.value = value; input.checked = checked;
    const text = el('span'); text.append(el('strong', '', label), el('small', 'h3-video-muted', description));
    wrap.append(input, text); fieldset.appendChild(wrap); return input;
  }
  const references = choice('references', 'Model references only', 'Small workflow file with settings and component references. The other installation needs the matching model files.', true);
  const weights = choice('weights', 'Include model weight files', 'Bundle the selected model, encoder, VAEs and LoRAs. These files can make the export very large.');
  form.appendChild(fieldset);
  const mediaLabel = el('label', 'h3-video-export-choice');
  const media = el('input'); media.type = 'checkbox'; media.name = 'includeAttachments';
  const mediaText = el('span'); mediaText.append(el('strong', '', 'Include input images, video and audio'), el('small', 'h3-video-muted', 'Add the media supplied for this workflow. Leave unchecked to choose new inputs after importing.'));
  mediaLabel.append(media, mediaText); form.appendChild(mediaLabel);
  const destinationLabel = el('label', 'h3-video-field h3-video-export-destination');
  destinationLabel.appendChild(el('span', '', 'Save destination'));
  const destination = el('select', 'cookbook-field-input'); destination.name = 'destination';
  const canChoose = videoWorkflow.canChooseWorkflowDestination?.() === true;
  if (canChoose) destination.add(new Option('Choose a file location…', 'file'));
  destination.add(new Option('Browser download', 'browser'));
  destinationLabel.appendChild(destination); form.appendChild(destinationLabel);
  const destinationHelp = el('p', 'h3-video-muted'); form.appendChild(destinationHelp);
  const status = el('p', 'h3-video-muted'); status.setAttribute('role', 'status'); form.appendChild(status);
  const progress = el('progress', 'h3-video-export-progress'); progress.hidden = true; progress.setAttribute('aria-label', 'Workflow export progress'); form.appendChild(progress);
  const error = el('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert'); form.appendChild(error);
  const actions = el('div', 'h3-video-actions'), submit = button('Export workflow', 'cookbook-btn'); submit.type = 'submit'; actions.appendChild(submit); form.appendChild(actions);
  overlay.appendChild(dialog); document.body.appendChild(overlay);
  let closed = false, exporting = false, controller = null, completed = false;
  const bytes = value => {
    const amount = Math.max(0, Number(value) || 0);
    if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(2)} GiB`;
    if (amount >= 1024 ** 2) return `${(amount / 1024 ** 2).toFixed(1)} MiB`;
    if (amount >= 1024) return `${(amount / 1024).toFixed(1)} KiB`;
    return `${Math.round(amount)} bytes`;
  };
  function describeDestination() {
    const file = destination.value === 'file';
    const safari = /Safari\//.test(navigator.userAgent) && !/Chrom(?:e|ium)|CriOS|Edg|OPR|FxiOS|Android/.test(navigator.userAgent);
    submit.textContent = file ? 'Choose location & export' : 'Start browser download';
    destinationHelp.textContent = file
      ? 'Choose a filename and folder on this device before export starts. Keep this panel open until saving finishes.'
      : `${canChoose ? '' : 'This browser cannot show a save-location picker here. '}Your browser controls the download folder and progress; Odysseus cannot confirm when that download finishes. ${safari ? 'In Safari on Mac, use Safari → Settings → General → File download location → Ask for each download.' : 'Enable your browser’s ask-where-to-save setting to choose each download location.'}`;
  }
  destination.onchange = describeDestination; describeDestination();
  const close = bindMenuDismiss(overlay, () => {
    closed = true; controller?.abort(); window.removeEventListener('keydown', onKey, true);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, event => event.target === overlay);
  const onKey = event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); close(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled)')];
      if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); }
      else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); }
    }
  };
  window.addEventListener('keydown', onKey, true);
  cancel.onclick = () => {
    if (exporting) { controller?.abort(); cancel.disabled = true; cancel.textContent = 'Canceling…'; }
    else close();
  };
  cancel.focus();
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (exporting || completed) return;
    exporting = true; controller = new AbortController(); error.hidden = true;
    submit.disabled = references.disabled = weights.disabled = media.disabled = destination.disabled = true;
    submit.textContent = 'Preparing export…'; cancel.textContent = 'Cancel export';
    status.textContent = destination.value === 'file' ? 'Choose where to save this workflow…' : 'Preparing the browser download…';
    progress.hidden = true;
    const onProgress = update => {
      if (closed || !exporting) return;
      const name = update.filename ? ` · ${update.filename}` : '';
      if (update.phase === 'choosing') status.textContent = 'Choose where to save this workflow…';
      else if (update.phase === 'preparing') status.textContent = `Preparing workflow${name}…`;
      else if (update.phase === 'saving') {
        status.textContent = `Saving ${bytes(update.loaded)}${update.total > 0 ? ` / ${update.totalIsEstimate ? 'about ' : ''}${bytes(update.total)}` : ''}${name}`;
        progress.hidden = false;
        if (update.total > 0) { progress.max = update.total; progress.value = update.loaded || 0; }
        else progress.removeAttribute('value');
        submit.textContent = 'Saving…';
      }
    };
    try {
      // Invoke directly inside this click's activation. The statically loaded
      // transport opens the OS picker before its first await or network call.
      const result = await onExport({ includeWeights: weights.checked, includeAttachments: media.checked, destination: destination.value, signal: controller.signal, onProgress });
      if (closed) return;
      if (!['saved', 'download-started'].includes(result?.status)) throw new Error('The export did not report a save or download result.');
      completed = true; progress.hidden = true; submit.hidden = true;
      status.textContent = result.status === 'saved'
        ? `Saved ${result.filename || 'workflow export'} to your chosen location${Number.isFinite(result.bytes) ? ` · ${bytes(result.bytes)}` : ''}.`
        : `Browser download started${result.filename ? `: ${result.filename}` : ''}. Check your browser’s Downloads for progress and the saved location.`;
    } catch (e) {
      if (!closed) {
        progress.hidden = true;
        if (e.name === 'AbortError' || controller.signal.aborted) status.textContent = 'Export canceled. You can retry when ready.';
        else { error.textContent = e.message || 'Could not export this workflow.'; error.hidden = false; status.textContent = ''; }
      }
    } finally {
      exporting = false; controller = null;
      if (!closed) {
        cancel.disabled = false; cancel.textContent = completed ? 'Done' : 'Cancel';
        if (!completed) { submit.disabled = references.disabled = weights.disabled = media.disabled = destination.disabled = false; describeDestination(); submit.focus(); }
        else cancel.focus();
      }
    }
  });
  return { overlay, close };
}

export function showH3Video({ preferredModel = null } = {}, anchor = document.activeElement) {
  document.querySelectorAll('.h3-video-overlay').forEach(dismissOrRemove);
  if (!document.getElementById('h3-video-styles')) {
    const style = el('link');
    style.id = 'h3-video-styles'; style.rel = 'stylesheet'; style.href = '/static/h3-video.css';
    document.head.appendChild(style);
  }
  let closed = false, submitting = false, polling = false, timer = null, inventory = null, jobs = [], loadFailed = false;
  let enhancing = false, enhancementController = null, enhancer = null, enhancerChecking = false, enhancerCheck = 0;
  let originalPrompt = null, editRevision = 0, enhancementStarted = 0;
  let enhancerSelection = null, enhancerScope = null, selectionInitialized = false;
  let gpuPolling = false, gpuSnapshot = [], gpuUpdated = null, gpuController = null;
  let transferBusy = false, batchQueue = null, inventoryLoading = false, jobEditor = null, queueControls = null, loraTouched = false;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE)) || {}; } catch {}
  let appliedPresetRevision = saved._installedPresetRevision || '';
  const fields = {}, uploads = {}, jobNodes = new Map(), pendingStops = new Set(), pendingDeletes = new Set(), deletedJobs = new Set(), gpuNodes = new Map();
  const overlay = el('div', 'cookbook-edit-overlay h3-video-overlay');
  overlay.style.zIndex = String(topPortalZ());
  const dialog = el('section', 'cookbook-edit-modal h3-video-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-labelledby', 'h3-video-title');
  const header = el('div', 'h3-video-header');
  const title = el('h2', '', 'MiniMax H3 Video'); title.id = 'h3-video-title';
  const closeButton = button('Close');
  header.append(title, closeButton); dialog.appendChild(header);
  const scroll = el('div', 'h3-video-scroll'); dialog.appendChild(scroll);
  scroll.appendChild(el('p', 'h3-video-target', `Local Odysseus server · ${window.location.host}`));
  scroll.appendChild(el('p', 'h3-video-description', 'Generate video with audio on this Odysseus server. Uses the Comfy inference core internally; no ComfyUI window, node editor, or separate server is needed.'));
  const gpuPanel = el('section', 'h3-video-gpu-panel'); gpuPanel.setAttribute('aria-label', 'Live GPU memory');
  const gpuHeader = el('div', 'h3-video-toolbar'); gpuHeader.appendChild(el('h3', '', 'GPU memory'));
  const gpuStatus = el('p', 'h3-video-muted', 'Checking live GPU usage…'); gpuStatus.setAttribute('role', 'status');
  const gpuCards = el('div', 'h3-video-grid h3-video-gpu-cards');
  gpuHeader.appendChild(gpuStatus); gpuPanel.append(gpuHeader, gpuCards,
    el('p', 'h3-video-muted', 'Usage includes all processes. Placement labels and GPU choices apply to new jobs.'));
  scroll.appendChild(gpuPanel);
  const inventoryBar = el('div', 'h3-video-toolbar');
  const inventoryStatus = el('p', 'h3-video-muted', 'Checking cached components…');
  inventoryStatus.setAttribute('role', 'status');
  const refresh = button('Refresh components');
  inventoryBar.append(inventoryStatus, refresh); scroll.appendChild(inventoryBar);
  const transferBar = el('div', 'h3-video-toolbar h3-video-transfer-toolbar');
  const transferActions = el('div', 'h3-video-job-actions'), exportWorkflow = button('Export workflow'), importWorkflow = button('Import workflow');
  exportWorkflow.disabled = importWorkflow.disabled = true;
  const importFile = el('input'); importFile.type = 'file'; importFile.accept = '.json,.zip,application/json,application/zip'; importFile.hidden = true;
  const transferStatus = el('p', 'h3-video-muted'); transferStatus.setAttribute('role', 'status'); transferStatus.hidden = true;
  transferActions.append(exportWorkflow, importWorkflow, importFile); transferBar.append(transferActions, transferStatus); scroll.appendChild(transferBar);
  const error = el('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert'); scroll.appendChild(error);
  const form = el('form', 'h3-video-form'); scroll.appendChild(form);
  const components = el('div', 'h3-video-grid');
  form.appendChild(components);

  function field(parent, key, label, control, help = '') {
    const wrap = el('label', 'h3-video-field');
    wrap.appendChild(el('span', '', label));
    control.name = key; control.id = 'h3-' + key; control.classList.add('cookbook-field-input');
    wrap.appendChild(control);
    if (help) wrap.appendChild(el('small', 'h3-video-muted', help));
    parent.appendChild(wrap); fields[key] = control;
    return control;
  }
  function select(parent, key, label, values = [], help = '') {
    const control = field(parent, key, label, el('select'), help);
    values.forEach(([value, text]) => control.add(new Option(text, value)));
    return control;
  }
  function number(parent, key, label, value, min, max, step = 1, help = '') {
    const control = el('input'); control.type = 'number'; control.value = value;
    control.min = min; control.max = max; control.step = step; control.required = true;
    return field(parent, key, label, control, help);
  }
  select(components, 'model', 'H3 model');
  select(components, 'mode', 'Mode', MODES);
  select(components, 'encoder', 'Text encoder');
  select(components, 'video_vae', 'Video VAE');
  select(components, 'audio_vae', 'Audio VAE');
  const loraEditor = createH3LoraEditor({ onChange: () => { loraTouched = true; editRevision++; updateMode(true); save(); } });
  form.appendChild(loraEditor.node);
  const componentPath = el('p', 'h3-video-component-path h3-video-muted'); form.appendChild(componentPath);
  const loraCompatibilityError = el('p', 'h3-video-error'); loraCompatibilityError.hidden = true; loraCompatibilityError.setAttribute('role', 'alert'); form.appendChild(loraCompatibilityError);
  const presetBar = el('div', 'h3-video-toolbar'); presetBar.hidden = true;
  const presetStatus = el('p', 'h3-video-muted'); presetStatus.setAttribute('role', 'status');
  const useInstalledPreset = button('Use installed preset');
  presetBar.append(presetStatus, useInstalledPreset); form.appendChild(presetBar);
  const promptGroup = el('div', 'h3-video-field'); form.appendChild(promptGroup);
  const promptHeader = el('div', 'h3-video-prompt-header');
  const promptLabel = el('label', '', 'Prompt'); promptLabel.htmlFor = 'h3-prompt';
  const promptActions = el('div', 'h3-video-prompt-actions');
  const enhanceButton = button('Enhance prompt'); enhanceButton.disabled = true;
  const restoreButton = button('Restore original'); restoreButton.hidden = true;
  const cancelEnhance = button('Cancel enhancement'); cancelEnhance.hidden = true;
  promptActions.append(enhanceButton, restoreButton, cancelEnhance);
  promptHeader.append(promptLabel, promptActions); promptGroup.appendChild(promptHeader);
  const prompt = el('textarea', 'cookbook-field-input');
  prompt.id = 'h3-prompt'; prompt.name = 'prompt'; fields.prompt = prompt; promptGroup.appendChild(prompt);
  prompt.rows = 4; prompt.required = true; prompt.maxLength = 16000; prompt.placeholder = 'Describe the scene, movement, sounds, and dialogue…';
  const enhancerBar = el('div', 'h3-video-enhancer-info');
  const enhancerPickerLabel = el('label', 'h3-video-field h3-video-enhancer-picker');
  enhancerPickerLabel.appendChild(el('span', '', 'Enhancement model'));
  const enhancerPicker = el('select', 'cookbook-field-input'); enhancerPicker.id = 'h3-enhancement-model';
  enhancerPicker.setAttribute('aria-label', 'Enhancement model'); enhancerPicker.disabled = true;
  enhancerPicker.add(new Option('Checking available models…', ''));
  enhancerPickerLabel.appendChild(enhancerPicker); promptGroup.appendChild(enhancerPickerLabel);
  const enhancerContext = el('p', 'h3-video-muted'); promptGroup.appendChild(enhancerContext);
  const enhancerStatus = el('p', 'h3-video-muted', 'Checking enhancement models…');
  enhancerStatus.setAttribute('role', 'status'); enhancerStatus.setAttribute('aria-live', 'polite');
  const refreshEnhancer = button('Check models');
  enhancerBar.append(enhancerStatus, refreshEnhancer); promptGroup.appendChild(enhancerBar);
  const enhancerError = el('p', 'h3-video-error'); enhancerError.hidden = true;
  enhancerError.setAttribute('role', 'alert'); promptGroup.appendChild(enhancerError);
  const batchContainer = el('div'); batchContainer.hidden = true; form.appendChild(batchContainer);
  const inputArea = el('div', 'h3-video-inputs'); form.appendChild(inputArea);

  function upload(key, label, accept, multiple, mode) {
    const wrap = el('section', 'h3-video-upload'); wrap.dataset.mode = mode;
    const heading = el('label', 'h3-video-upload-label', label);
    const input = el('input'); input.type = 'file'; input.accept = accept; input.multiple = multiple;
    input.id = 'h3-' + key; heading.htmlFor = input.id;
    const list = el('ul', 'h3-video-file-list');
    const state = { files: [], retained: [], input, wrap, heading, label, multiple };
    const render = () => {
      list.replaceChildren();
      [...state.retained.map(file => ({ file, saved: true })), ...state.files.map(file => ({ file, saved: false }))].forEach(({ file, saved }, index) => {
        const item = el('li');
        const referenceLabel = { reference_images: 'Picture', reference_videos: 'Video', reference_audio: 'Audio' }[key];
        const prefix = key === 'source_video' ? 'Source video · ' : referenceLabel ? `<${referenceLabel} ${index + 1}> · ` : '';
        const name = el('span', '', `${prefix}${file.name} · ${(file.size / 1048576).toFixed(1)} MB${saved ? ' · Saved on server' : ''}`);
        const remove = button('Remove'); remove.setAttribute('aria-label', `Remove ${file.name}`);
        remove.onclick = () => { if (saved) state.retained.splice(index, 1); else state.files.splice(index - state.retained.length, 1); editRevision++; render(); updateReady(); };
        item.append(name, remove); list.appendChild(item);
      });
    };
    state.render = render;
    input.addEventListener('change', () => {
      const selected = Array.from(input.files || []);
      if (!input.multiple && selected.length) state.retained = [];
      state.files = input.multiple ? [...state.files, ...selected] : selected.slice(0, 1);
      input.value = ''; render(); setError('');
    });
    wrap.append(heading, input, list); inputArea.appendChild(wrap); uploads[key] = state;
  }
  upload('first_frame', 'First frame', 'image/jpeg,image/png,image/webp', false, 'fl2va');
  upload('last_frame', 'Last frame', 'image/jpeg,image/png,image/webp', false, 'fl2va');
  upload('source_video', 'Source video', 'video/*', false, 'ref2va');
  upload('reference_images', 'Reference images', 'image/jpeg,image/png,image/webp', true, 'ref2va');
  upload('reference_videos', 'Reference videos', 'video/*', true, 'ref2va');
  upload('reference_audio', 'Reference audio (optional)', 'audio/*', true, 'ref2va');
  const modeHelp = el('p', 'h3-video-muted'); form.appendChild(modeHelp);
  const vfxInputError = el('p', 'h3-video-error'); vfxInputError.hidden = true; vfxInputError.setAttribute('role', 'alert'); form.appendChild(vfxInputError);
  const dimensions = el('div', 'h3-video-grid h3-video-numbers'); form.appendChild(dimensions);
  number(dimensions, 'width', 'Width', 960, 256, 1920, 32);
  number(dimensions, 'height', 'Height', 544, 256, 1920, 32);
  select(dimensions, 'frames', 'Length at 24 fps', [73, 90, 107, ...Array.from({ length: 15 }, (_, index) => 124 + 17 * index)]
    .map(frames => [String(frames), `${(frames / 24).toFixed(2)} seconds · ${frames} frames`]));
  fields.frames.value = '124';
  const sourceTimingHelp = el('small', 'h3-video-muted', 'Matches source video. Its aspect ratio is preserved within the Width × Height pixel budget.');
  sourceTimingHelp.hidden = true; fields.frames.parentElement.appendChild(sourceTimingHelp);
  number(dimensions, 'steps', 'Steps', 20, 1, 100);
  number(dimensions, 'seed', 'Seed', 42, 0, 4294967295);
  select(dimensions, 'gpu', 'Model / encoder GPU');
  select(dimensions, 'vae_gpu', 'VAE GPU', [['', 'Same as model GPU']], 'Runs video/audio VAE encoding and decoding on this GPU. Select the 4090 to keep VAEs separate from the 5090 model.');
  const gpuSelectionError = el('p', 'h3-video-error'); gpuSelectionError.hidden = true; form.appendChild(gpuSelectionError);
  const advanced = el('details', 'h3-video-advanced');
  advanced.appendChild(el('summary', '', 'Advanced'));
  const advancedGrid = el('div', 'h3-video-grid h3-video-numbers'); advanced.appendChild(advancedGrid); form.appendChild(advanced);
  select(advancedGrid, 'sampler', 'Sampler', [['euler', 'Euler'], ['res_multistep', 'RES multistep']]);
  select(advancedGrid, 'scheduler', 'Scheduler', [['simple', 'Simple'], ['normal', 'Normal']]);
  number(advancedGrid, 'shift_video', 'Video shift', 12, 0.01, 100, 0.01);
  number(advancedGrid, 'shift_audio', 'Audio shift', 3, 0.01, 100, 0.01);
  select(advancedGrid, 'reference_size', 'Reference sizing', [['match', 'Match output'], ['max', 'Maximum']]);
  advanced.appendChild(el('p', 'h3-video-muted', 'H3 is distilled. Negative prompts and guidance controls do not apply.'));
  const actions = el('div', 'h3-video-actions');
  const generate = button('Generate video', 'cookbook-btn'); generate.type = 'submit'; generate.disabled = true;
  actions.appendChild(generate); form.appendChild(actions);
  form.appendChild(el('p', 'h3-video-muted', 'Jobs render one at a time in submission order, shared with BFS workflows. Keep editing this draft to add more jobs; each submission saves its own prompt, settings, and files.'));
  const submissionStatus = el('p', 'h3-video-muted h3-video-submission-status');
  submissionStatus.setAttribute('role', 'status'); submissionStatus.setAttribute('aria-live', 'polite'); submissionStatus.hidden = true;
  form.appendChild(submissionStatus);
  const jobsHeader = el('div', 'h3-video-toolbar');
  jobsHeader.appendChild(el('h3', '', 'Recent video jobs'));
  const refreshJobs = button('Refresh jobs'); jobsHeader.appendChild(refreshJobs); scroll.appendChild(jobsHeader);
  const jobStatus = el('p', 'h3-video-muted', 'Loading jobs…'); jobStatus.setAttribute('role', 'status'); scroll.appendChild(jobStatus);
  const queueContainer = el('div'); scroll.appendChild(queueContainer);
  const jobList = el('div', 'h3-video-jobs'); scroll.appendChild(jobList);
  overlay.appendChild(dialog); document.body.appendChild(overlay);

  const setError = message => {
    error.textContent = message || ''; error.hidden = !message;
    if (message) error.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
  const config = () => {
    const loras = loraEditor.getValue();
    return { ...Object.fromEntries(SETTINGS.map(key => [key, NUMBER_FIELDS.has(key) ? Number(fields[key].value) : fields[key].value])), loras, lora: loras[0]?.id || '', lora_scale: loras[0]?.strength ?? 1 };
  };
  function save() {
    if (jobEditor?.active) return;
    try { localStorage.setItem(STORAGE, JSON.stringify({ ...config(), _installedPresetRevision: appliedPresetRevision })); } catch {}
  }
  function applyInstalledPreset({ automatic = false } = {}) {
    const preset = inventory?.installed_preset, values = preset?.config;
    if (!values || !preset.revision || jobEditor?.active || jobEditor?.loading || queueControls?.busy || queueControls?.editing || batchQueue?.busy || transferBusy || enhancing || submitting) return false;
    if (automatic && (loraTouched || Object.prototype.hasOwnProperty.call(saved, 'loras') || appliedPresetRevision === preset.revision || fields.mode.value !== values.mode)) return false;
    if (!automatic) {
      fields.mode.value = values.mode;
      fields.model.value = values.model;
    }
    // An existing draft keeps its prompt, files, base components, dimensions,
    // seed and GPU placement. Installation supplies only the adapter settings.
    loraEditor.setValue(h3LoraStack(values));
    for (const key of ['steps', 'sampler', 'scheduler', 'shift_video', 'shift_audio']) {
      if (values[key] !== undefined) fields[key].value = values[key];
    }
    appliedPresetRevision = preset.revision; editRevision++; save();
    presetStatus.textContent = `${preset.name} applied to this draft. Queued jobs are unchanged.`;
    return true;
  }
  function incompatibleLora() {
    const stack = loraEditor.getValue();
    if (inventory && inventory.lora_stack !== true && stack.length > 1) return 'Multiple LoRAs need the updated Odysseus server. Restart the server and refresh components.';
    return h3LoraIssue(stack, inventory?.components, fields.mode.value, inventory?.max_loras || 8);
  }
  const inputCount = key => uploads[key].files.length + uploads[key].retained.length;
  function isVfxEdit() {
    return loraEditor.getValue().some(row => Number(row.strength) !== 0 && inventory?.components?.some(item => item.id === row.id && item.role === 'lora' && item.recipe === 'vfx_edit'));
  }
  function incompatibleLength() {
    return !isVfxEdit() && Number(fields.frames.value) < 124 ? 'This duration is available only for VFX Edit. Choose a length of at least 124 frames.' : '';
  }
  const batchActive = () => !jobEditor?.active && fields.mode.value === 'ref2va' && !!batchQueue?.active;
  const batchVideoField = () => isVfxEdit() ? 'source_video' : 'reference_videos';
  function vfxInputsIssue({ requireSource = false, batch = batchActive() } = {}) {
    if (!isVfxEdit()) return inputCount('source_video') ? 'The source video requires an active VFX Edit LoRA. Enable it or remove the source video.' : '';
    if (inventory?.vfx_references !== true) return 'Separate VFX source and reference inputs need the updated Odysseus server. Restart the server and refresh components.';
    const keyframes = ['first_frame', 'last_frame'].filter(key => inputCount(key));
    if (keyframes.length) return 'VFX Edit uses Reference mode. Remove the preserved first/last keyframes, or use reference images instead.';
    if (!batch && inputCount('source_video') > 1) return 'VFX Edit accepts one source video per job. Remove extra source videos, or use Batch job.';
    if (requireSource && !batch && !inputCount('source_video')) return 'Choose a source video for VFX Edit. Reference videos are separate guidance inputs.';
    return '';
  }
  function updateInputs() {
    const vfx = isVfxEdit();
    for (const [key, state] of Object.entries(uploads)) {
      const unused = vfx && ['first_frame', 'last_frame'].includes(key) || !vfx && key === 'source_video';
      state.wrap.hidden = unused ? !inputCount(key) : state.wrap.dataset.mode !== fields.mode.value || (key === batchVideoField() && batchActive());
      state.input.disabled = unused || state.wrap.dataset.mode !== fields.mode.value;
      state.input.multiple = state.multiple;
      state.heading.textContent = unused && inputCount(key) ? `${state.label} · unused in this mode` : state.label;
    }
    vfxInputError.textContent = vfxInputsIssue(); vfxInputError.hidden = !vfxInputError.textContent;
  }
  function batchIssue() {
    if (jobEditor?.active || jobEditor?.loading) return 'Finish editing the queued job before adding a batch.';
    if (inventory && inventory.batch_jobs !== true) return 'Batch jobs need the updated Odysseus server. Restart the server after active transfers finish.';
    if (fields.mode.value !== 'ref2va') return 'Batch jobs require References → video + audio mode.';
    if (queueControls?.busy || inventoryLoading || transferBusy || enhancing || submitting) return 'Wait for the current operation to finish.';
    if (loadFailed || !inventory?.runtime_ready) return inventory?.runtime_error || 'The H3 runtime is not ready.';
    if (!['model', 'encoder', 'video_vae', 'audio_vae', 'gpu'].every(key => fields[key].value)) return 'Choose the required models, VAEs, and GPU.';
    if (!['gpu', 'vae_gpu'].every(key => !fields[key].value || inventory.gpus?.some(item => String(item.id) === fields[key].value))) return 'Choose available GPUs.';
    const primary = inventory.gpus.find(item => String(item.id) === fields.gpu.value);
    const selected = ['model', 'encoder', 'video_vae', 'audio_vae'].map(key => inventory.components?.find(item => item.id === fields[key].value));
    selected.push(...loraEditor.getValue().filter(row => Number(row.strength) !== 0).map(row => inventory.components?.find(item => item.id === row.id)));
    if (selected.slice(0, 4).some(item => !item) || selected[0]?.variant !== 'ref2va') return 'Choose available components for References → video + audio.';
    if (incompatibleLora()) return incompatibleLora();
    if (incompatibleLength()) return incompatibleLength();
    if (vfxInputsIssue({ batch: true })) return vfxInputsIssue({ batch: true });
    if (selected.some(item => item?.nvfp4) && !primary.nvfp4) return 'The selected NVFP4 components require a Blackwell GPU, such as the RTX 5090.';
    if (!prompt.value.trim()) return 'Enter the prompt to use for every video.';
    if (Number(fields.width.value) * Number(fields.height.value) > 1032192) return 'Choose dimensions totaling at most 1,032,192 pixels.';
    const images = uploads.reference_images.files.length, audio = uploads.reference_audio.files.length;
    const videos = isVfxEdit() ? uploads.reference_videos.files.length : 1;
    if (images > 9 || videos > 3 || audio > 3 || images + audio + videos > 12) return 'Each batch job supports up to 9 reference images, 3 reference videos, 3 reference audio files, and 12 references total. The VFX source video is separate.';
    if (!form.checkValidity()) return 'Check the video settings before adding the batch.';
    return '';
  }
  function batchSnapshot() {
    form.reportValidity();
    const issue = batchIssue(); if (issue) throw new Error(issue);
    save(); setError('');
    return {
      config: { ...config(), prompt: prompt.value.trim() }, videoField: batchVideoField(),
      uploads: Object.fromEntries(Object.entries(uploads)
        .filter(([key, state]) => key !== batchVideoField() && state.wrap.dataset.mode === 'ref2va')
        .map(([key, state]) => [key, [...state.files]])),
    };
  }
  function updateReady() {
    jobEditor?.observe(jobs);
    const batching = batchActive(), batchBusy = !!batchQueue?.busy;
    const editing = !!jobEditor?.active, editLoading = !!jobEditor?.loading;
    const queueBusy = !!queueControls?.busy;
    const required = ['model', 'encoder', 'video_vae', 'audio_vae', 'gpu'];
    const missingGpu = inventory && ['gpu', 'vae_gpu'].filter(key => fields[key].value && !(inventory.gpus || []).some(item => String(item.id) === fields[key].value));
    gpuSelectionError.textContent = missingGpu?.length ? `Selected ${missingGpu.map(key => key === 'vae_gpu' ? 'VAE GPU' : 'model GPU').join(' and ')} unavailable. Choose an available GPU before generating.` : '';
    gpuSelectionError.hidden = !missingGpu?.length;
    generate.hidden = batching;
    updateInputs();
    fields.mode.disabled = fields.model.disabled = batchBusy;
    loraEditor.setDisabled(batchBusy || queueBusy || editLoading || transferBusy || enhancing || submitting);
    refresh.disabled = inventoryLoading || batchBusy || editing || editLoading;
    loraCompatibilityError.textContent = [incompatibleLora(), incompatibleLength()].filter(Boolean).join(' '); loraCompatibilityError.hidden = !loraCompatibilityError.textContent;
    generate.disabled = queueBusy || batching || batchBusy || editLoading || !!jobEditor?.blocked || transferBusy || enhancing || submitting || loadFailed || !inventory?.runtime_ready || !!missingGpu?.length || !!loraCompatibilityError.textContent || !!vfxInputError.textContent || !required.every(key => fields[key].value);
    useInstalledPreset.disabled = !inventory?.installed_preset || inventoryLoading || queueBusy || !!queueControls?.editing || editing || editLoading || batchBusy || transferBusy || enhancing || submitting;
    exportWorkflow.disabled = importWorkflow.disabled = queueBusy || editing || editLoading || batchBusy || transferBusy || enhancing || submitting || !inventory;
    importWorkflow.textContent = transferBusy ? 'Importing workflow…' : 'Import workflow';
    generate.textContent = submitting ? editing ? 'Saving changes…' : 'Uploading and adding job…' : editing ? 'Save changes' : jobs.some(job => ACTIVE.has(job.status)) ? 'Add to queue' : 'Generate video';
    const issue = batchIssue();
    const snapshotReason = editing || editLoading ? 'Finish editing the queued job before adding a batch.' : inventory && inventory.batch_jobs !== true ? 'Batch jobs need the updated Odysseus server. Restart the server after active transfers finish.'
      : queueBusy || inventoryLoading || transferBusy || enhancing || submitting ? 'Wait for the current operation to finish.'
      : loadFailed || !inventory?.runtime_ready ? inventory?.runtime_error || 'The H3 runtime is not ready.' : '';
    batchQueue?.setEnabled(!issue, issue, { snapshotEnabled: !snapshotReason, snapshotReason });
    batchQueue?.setAvailable(!editing && fields.mode.value === 'ref2va');
    batchContainer.hidden = editing || fields.mode.value !== 'ref2va';
    for (const [id, nodes] of jobNodes) nodes.edit.disabled = queueBusy || !!queueControls?.editing || editing || editLoading || batchBusy || transferBusy || enhancing || submitting || inventoryLoading || !inventory || pendingStops.has(id);
    queueControls?.setBlocked(editing || editLoading || batchBusy || transferBusy || enhancing || submitting || inventoryLoading || pendingStops.size > 0 || pendingDeletes.size > 0);
    updateEnhancerControls();
  }
  function updateEnhancerControls() {
    const retainedImages = Object.entries(uploads).some(([key, state]) => state.wrap.dataset.mode === fields.mode.value && ['first_frame', 'last_frame', 'reference_images'].includes(key) && state.retained.length);
    enhanceButton.disabled = !!queueControls?.busy || retainedImages || !!vfxInputsIssue() || jobEditor?.loading || transferBusy || enhancing || submitting || enhancerChecking || !selectedEnhancer() || !prompt.value.trim();
    enhanceButton.textContent = enhancing ? 'Enhancing…' : 'Enhance prompt';
    restoreButton.hidden = originalPrompt === null; restoreButton.disabled = enhancing || submitting;
    cancelEnhance.hidden = !enhancing;
    refreshEnhancer.disabled = enhancing || enhancerChecking;
    promptGroup.setAttribute('aria-busy', String(enhancing));
    const mode = fields.mode.value;
    if (mode === 'fl2va') {
      const first = !!uploads.first_frame.files.length, last = !!uploads.last_frame.files.length;
      enhancerContext.textContent = first || last
        ? `Sends the ${first && last ? 'first and last frame images' : first ? 'first-frame image' : 'last-frame image'} to the selected enhancement model.`
        : 'Enhancement uses your prompt and video settings. Add first or last frames to include their images.';
    } else if (mode === 'ref2va') {
      const count = uploads.reference_images.files.length;
      enhancerContext.textContent = `${count ? `Sends ${count} reference image${count === 1 ? '' : 's'} to the selected enhancement model.` : 'No reference images attached for enhancement.'} ${batchActive() ? 'Batch enhancement describes one video per job. ' : ''}Video and audio references provide counts only; their contents are not sent.`;
    } else enhancerContext.textContent = 'Enhancement uses your prompt and video settings. Text mode does not send attached images.';
    if (retainedImages) enhancerContext.textContent = 'Saved images stay attached to this job. To use Enhance prompt with their image context, select those images again first.';
    if (isVfxEdit() && !retainedImages) enhancerContext.textContent = `Enhancement makes a concise VFX edit instruction${uploads.reference_images.files.length ? ' and sends your reference images for context' : ''}. Source and reference video/audio contents are not sent; describe their roles in your prompt. The source video is not a numbered reference.`;
  }
  function enhancementPayload() {
    return {
      ...(isVfxEdit() ? { recipe: 'vfx_edit' } : {}),
      endpoint_id: enhancerSelection?.endpoint_id || '', model: enhancerSelection?.model || '',
      prompt: prompt.value.trim(), mode: fields.mode.value,
      frames: Number(fields.frames.value), width: Number(fields.width.value), height: Number(fields.height.value),
      reference_counts: Object.fromEntries(Object.entries(uploads).map(([key, state]) =>
        [key, key === batchVideoField() && batchActive() ? 1 : state.wrap.dataset.mode === fields.mode.value ? inputCount(key) : 0])),
    };
  }
  function validSelection(value) {
    return value && typeof value.endpoint_id === 'string' && !!value.endpoint_id && typeof value.model === 'string' && !!value.model;
  }
  function selectionKey(value) {
    return validSelection(value) ? JSON.stringify([value.endpoint_id, value.model]) : '';
  }
  function selectedEnhancer() {
    const key = selectionKey(enhancerSelection);
    return key ? enhancer?.models?.find(item => selectionKey(item) === key) : null;
  }
  function saveEnhancerSelection() {
    if (!enhancerScope) return;
    const key = `${ENHANCER_STORAGE}:${encodeURIComponent(enhancerScope)}`;
    try {
      if (validSelection(enhancerSelection)) localStorage.setItem(key, JSON.stringify(enhancerSelection));
      else localStorage.removeItem(key);
    } catch {}
  }
  function describeEnhancer() {
    const selected = selectedEnhancer();
    enhancerStatus.title = enhancerSelection?.model || '';
    enhancerStatus.textContent = selected
      ? `Prompt enhancer: ${enhancerName(selected.label || selected.model)}${selected.endpoint_label ? ` · ${selected.endpoint_label}` : ''}. Click Enhance prompt to rewrite your text.`
      : enhancerSelection ? 'Your selected enhancer is unavailable. Check models or choose another model; your selection will not be replaced automatically.'
        : (enhancer?.reason || 'Choose an enhancement model. The loaded Qwen3.8 27B is used by default when available.');
  }
  enhancerPicker.addEventListener('change', () => {
    const selected = enhancer?.models?.find(item => selectionKey(item) === enhancerPicker.value);
    enhancerSelection = selected ? { endpoint_id: selected.endpoint_id, model: selected.model } : null;
    editRevision++; saveEnhancerSelection(); describeEnhancer(); updateEnhancerControls();
  });
  async function checkEnhancer() {
    if (enhancing) return;
    const check = ++enhancerCheck;
    enhancerChecking = true; enhancerStatus.textContent = 'Checking enhancement models…'; updateEnhancerControls();
    try {
      const result = await request('/prompt-enhancer');
      if (closed || check !== enhancerCheck) return;
      enhancer = { ...result, models: (Array.isArray(result.models) ? result.models : []).filter(validSelection) };
      const scope = typeof result.preference_scope === 'string' ? result.preference_scope : null;
      if (scope !== enhancerScope) { enhancerScope = scope; selectionInitialized = false; enhancerSelection = null; }
      if (!selectionInitialized) {
        let remembered;
        if (enhancerScope) {
          try { remembered = JSON.parse(localStorage.getItem(`${ENHANCER_STORAGE}:${encodeURIComponent(enhancerScope)}`)); } catch {}
        }
        enhancerSelection = validSelection(remembered) ? { endpoint_id: remembered.endpoint_id, model: remembered.model }
          : validSelection(result.default) ? { endpoint_id: result.default.endpoint_id, model: result.default.model } : null;
        selectionInitialized = true;
        saveEnhancerSelection();
      }
      enhancerPicker.replaceChildren(new Option('Choose enhancement model…', ''));
      enhancer.models.forEach(item => enhancerPicker.add(new Option(
        `${enhancerName(item.label || item.model)}${item.endpoint_label ? ` · ${item.endpoint_label}` : ''}`, selectionKey(item))));
      if (enhancerSelection && !selectedEnhancer()) {
        enhancerPicker.add(new Option(`Unavailable: ${enhancerName(enhancerSelection.model)}`, selectionKey(enhancerSelection)));
      }
      enhancerPicker.value = selectionKey(enhancerSelection);
      enhancerPicker.disabled = !enhancer.models.length && !enhancerSelection;
      describeEnhancer();
    } catch (e) {
      if (!closed && check === enhancerCheck) { enhancer = null; enhancerStatus.textContent = `Could not check prompt enhancer: ${e.message}`; }
    } finally {
      if (!closed && check === enhancerCheck) { enhancerChecking = false; updateEnhancerControls(); }
    }
  }
  enhanceButton.addEventListener('click', async () => {
    if (enhanceButton.disabled || closed) return;
    const payload = enhancementPayload();
    const requestedModel = enhancerSelection.model;
    const before = prompt.value, revision = editRevision, context = JSON.stringify(payload);
    if (!['width', 'height', 'frames'].every(key => fields[key].reportValidity())) return;
    const controller = new AbortController(); enhancementController = controller;
    enhancing = true; enhancementStarted = Date.now(); enhancerError.hidden = true; enhancerError.textContent = '';
    enhancerStatus.textContent = `Enhancing with ${enhancerName(requestedModel)}…`; updateReady();
    try {
      const imageFields = payload.mode === 'fl2va' ? ['first_frame', 'last_frame'] : payload.mode === 'ref2va' ? ['reference_images'] : [];
      const images = imageFields.flatMap(key => uploads[key].files.map(file => ({ key, file })));
      let body = JSON.stringify(payload), headers = { 'Content-Type': 'application/json' };
      if (images.length) {
        body = new FormData(); body.append('config', JSON.stringify(payload));
        images.forEach(({ key, file }) => body.append(key, file, file.name));
        headers = {};
      }
      const result = await request('/enhance-prompt', {
        method: 'POST', headers, body, signal: controller.signal,
      });
      if (closed || enhancementController !== controller) return;
      if (typeof result.prompt !== 'string' || !result.prompt.trim() || result.prompt.length > 16000) throw new Error('The model did not return a usable prompt. Your original text is unchanged.');
      if (editRevision !== revision || prompt.value !== before || JSON.stringify(enhancementPayload()) !== context) {
        enhancerStatus.textContent = 'Your prompt or video inputs changed during enhancement. Your edits were kept; click Enhance prompt again when ready.';
        return;
      }
      originalPrompt = before; prompt.value = result.prompt.trim(); editRevision++;
      enhancerStatus.title = result.model || requestedModel;
      enhancerStatus.textContent = `Enhanced with ${enhancerName(result.model || requestedModel)}. Review or edit the prompt before submitting your video job.`;
    } catch (e) {
      if (closed || enhancementController !== controller) return;
      if (e.name === 'AbortError') enhancerStatus.textContent = 'Enhancement canceled. Your prompt is unchanged.';
      else {
        enhancerError.textContent = e.message; enhancerError.hidden = false;
        enhancerStatus.textContent = 'Enhancement failed. Your prompt is unchanged.';
      }
    } finally {
      if (enhancementController === controller) {
        enhancementController = null; enhancing = false;
        if (!closed) updateReady();
      }
    }
  });
  cancelEnhance.addEventListener('click', () => enhancementController?.abort());
  restoreButton.addEventListener('click', () => {
    if (originalPrompt === null || enhancing || submitting) return;
    prompt.value = originalPrompt; originalPrompt = null; editRevision++;
    enhancerError.hidden = true; enhancerStatus.textContent = 'Original prompt restored.'; updateEnhancerControls(); prompt.focus();
  });
  refreshEnhancer.addEventListener('click', checkEnhancer);
  form.addEventListener('input', () => { editRevision++; updateReady(); });
  form.addEventListener('change', () => { editRevision++; updateEnhancerControls(); });
  function supports(component, mode) {
    return component?.variant === mode || (mode === 't2va' && component?.variant === 'fl2va');
  }
  function updateMode(fromModel = false) {
    const models = inventory?.components?.filter(item => item.role === 'model') || [];
    const selected = models.find(item => item.id === fields.model.value);
    if (fromModel && selected && !supports(selected, fields.mode.value)) fields.mode.value = selected.variant === 'ref2va' ? 'ref2va' : 't2va';
    if (!fromModel && !supports(selected, fields.mode.value)) {
      fields.model.value = models.find(item => supports(item, fields.mode.value))?.id || '';
    }
    for (const option of fields.mode.options) option.disabled = !models.some(item => supports(item, option.value));
    const vfx = isVfxEdit();
    for (const state of Object.values(uploads)) state.render();
    for (const option of fields.frames.options) { option.disabled = option.hidden = !vfx && Number(option.value) < 124; }
    fields.frames.disabled = vfx;
    sourceTimingHelp.hidden = !vfx;
    batchQueue?.setAvailable(fields.mode.value === 'ref2va');
    batchContainer.hidden = fields.mode.value !== 'ref2va';
    fields.reference_size.disabled = fields.mode.value !== 'ref2va';
    batchQueue?.setVideoField?.(batchVideoField());
    modeHelp.textContent = vfx ? 'VFX Edit changes one source video and retains its original sound. Add optional reference images, videos or audio and describe their roles with <Picture 1>, <Video 1> or <Audio 1>. The source video is a separate guide, not <Video 1>. Use Batch job for one source per job with the same references.' : fields.mode.value === 'fl2va' ? 'Choose a first frame, a last frame, or both to guide the video.' : fields.mode.value === 'ref2va' ? 'Add an image or video, with optional audio. In your prompt, use <Picture 1>, <Video 1>, or <Audio 1> to reference uploads. Clips: 2–15 seconds, with 15 seconds total per video/audio type.' : 'Text mode uses the first / last frame model without image inputs.';
    const current = models.find(item => item.id === fields.model.value);
    componentPath.textContent = current?.path || '';
    componentPath.title = current?.path || '';
    updateReady();
  }
  function fillOptions(key, options, preferred, optional = false) {
    const control = fields[key]; control.replaceChildren();
    control.add(new Option(optional ? 'None' : options.length ? 'Choose component…' : `No cached ${key.replaceAll('_', ' ')} found`, ''));
    options.forEach(item => control.add(new Option(item.name, String(item.id))));
    // An explicit empty selection means unresolved or deliberately unselected,
    // including imported components. Refreshing must not replace it silently.
    if (preferred !== undefined && preferred !== null) control.value = [...control.options].some(option => option.value === String(preferred)) ? String(preferred) : '';
    else if (!optional && options.length) control.value = String(options[0].id);
  }
  function fillGpuOptions(key, options, preferred) {
    const control = fields[key]; control.replaceChildren();
    if (key === 'vae_gpu') control.add(new Option('Same as model GPU', ''));
    options.forEach(item => control.add(new Option(item.name, String(item.id))));
    if (preferred && !options.some(item => String(item.id) === String(preferred))) control.add(new Option('Previously selected GPU is unavailable', String(preferred)));
    if (!control.options.length) control.add(new Option('No available GPU', ''));
    if (preferred !== undefined && [...control.options].some(option => option.value === String(preferred))) control.value = String(preferred);
  }
  function renderGpuUsage() {
    const ids = new Set(gpuSnapshot.map(item => String(item.id)));
    for (const [id, nodes] of gpuNodes) if (!ids.has(id)) { nodes.card.remove(); gpuNodes.delete(id); }
    for (const item of gpuSnapshot) {
      const id = String(item.id); let nodes = gpuNodes.get(id);
      if (!nodes) {
        const card = el('div', 'h3-video-gpu-card'); card.dataset.gpuId = id; card.title = id;
        const heading = el('strong'), roles = el('span', 'h3-video-gpu-roles'), usage = el('p', 'h3-video-muted');
        const meter = el('progress'); meter.setAttribute('aria-label', `${item.name} VRAM used`);
        card.append(heading, roles, usage, meter); gpuCards.appendChild(card);
        nodes = { card, heading, roles, usage, meter }; gpuNodes.set(id, nodes);
      }
      nodes.heading.textContent = item.name || id;
      nodes.roles.textContent = [fields.gpu.value === id ? 'Model / encoder' : '', (fields.vae_gpu.value || fields.gpu.value) === id ? 'VAEs' : ''].filter(Boolean).join(' · ');
      const validMemory = Number.isFinite(item.memory_used_mib) && Number.isFinite(item.memory_total_mib) && item.memory_total_mib > 0;
      const utilization = Number.isFinite(item.utilization_percent) ? ` · GPU ${item.utilization_percent}%` : '';
      nodes.usage.textContent = validMemory ? `${(item.memory_used_mib / 1024).toFixed(1)} / ${(item.memory_total_mib / 1024).toFixed(1)} GiB used${utilization}` : `VRAM usage unavailable${utilization}`;
      nodes.meter.hidden = !validMemory;
      if (validMemory) { nodes.meter.max = item.memory_total_mib; nodes.meter.value = item.memory_used_mib; }
    }
  }
  async function loadGpuUsage() {
    if (closed || gpuPolling) return;
    gpuPolling = true; gpuController = new AbortController();
    try {
      const data = await request('/gpus', { signal: gpuController.signal }); if (closed) return;
      if (data.error) throw new Error(data.error);
      gpuSnapshot = Array.isArray(data.gpus) ? data.gpus : []; gpuUpdated = new Date();
      gpuPanel.classList.remove('h3-video-gpu-stale');
      gpuStatus.textContent = gpuSnapshot.length ? 'Live · refreshes every 2 seconds' : 'No GPU usage reported.';
      renderGpuUsage();
    } catch (e) {
      if (!closed && e.name !== 'AbortError') {
        gpuPanel.classList.add('h3-video-gpu-stale');
        gpuStatus.textContent = `Live usage unavailable${gpuUpdated ? `; last reading ${gpuUpdated.toLocaleTimeString()}` : ''}: ${e.message}`;
      }
    } finally { gpuPolling = false; gpuController = null; }
  }
  async function loadInventory(providedInventory = null) {
    inventoryLoading = true; refresh.disabled = true; inventoryStatus.textContent = 'Checking cached components…'; updateReady();
    try {
      const data = providedInventory || await request('/inventory'); if (closed) return;
      const firstInventory = !inventory;
      const useBaseDefaults = preferredModel || (saved.mode && data.installed_preset && saved.mode !== data.installed_preset.config.mode);
      const previous = inventory ? config() : { ...((useBaseDefaults ? data.base_defaults : data.defaults) || data.defaults || {}), ...saved };
      inventory = data; loadFailed = false;
      presetBar.hidden = !data.installed_preset && !data.installed_preset_error;
      useInstalledPreset.hidden = !data.installed_preset;
      presetStatus.textContent = data.installed_preset
        ? `${data.installed_preset.name} is installed for ${data.installed_preset.config.mode.toUpperCase()} mode. Apply it to this draft at any time.`
        : data.installed_preset_error || '';
      for (const key of ['model', 'encoder', 'video_vae', 'audio_vae']) {
        const options = (data.components || []).filter(item => item.role === key);
        let preferred = previous[key];
        if (preferredModel && key !== 'lora') {
          // Cached rows carry the cache root in `path`, shared by unrelated
          // models and adapters. Match the exact repository directory, never
          // that root. A base-model click must never choose an optional LoRA.
          const repo = preferredModel.repo_id || '';
          const directory = preferredModel.is_local_dir ? repo : `models--${repo.replaceAll('/', '--')}`;
          const match = options.find(item => (repo && (item.path?.includes(`/${directory}/`) || item.repo_id === repo))
            || item.name === preferredModel.name || item.name === repo);
          if (match) preferred = match.id;
        }
        fillOptions(key, options, preferred, key === 'lora');
      }
      loraEditor.setInventory(data.components || [], data.max_loras || 8);
      // Migrate the saved source independently: a new server default stack must
      // not override an older draft's single adapter (including explicit None).
      const loraSource = firstInventory && (Object.prototype.hasOwnProperty.call(saved, 'loras') || Object.prototype.hasOwnProperty.call(saved, 'lora')) ? saved : previous;
      loraEditor.setValue(h3LoraStack(loraSource));
      preferredModel = null;
      fillGpuOptions('gpu', data.gpus || [], previous.gpu);
      const separateVaeGpu = firstInventory && !Object.prototype.hasOwnProperty.call(saved, 'vae_gpu')
        && (data.gpus || []).some(item => String(item.id) === fields.gpu.value && /\b5090\b/.test(item.name))
        ? (data.gpus || []).find(item => /\b4090\b/.test(item.name)) : null;
      fillGpuOptions('vae_gpu', data.gpus || [], separateVaeGpu?.id ?? previous.vae_gpu ?? '');
      renderGpuUsage();
      for (const key of SETTINGS.filter(key => !['model', 'encoder', 'video_vae', 'audio_vae', 'lora', 'gpu', 'vae_gpu'].includes(key))) {
        if (previous[key] !== undefined) fields[key].value = previous[key];
      }
      updateMode(true);
      if (firstInventory && applyInstalledPreset({ automatic: true })) updateMode(true);
      if (separateVaeGpu) save();
      const missing = ['model', 'encoder', 'video_vae', 'audio_vae', 'gpu'].filter(key => !fields[key].value);
      inventoryStatus.textContent = !data.runtime_ready ? `Runtime unavailable: ${data.runtime_error || 'H3 runtime is not installed.'}` : missing.length ? `Missing: ${missing.map(key => key.replaceAll('_', ' ')).join(', ')}. Download the H3 components, then refresh.` : 'Components ready on this Odysseus server.';
      inventoryStatus.classList.toggle('h3-video-error', !data.runtime_ready || !!missing.length);
    } catch (e) {
      if (!closed) { loadFailed = true; inventoryStatus.textContent = e.message; inventoryStatus.classList.add('h3-video-error'); }
    } finally { inventoryLoading = false; if (!closed) updateReady(); }
  }
  function renderJobs() {
    const running = jobs.filter(job => job.status === 'running').length, queued = jobs.filter(job => job.status === 'queued').length;
    jobStatus.textContent = jobs.length ? `${running} rendering · ${queued} queued here. Jobs and outputs are saved on the server. Closing this panel does not change the queue.` : 'No H3 video jobs yet.';
    const ids = new Set(jobs.map(job => job.id));
    for (const [id, nodes] of jobNodes) if (!ids.has(id)) { nodes.card.remove(); jobNodes.delete(id); }
    for (const [index, job] of [...jobs].sort(jobOrder).entries()) {
      let nodes = jobNodes.get(job.id);
      if (!nodes) {
        const card = el('article', 'h3-video-job'); card.dataset.jobId = job.id;
        const row = el('div', 'h3-video-toolbar');
        const heading = el('strong'); const stop = button('Stop'); const remove = button('Delete job'), exportJob = button('Export workflow'), edit = button('Edit');
        const jobActions = el('div', 'h3-video-job-actions'); jobActions.append(edit, exportJob, stop, remove); row.append(heading, jobActions);
        const description = el('p', 'h3-video-muted'); description.setAttribute('role', 'status');
        const progress = el('progress'); progress.max = 1; progress.value = 0; progress.setAttribute('aria-label', 'Video generation progress');
        const jobError = el('p', 'h3-video-error'); jobError.setAttribute('role', 'alert');
        const details = el('details'); details.appendChild(el('summary', '', 'Runtime log'));
        const log = el('pre', 'h3-video-log'); details.appendChild(log);
        const promptDetails = el('details', 'h3-video-job-prompt');
        promptDetails.appendChild(el('summary', '', 'Prompt used'));
        const promptText = el('pre', 'h3-video-prompt-used');
        const promptTools = el('div', 'h3-video-job-prompt-tools');
        const copyPrompt = button('Copy prompt');
        const copyStatus = el('span', 'h3-video-muted');
        copyStatus.setAttribute('role', 'status'); copyStatus.setAttribute('aria-live', 'polite');
        promptTools.append(copyPrompt, copyStatus); promptDetails.append(promptText, promptTools);
        copyPrompt.addEventListener('click', async () => {
          copyPrompt.disabled = true;
          try {
            if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
            await navigator.clipboard.writeText(promptText.textContent);
            if (!closed) copyStatus.textContent = 'Prompt copied.';
          } catch {
            if (!closed) copyStatus.textContent = 'Copy is unavailable here. Select the prompt text to copy it.';
          } finally { if (!closed) copyPrompt.disabled = false; }
        });
        const output = el('div', 'h3-video-output');
        const deletion = el('div', 'h3-video-delete-confirmation'); deletion.hidden = true;
        deletion.appendChild(el('p', '', 'Permanently delete this job, its output, uploads, logs and saved prompt, including its linked Gallery output? This cannot be undone.'));
        const deleteActions = el('div', 'h3-video-job-actions'); const keep = button('Keep job'), confirmDelete = button('Delete permanently');
        confirmDelete.classList.add('h3-video-delete-button'); deleteActions.append(keep, confirmDelete); deletion.appendChild(deleteActions);
        const deleteError = el('p', 'h3-video-error'); deleteError.hidden = true; deleteError.setAttribute('role', 'alert'); deletion.appendChild(deleteError);
        card.append(row, description, deletion, progress, jobError, output, promptDetails, details); jobList.appendChild(card);
        nodes = { card, heading, edit, stop, remove, deletion, keep, confirmDelete, deleteError, description, progress, jobError, log, output, promptText, copyPrompt, copyStatus }; jobNodes.set(job.id, nodes);
        edit.onclick = () => jobEditor.open(job.id);
        stop.onclick = () => stopJob(job.id);
        remove.onclick = () => { deletion.hidden = false; deleteError.hidden = true; keep.focus(); };
        keep.onclick = () => { deletion.hidden = true; remove.focus(); };
        confirmDelete.onclick = () => deleteJob(job.id);
        exportJob.onclick = () => showVideoWorkflowExportOptions(options => videoWorkflow.downloadJobWorkflow('h3', job.id, options), exportJob);
      }
      if (jobList.children[index] !== nodes.card) jobList.insertBefore(nodes.card, jobList.children[index] || null);
      const queued = job.status === 'queued';
      nodes.edit.hidden = !queued;
      const position = Number.isInteger(job.queue_position) && job.queue_position > 0 ? ` · Position ${job.queue_position}` : '';
      nodes.heading.textContent = `${job.source_name ? `${job.source_name} · ` : ''}${queued ? `Queued${position}` : job.status.charAt(0).toUpperCase() + job.status.slice(1)} · ${job.id}`;
      nodes.description.textContent = queued
        ? `Waiting for its turn in the shared video queue · ${formatElapsed(job.created_at)}`
        : [job.phase, job.total_steps ? `Step ${job.step || 0} / ${job.total_steps}` : '', formatElapsed(job.started_at || job.created_at, job.finished_at)].filter(Boolean).join(' · ');
      nodes.progress.hidden = job.status !== 'running';
      if (job.total_steps) { nodes.progress.max = job.total_steps; nodes.progress.value = job.step || 0; }
      else nodes.progress.removeAttribute('value');
      nodes.jobError.textContent = job.error || ''; nodes.jobError.hidden = !job.error;
      nodes.log.textContent = Array.isArray(job.log_tail) ? job.log_tail.join('\n') : (job.log_tail || 'No runtime output yet.');
      const hasPrompt = typeof job.prompt === 'string' && job.prompt.length > 0;
      const usedPrompt = hasPrompt ? job.prompt : 'Prompt unavailable for this older job.';
      // Keep both the details node and its text node intact while polling so
      // reading/copying a past prompt does not collapse it or lose selection.
      if (nodes.promptText.textContent !== usedPrompt) {
        nodes.promptText.textContent = usedPrompt;
        nodes.copyStatus.textContent = '';
      }
      nodes.copyPrompt.hidden = !hasPrompt;
      nodes.stop.hidden = !ACTIVE.has(job.status); nodes.stop.disabled = !!queueControls?.busy || pendingStops.has(job.id);
      nodes.stop.textContent = pendingStops.has(job.id) ? (queued ? 'Canceling…' : 'Stopping and verifying…') : queued ? 'Cancel queued job' : 'Stop';
      nodes.remove.hidden = !TERMINAL.has(job.status); nodes.remove.disabled = !!queueControls?.busy || pendingDeletes.has(job.id);
      nodes.keep.disabled = !!queueControls?.busy || pendingDeletes.has(job.id); nodes.confirmDelete.disabled = !!queueControls?.busy || pendingDeletes.has(job.id);
      nodes.confirmDelete.textContent = pendingDeletes.has(job.id) ? 'Deleting…' : 'Delete permanently';
      if (!TERMINAL.has(job.status)) nodes.deletion.hidden = true;
      if (job.status === 'completed' && !nodes.output.childElementCount) {
        // Keep media URLs on the authenticated job endpoint, never an arbitrary
        // provider URL from model metadata or a persisted job record.
        const url = `${API}/jobs/${encodeURIComponent(job.id)}/video`;
        const video = el('video'); video.controls = true; video.playsInline = true; video.preload = 'metadata'; video.src = url;
        const download = el('a', 'memory-toolbar-btn', 'Download MP4'); download.href = url; download.download = job.filename || 'odysseus-h3.mp4';
        nodes.output.append(video, download);
      }
    }
    updateReady();
  }
  async function loadJobs() {
    if (polling || closed) return;
    polling = true; refreshJobs.disabled = true;
    try {
      const result = await request('/jobs'); if (closed) return;
      queueControls?.update(result.queue);
      jobs = Array.isArray(result.jobs) ? result.jobs.filter(job => !deletedJobs.has(job.id)) : []; renderJobs();
    } catch (e) { if (!closed) jobStatus.textContent = `Could not refresh jobs: ${e.message}`; }
    finally { polling = false; if (!closed) refreshJobs.disabled = false; }
  }
  async function stopJob(id) {
    if (queueControls?.busy || pendingStops.has(id)) return;
    pendingStops.add(id); setError(''); renderJobs();
    try {
      const result = await request(`/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
      if (closed) return;
      const stopped = result.job || result;
      if (stopped.status !== 'stopped' && stopped.status !== 'completed' && stopped.status !== 'failed') throw new Error('The server has not confirmed that this job stopped. Refresh jobs and retry.');
      jobs = jobs.map(job => job.id === id ? stopped : job);
    } catch (e) { if (!closed) setError(`Could not stop video job: ${e.message}`); }
    finally { pendingStops.delete(id); if (!closed) { renderJobs(); await loadJobs(); } }
  }
  async function deleteJob(id) {
    const nodes = jobNodes.get(id);
    if (queueControls?.busy || !nodes || pendingDeletes.has(id) || !TERMINAL.has(jobs.find(job => job.id === id)?.status)) return;
    pendingDeletes.add(id); nodes.deleteError.hidden = true; renderJobs();
    try {
      const result = await request(`/jobs/${encodeURIComponent(id)}/record`, { method: 'DELETE' }); if (closed) return;
      if (result.deleted !== true) throw new Error('The server has not confirmed deletion. Refresh jobs and retry.');
      deletedJobs.add(id); jobs = jobs.filter(job => job.id !== id); renderJobs(); refreshJobs.focus();
    } catch (e) {
      if (!closed) { nodes.deleteError.textContent = `Could not delete job: ${e.message}`; nodes.deleteError.hidden = false; }
    } finally { pendingDeletes.delete(id); if (!closed) renderJobs(); }
  }
  exportWorkflow.onclick = () => {
    const issue = vfxInputsIssue(); if (issue) { setError(issue); return; }
    const snapshot = { ...config(), prompt: prompt.value };
    const attached = Object.fromEntries(Object.entries(uploads).filter(([, state]) => state.wrap.dataset.mode === fields.mode.value).map(([key, state]) => [key, [...state.files]]));
    const currentInventory = inventory;
    showVideoWorkflowExportOptions(options => videoWorkflow.exportVideoWorkflow({ family: 'h3', config: snapshot, inventory: currentInventory, uploads: attached, ...options }), exportWorkflow);
  };
  importWorkflow.onclick = () => importFile.click();
  importFile.onchange = async () => {
    const file = importFile.files?.[0]; importFile.value = ''; if (!file || transferBusy) return;
    const revision = editRevision; transferBusy = true; transferStatus.hidden = false; transferStatus.classList.remove('h3-video-error');
    transferStatus.textContent = 'Importing workflow… Large bundles may take time.'; updateReady();
    try {
      const { importVideoWorkflow } = await import('./videoWorkflow.js');
      const result = await importVideoWorkflow(file, { family: 'h3', inventory, onProgress: ({ phase, loaded, total }) => {
        if (closed || !transferBusy) return;
        transferStatus.textContent = phase === 'installing' ? 'Upload complete; checking and installing workflow files…'
          : total > 0 ? `Uploading workflow… ${Math.min(100, Math.round(loaded / total * 100))}% · ${(loaded / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} MB`
            : 'Uploading workflow…';
      } }); if (closed) return;
      if (revision !== editRevision) throw new Error('The draft changed while importing. Import again to apply this workflow.');
      if (result.inventory) await loadInventory(result.inventory);
      if (closed) return;
      if (revision !== editRevision) throw new Error('The draft changed while importing. Import again to apply this workflow.');
      for (const key of SETTINGS.filter(key => !['gpu', 'vae_gpu'].includes(key))) {
        if (result.config[key] !== undefined) fields[key].value = String(result.config[key]);
      }
      loraEditor.setValue(h3LoraStack(result.config));
      loraTouched = true;
      prompt.value = result.config.prompt || '';
      for (const [key, state] of Object.entries(uploads)) {
        state.files = Array.isArray(result.uploads?.[key]) ? (state.multiple ? result.uploads[key] : result.uploads[key].slice(0, 1)) : [];
        state.input.value = ''; state.render();
      }
      originalPrompt = null; editRevision++; updateMode(true); save();
      transferStatus.textContent = ['Workflow imported into this draft. Review settings and inputs before generating.', ...(result.warnings || [])].join(' ');
    } catch (e) { if (!closed) { transferStatus.textContent = e.message || 'Could not import this workflow.'; transferStatus.classList.add('h3-video-error'); } }
    finally { transferBusy = false; if (!closed) updateReady(); }
  };
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (batchActive() || generate.disabled || !form.reportValidity()) return;
    setError('');
    const values = { ...config(), prompt: prompt.value.trim() };
    if (!values.prompt) { setError('Enter a prompt.'); prompt.focus(); return; }
    if (values.width * values.height > 1032192) { setError('Choose dimensions totaling at most 1,032,192 pixels (for example, 960 × 544 or 1344 × 768).'); return; }
    const vfxIssue = vfxInputsIssue({ requireSource: true });
    if (vfxIssue) { setError(vfxIssue); return; }
    const applicable = Object.entries(uploads).filter(([, state]) => state.wrap.dataset.mode === values.mode);
    if (values.mode === 'fl2va' && !inputCount('first_frame') && !inputCount('last_frame')) { setError('Choose a first frame, a last frame, or both.'); uploads.first_frame.input.focus(); return; }
    if (values.mode === 'ref2va' && !isVfxEdit() && !inputCount('reference_images') && !inputCount('reference_videos')) { setError('Add at least one reference image or video. Audio alone is not supported.'); return; }
    const editing = jobEditor.active;
    const body = editing ? videoJobEditFormData(editing, values,
      Object.fromEntries(applicable.map(([key, state]) => [key, state.files])),
      Object.fromEntries(applicable.map(([key, state]) => [key, state.retained]))) : new FormData();
    if (!editing) {
      body.append('config', JSON.stringify(values));
      applicable.forEach(([key, state]) => state.files.forEach(file => body.append(key, file, file.name)));
    }
    submitting = true; submissionStatus.hidden = true; save(); updateReady();
    try {
      const result = await request(editing ? `/jobs/${encodeURIComponent(editing.id)}` : '/jobs', { method: editing ? 'PATCH' : 'POST', body }); if (closed) return;
      const job = result.job || result;
      if (editing) jobEditor.finish();
      jobs = [job, ...jobs.filter(item => item.id !== job.id)]; renderJobs();
      submissionStatus.textContent = editing ? `Changes saved to job ${job.id}. Its place in the queue is unchanged. Your previous draft has been restored.` : `Job ${job.id} ${job.status === 'queued' ? 'added to the queue' : 'accepted'}. Its prompt, settings, and files are saved. This draft is ready to edit and submit again.`;
      submissionStatus.hidden = false; submissionStatus.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) { if (!closed) { if (editing) jobEditor.failed(e); else setError(e.message); } }
    finally { submitting = false; if (!closed) updateReady(); }
  });
  fields.model.addEventListener('change', () => { updateMode(true); if (applyInstalledPreset({ automatic: true })) updateMode(true); });
  fields.mode.addEventListener('change', () => { updateMode(); if (applyInstalledPreset({ automatic: true })) updateMode(); });
  useInstalledPreset.onclick = () => { if (applyInstalledPreset()) { updateMode(); updateReady(); } };
  form.addEventListener('change', () => { save(); updateReady(); renderGpuUsage(); });
  refresh.onclick = () => loadInventory(); refreshJobs.onclick = loadJobs;
  const onKey = event => {
    if (document.querySelector('.video-workflow-export-overlay')) return;
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation();
      const confirming = [...jobNodes.values()].find(nodes => !nodes.deletion.hidden);
      if (confirming) { if (!confirming.keep.disabled) { confirming.deletion.hidden = true; confirming.remove.focus(); } }
      else dismissTopMenu();
    }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href]')].filter(node => node.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  const close = bindMenuDismiss(overlay, () => {
    closed = true; clearInterval(timer); window.removeEventListener('keydown', onKey, true);
    batchQueue?.destroy(); queueControls?.destroy();
    enhancementController?.abort();
    gpuController?.abort();
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, event => event.target === overlay);
  closeButton.onclick = close; window.addEventListener('keydown', onKey, true); closeButton.focus();
  function applyEditorDraft(values, attached, retained = {}) {
    loraEditor.setValue(h3LoraStack(values));
    // Old VFX jobs used the only reference video as their source guide. New
    // records explicitly contain source_video (even when empty), so never
    // reinterpret one of their reference videos as a source.
    if (isVfxEdit() && !Object.prototype.hasOwnProperty.call(attached, 'source_video')
      && !Object.prototype.hasOwnProperty.call(retained, 'source_video')
      && (attached.reference_videos?.length || 0) + (retained.reference_videos?.length || 0) === 1) {
      attached = { ...attached, source_video: attached.reference_videos || [], reference_videos: [] };
      retained = { ...retained, source_video: retained.reference_videos || [], reference_videos: [] };
    }
    for (const key of [...SETTINGS, 'prompt']) {
      if (values[key] === undefined) continue;
      const control = fields[key], value = String(values[key]);
      if (control.tagName === 'SELECT' && value && ![...control.options].some(option => option.value === value)) control.add(new Option('Previously selected value is unavailable', value));
      control.value = value;
    }
    for (const [key, state] of Object.entries(uploads)) {
      state.files = [...(attached[key] || [])]; state.retained = [...(retained[key] || [])]; state.input.value = ''; state.render();
    }
    editRevision++; updateMode(true); renderGpuUsage();
  }
  jobEditor = createVideoJobEditor({
    form, request, isClosed: () => closed,
    isBusy: () => !!queueControls?.busy || !!queueControls?.editing || submitting || enhancing || transferBusy || inventoryLoading || !!batchQueue?.busy,
    getDraft: () => ({ config: { ...config(), prompt: prompt.value }, uploads: Object.fromEntries(Object.entries(uploads).map(([key, state]) => [key, [...state.files]])), originalPrompt }),
    applyEdit: value => { originalPrompt = null; submissionStatus.hidden = true; applyEditorDraft(value.config, {}, value.inputs); },
    restoreDraft: value => { originalPrompt = value.originalPrompt; applyEditorDraft(value.config, value.uploads); },
    onChange: updateReady, onError: setError,
  });
  batchQueue = createVideoBatchQueue({
    family: 'h3', container: batchContainer, getSnapshot: batchSnapshot,
    onJob: job => { if (!closed) { jobs = [job, ...jobs.filter(item => item.id !== job.id)]; renderJobs(); } },
    onChange: () => { if (!closed) { editRevision++; updateReady(); } },
  });
  queueControls = createVideoQueueControls({
    container: queueContainer, family: 'h3', request, reload: loadJobs, onChange: updateReady, isClosed: () => closed,
    getFields: () => [...SETTINGS, 'prompt'].filter(key => key !== 'mode').map(key => {
      const control = fields[key], label = control.closest('label')?.querySelector('span')?.textContent || (key === 'prompt' ? 'Prompt' : key.replaceAll('_', ' '));
      return videoQueueField(key, label, control, NUMBER_FIELDS.has(key));
    }).concat([{ key: 'loras', label: 'LoRA stack (replace all)', create: onChange => createH3LoraEditor({
      components: inventory?.components || [], value: loraEditor.getValue(), max: inventory?.max_loras || 8, idPrefix: 'h3-queue', onChange,
    }) }]),
  });
  loadInventory(); loadJobs(); checkEnhancer(); loadGpuUsage();
  timer = setInterval(() => {
    if (!document.hidden) { loadJobs(); loadGpuUsage(); }
    if (enhancing) enhancerStatus.textContent = `Enhancing… ${formatElapsed(enhancementStarted)}`;
  }, 2000);
  return { overlay, close };
}
