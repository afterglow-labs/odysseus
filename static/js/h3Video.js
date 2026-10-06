import { bindMenuDismiss, dismissOrRemove, dismissTopMenu } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';

const API = '/api/video/h3';
const STORAGE = 'odysseus-h3-video-settings-v1';
const ACTIVE = new Set(['queued', 'running']);
const MODES = [ ['t2va', 'Text → video + audio'], ['fl2va', 'First / last frame → video + audio'], ['ref2va', 'References → video + audio'] ];
const SETTINGS = ['mode', 'model', 'encoder', 'video_vae', 'audio_vae', 'lora', 'lora_scale', 'gpu', 'width', 'height', 'frames', 'steps', 'seed', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'reference_size'];
const NUMBER_FIELDS = new Set(['lora_scale', 'width', 'height', 'frames', 'steps', 'seed', 'shift_video', 'shift_audio']);

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
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) {
    const detail = data.error || data.detail || data.message;
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`);
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

export function showH3Video({ preferredModel = null } = {}, anchor = document.activeElement) {
  document.querySelectorAll('.h3-video-overlay').forEach(dismissOrRemove);
  if (!document.getElementById('h3-video-styles')) {
    const style = el('link');
    style.id = 'h3-video-styles'; style.rel = 'stylesheet'; style.href = '/static/h3-video.css';
    document.head.appendChild(style);
  }
  let closed = false, submitting = false, polling = false, timer = null, inventory = null, jobs = [], loadFailed = false;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(STORAGE)) || {}; } catch {}
  const fields = {}, uploads = {}, jobNodes = new Map(), pendingStops = new Set();
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
  const inventoryBar = el('div', 'h3-video-toolbar');
  const inventoryStatus = el('p', 'h3-video-muted', 'Checking cached components…');
  inventoryStatus.setAttribute('role', 'status');
  const refresh = button('Refresh components');
  inventoryBar.append(inventoryStatus, refresh); scroll.appendChild(inventoryBar);
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
  select(components, 'lora', 'LoRA (optional)', [['', 'None']]);
  const componentPath = el('p', 'h3-video-component-path h3-video-muted'); form.appendChild(componentPath);
  const prompt = field(form, 'prompt', 'Prompt', el('textarea'));
  prompt.rows = 4; prompt.required = true; prompt.maxLength = 16000; prompt.placeholder = 'Describe the scene, movement, sounds, and dialogue…';
  const inputArea = el('div', 'h3-video-inputs'); form.appendChild(inputArea);

  function upload(key, label, accept, multiple, mode) {
    const wrap = el('section', 'h3-video-upload'); wrap.dataset.mode = mode;
    const heading = el('label', 'h3-video-upload-label', label);
    const input = el('input'); input.type = 'file'; input.accept = accept; input.multiple = multiple;
    input.id = 'h3-' + key; heading.htmlFor = input.id;
    const list = el('ul', 'h3-video-file-list');
    const state = { files: [], input, wrap, multiple };
    const render = () => {
      list.replaceChildren();
      state.files.forEach((file, index) => {
        const item = el('li');
        const referenceLabel = { reference_images: 'Picture', reference_videos: 'Video', reference_audio: 'Audio' }[key];
        const name = el('span', '', `${referenceLabel ? `<${referenceLabel} ${index + 1}> · ` : ''}${file.name} · ${(file.size / 1048576).toFixed(1)} MB`);
        const remove = button('Remove'); remove.setAttribute('aria-label', `Remove ${file.name}`);
        remove.onclick = () => { state.files.splice(index, 1); render(); };
        item.append(name, remove); list.appendChild(item);
      });
    };
    input.addEventListener('change', () => {
      const selected = Array.from(input.files || []);
      state.files = multiple ? [...state.files, ...selected] : selected.slice(0, 1);
      input.value = ''; render(); setError('');
    });
    wrap.append(heading, input, list); inputArea.appendChild(wrap); uploads[key] = state;
  }
  upload('first_frame', 'First frame', 'image/jpeg,image/png,image/webp', false, 'fl2va');
  upload('last_frame', 'Last frame', 'image/jpeg,image/png,image/webp', false, 'fl2va');
  upload('reference_images', 'Reference images', 'image/jpeg,image/png,image/webp', true, 'ref2va');
  upload('reference_videos', 'Reference videos', 'video/*', true, 'ref2va');
  upload('reference_audio', 'Reference audio (optional)', 'audio/*', true, 'ref2va');
  const modeHelp = el('p', 'h3-video-muted'); form.appendChild(modeHelp);
  const dimensions = el('div', 'h3-video-grid h3-video-numbers'); form.appendChild(dimensions);
  number(dimensions, 'width', 'Width', 960, 256, 1920, 32);
  number(dimensions, 'height', 'Height', 544, 256, 1920, 32);
  select(dimensions, 'frames', 'Length at 24 fps', Array.from({ length: 15 }, (_, index) => {
    const frames = 124 + 17 * index;
    return [String(frames), `${(frames / 24).toFixed(2)} seconds · ${frames} frames`];
  }));
  number(dimensions, 'steps', 'Steps', 20, 1, 100);
  number(dimensions, 'seed', 'Seed', 42, 0, 4294967295);
  select(dimensions, 'gpu', 'GPU');
  const advanced = el('details', 'h3-video-advanced');
  advanced.appendChild(el('summary', '', 'Advanced'));
  const advancedGrid = el('div', 'h3-video-grid h3-video-numbers'); advanced.appendChild(advancedGrid); form.appendChild(advanced);
  select(advancedGrid, 'sampler', 'Sampler', [['euler', 'Euler'], ['res_multistep', 'RES multistep']]);
  select(advancedGrid, 'scheduler', 'Scheduler', [['simple', 'Simple'], ['normal', 'Normal']]);
  number(advancedGrid, 'shift_video', 'Video shift', 12, 0.01, 100, 0.01);
  number(advancedGrid, 'shift_audio', 'Audio shift', 3, 0.01, 100, 0.01);
  number(advancedGrid, 'lora_scale', 'LoRA strength', 1, -4, 4, 0.05);
  select(advancedGrid, 'reference_size', 'Reference sizing', [['match', 'Match output'], ['max', 'Maximum']]);
  advanced.appendChild(el('p', 'h3-video-muted', 'H3 is distilled. Negative prompts and guidance controls do not apply.'));
  const actions = el('div', 'h3-video-actions');
  const generate = button('Generate video', 'cookbook-btn'); generate.type = 'submit'; generate.disabled = true;
  actions.appendChild(generate); form.appendChild(actions);
  const jobsHeader = el('div', 'h3-video-toolbar');
  jobsHeader.appendChild(el('h3', '', 'Recent video jobs'));
  const refreshJobs = button('Refresh jobs'); jobsHeader.appendChild(refreshJobs); scroll.appendChild(jobsHeader);
  const jobStatus = el('p', 'h3-video-muted', 'Loading jobs…'); jobStatus.setAttribute('role', 'status'); scroll.appendChild(jobStatus);
  const jobList = el('div', 'h3-video-jobs'); scroll.appendChild(jobList);
  overlay.appendChild(dialog); document.body.appendChild(overlay);

  const setError = message => {
    error.textContent = message || ''; error.hidden = !message;
    if (message) error.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  };
  const config = () => Object.fromEntries(SETTINGS.map(key => [key, NUMBER_FIELDS.has(key) ? Number(fields[key].value) : fields[key].value]));
  function save() {
    try { localStorage.setItem(STORAGE, JSON.stringify(config())); } catch {}
  }
  function updateReady() {
    const required = ['model', 'encoder', 'video_vae', 'audio_vae', 'gpu'];
    generate.disabled = submitting || loadFailed || !inventory?.runtime_ready || !required.every(key => fields[key].value) || jobs.some(job => ACTIVE.has(job.status));
    generate.textContent = submitting ? 'Uploading and starting…' : jobs.some(job => ACTIVE.has(job.status)) ? 'Video job in progress' : 'Generate video';
  }
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
    for (const state of Object.values(uploads)) state.wrap.hidden = state.wrap.dataset.mode !== fields.mode.value;
    fields.reference_size.disabled = fields.mode.value !== 'ref2va';
    fields.lora_scale.disabled = !fields.lora.value;
    modeHelp.textContent = fields.mode.value === 'fl2va' ? 'Choose a first frame, a last frame, or both to guide the video.' : fields.mode.value === 'ref2va' ? 'Add an image or video, with optional audio. In your prompt, use <Picture 1>, <Video 1>, or <Audio 1> to reference uploads. Clips: 2–15 seconds, with 15 seconds total per video/audio type.' : 'Text mode uses the first / last frame model without image inputs.';
    const current = models.find(item => item.id === fields.model.value);
    componentPath.textContent = current?.path || '';
    componentPath.title = current?.path || '';
    updateReady();
  }
  function fillOptions(key, options, preferred, optional = false) {
    const control = fields[key]; control.replaceChildren();
    if (optional) control.add(new Option('None', ''));
    options.forEach(item => control.add(new Option(item.name, String(item.id))));
    if (!options.length && !optional) control.add(new Option(`No cached ${key.replaceAll('_', ' ')} found`, ''));
    if ([...control.options].some(option => option.value === String(preferred))) control.value = preferred;
  }
  async function loadInventory() {
    refresh.disabled = true; inventoryStatus.textContent = 'Checking cached components…';
    try {
      const data = await request('/inventory'); if (closed) return;
      const previous = inventory ? config() : { ...(data.defaults || {}), ...saved };
      inventory = data; loadFailed = false;
      for (const key of ['model', 'encoder', 'video_vae', 'audio_vae', 'lora']) {
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
      preferredModel = null;
      fillOptions('gpu', data.gpus || [], previous.gpu);
      for (const key of SETTINGS.filter(key => !['model', 'encoder', 'video_vae', 'audio_vae', 'lora', 'gpu'].includes(key))) {
        if (previous[key] !== undefined) fields[key].value = previous[key];
      }
      updateMode(true);
      const missing = ['model', 'encoder', 'video_vae', 'audio_vae', 'gpu'].filter(key => !fields[key].value);
      inventoryStatus.textContent = !data.runtime_ready ? `Runtime unavailable: ${data.runtime_error || 'H3 runtime is not installed.'}` : missing.length ? `Missing: ${missing.map(key => key.replaceAll('_', ' ')).join(', ')}. Download the H3 components, then refresh.` : 'Components ready on this Odysseus server.';
      inventoryStatus.classList.toggle('h3-video-error', !data.runtime_ready || !!missing.length);
    } catch (e) {
      if (!closed) { loadFailed = true; inventoryStatus.textContent = e.message; inventoryStatus.classList.add('h3-video-error'); }
    } finally { if (!closed) { refresh.disabled = false; updateReady(); } }
  }
  function renderJobs() {
    jobStatus.textContent = jobs.length ? 'Jobs and outputs are saved on the server. Closing this panel keeps a job running.' : 'No H3 video jobs yet.';
    const ids = new Set(jobs.map(job => job.id));
    for (const [id, nodes] of jobNodes) if (!ids.has(id)) { nodes.card.remove(); jobNodes.delete(id); }
    for (const [index, job] of jobs.entries()) {
      let nodes = jobNodes.get(job.id);
      if (!nodes) {
        const card = el('article', 'h3-video-job'); card.dataset.jobId = job.id;
        const row = el('div', 'h3-video-toolbar');
        const heading = el('strong'); const stop = button('Stop'); row.append(heading, stop);
        const description = el('p', 'h3-video-muted'); description.setAttribute('role', 'status');
        const progress = el('progress'); progress.max = 1; progress.value = 0; progress.setAttribute('aria-label', 'Video generation progress');
        const jobError = el('p', 'h3-video-error'); jobError.setAttribute('role', 'alert');
        const details = el('details'); details.appendChild(el('summary', '', 'Runtime log'));
        const log = el('pre', 'h3-video-log'); details.appendChild(log);
        const output = el('div', 'h3-video-output');
        card.append(row, description, progress, jobError, output, details); jobList.appendChild(card);
        nodes = { card, heading, stop, description, progress, jobError, log, output }; jobNodes.set(job.id, nodes);
        stop.onclick = () => stopJob(job.id);
      }
      if (jobList.children[index] !== nodes.card) jobList.insertBefore(nodes.card, jobList.children[index] || null);
      nodes.heading.textContent = `${job.status.charAt(0).toUpperCase() + job.status.slice(1)} · ${job.id}`;
      nodes.description.textContent = [job.phase, job.total_steps ? `Step ${job.step || 0} / ${job.total_steps}` : '', formatElapsed(job.created_at, job.finished_at)].filter(Boolean).join(' · ');
      nodes.progress.hidden = !ACTIVE.has(job.status);
      if (job.total_steps) { nodes.progress.max = job.total_steps; nodes.progress.value = job.step || 0; }
      else nodes.progress.removeAttribute('value');
      nodes.jobError.textContent = job.error || ''; nodes.jobError.hidden = !job.error;
      nodes.log.textContent = Array.isArray(job.log_tail) ? job.log_tail.join('\n') : (job.log_tail || 'No runtime output yet.');
      nodes.stop.hidden = !ACTIVE.has(job.status); nodes.stop.disabled = pendingStops.has(job.id);
      nodes.stop.textContent = pendingStops.has(job.id) ? 'Stopping and verifying…' : 'Stop';
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
      jobs = Array.isArray(result.jobs) ? result.jobs : []; renderJobs();
    } catch (e) { if (!closed) jobStatus.textContent = `Could not refresh jobs: ${e.message}`; }
    finally { polling = false; if (!closed) refreshJobs.disabled = false; }
  }
  async function stopJob(id) {
    if (pendingStops.has(id)) return;
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
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (generate.disabled || !form.reportValidity()) return;
    setError('');
    const values = { ...config(), prompt: prompt.value.trim() };
    if (!values.prompt) { setError('Enter a prompt.'); prompt.focus(); return; }
    if (values.width * values.height > 1032192) { setError('Choose dimensions totaling at most 1,032,192 pixels (for example, 960 × 544 or 1344 × 768).'); return; }
    const applicable = Object.entries(uploads).filter(([, state]) => state.wrap.dataset.mode === values.mode);
    if (values.mode === 'fl2va' && !uploads.first_frame.files.length && !uploads.last_frame.files.length) { setError('Choose a first frame, a last frame, or both.'); uploads.first_frame.input.focus(); return; }
    if (values.mode === 'ref2va' && !uploads.reference_images.files.length && !uploads.reference_videos.files.length) { setError('Add at least one reference image or video. Audio alone is not supported.'); return; }
    const body = new FormData(); body.append('config', JSON.stringify(values));
    applicable.forEach(([key, state]) => state.files.forEach(file => body.append(key, file, file.name)));
    submitting = true; save(); updateReady();
    try {
      const result = await request('/jobs', { method: 'POST', body }); if (closed) return;
      const job = result.job || result;
      jobs = [job, ...jobs.filter(item => item.id !== job.id)]; renderJobs();
      jobList.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    } catch (e) { if (!closed) setError(e.message); }
    finally { submitting = false; if (!closed) updateReady(); }
  });
  fields.model.addEventListener('change', () => updateMode(true));
  fields.mode.addEventListener('change', () => updateMode());
  fields.lora.addEventListener('change', () => updateMode(true));
  form.addEventListener('change', save);
  refresh.onclick = loadInventory; refreshJobs.onclick = loadJobs;
  const onKey = event => {
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); dismissTopMenu(); }
    else if (event.key === 'Tab') {
      const controls = [...dialog.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, a[href]')].filter(node => node.getClientRects().length);
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  const close = bindMenuDismiss(overlay, () => {
    closed = true; clearInterval(timer); window.removeEventListener('keydown', onKey, true);
    overlay.remove(); if (anchor?.isConnected) anchor.focus();
  }, event => event.target === overlay);
  closeButton.onclick = close; window.addEventListener('keydown', onKey, true); closeButton.focus();
  loadInventory(); loadJobs();
  timer = setInterval(() => { if (!document.hidden) loadJobs(); }, 2000);
  return { overlay, close };
}
