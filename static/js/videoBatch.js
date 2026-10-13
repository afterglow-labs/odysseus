// A batch is a series of ordinary jobs, each with exactly one input video.
// File objects remain streamed uploads; no video bytes are buffered in JS.
import { appendVideoInputs } from './videoInputs.js';
import { showVideoServerFiles } from './videoServerFiles.js';
import { referenceVideoRange, hasReferenceVideoRange, createReferenceVideoRangeEditor, validateReferenceVideoRanges } from './referenceVideoRange.js';
const VIDEO = /\.(mp4|mov|m4v|webm|mkv|avi)$/i;
const IMAGE = /\.(png|jpe?g|webp)$/i, AUDIO = /\.(wav|mp3|flac|m4a|aac|ogg|opus)$/i;
const H3_FIELDS = new Set(['reference_videos','source_video','reference_images','first_frame','reference_audio','copies']);
const inputKind = field => field === 'copies' ? 'copy' : ['reference_images','first_frame'].includes(field) ? 'image' : field === 'reference_audio' ? 'audio' : 'video';
const acceptsInput = (file,field) => ({video:VIDEO,image:IMAGE,audio:AUDIO}[inputKind(field)]?.test(file.name) && file.size>0);
const fieldFor = family => {
  if (family === 'h3') return 'reference_videos';
  if (family === 'bfs') return 'source_video';
  throw new Error('Unknown video workflow family.');
};
const identity = () => crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('');
const fileKey = file => file.serverFile ? JSON.stringify(['server', file.serverFile.root, file.serverFile.path, file.size, file.serverFile.mtime_ns])
  : JSON.stringify([file.webkitRelativePath || file.name, file.size, file.lastModified]);
// Keep unsubmitted File objects when a workflow panel is closed and reopened.
// They live only in this page, never in localStorage or another user's session.
const sessions = new Map();
let unloadGuardInstalled = false;

export function snapshotVideoBatch(value, family) {
  const defaultField = fieldFor(family), videoField = value?.videoField;
  if (!value?.config || !(videoField === defaultField || family === 'h3' && H3_FIELDS.has(videoField))) throw new Error('Choose a video workflow before adding a batch.');
  const config = JSON.parse(JSON.stringify(value.config));
  const uploads = Object.fromEntries(Object.entries(value.uploads || {})
    .filter(([field]) => field !== videoField).map(([field, files]) => [field, [...files]]));
  return { config, uploads, videoField, autoVideoLength: family === 'h3' && value.autoVideoLength === true };
}

export function videoBatchFormData(item) {
  const body = new FormData();
  body.append('config', JSON.stringify(item.snapshot.config));
  if (item.snapshot.autoVideoLength) body.append('auto_video_length', 'true');
  appendVideoInputs(body, { ...item.snapshot.uploads, ...(item.snapshot.videoField==='copies'?{}:{[item.snapshot.videoField]:[item.file]}) });
  return body;
}

export function submitVideoBatchJob(family, item, onProgress) {
  fieldFor(family);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const failure = (message, status = 0) => Object.assign(new Error(message), { status });
    xhr.open('POST', `/api/video/${family}/jobs`);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-Odysseus-Submission-Id', item.id);
    xhr.upload.onprogress = event => onProgress?.({ loaded: event.loaded, total: event.lengthComputable ? event.total : 0 });
    xhr.upload.onload = () => onProgress?.({ waiting: true });
    xhr.onerror = () => reject(failure('Connection lost. Retry this file to check or finish its original submission.'));
    xhr.onabort = () => reject(failure('Upload interrupted. Retry this file to check or finish its original submission.'));
    xhr.onload = () => {
      let result = {};
      try { result = JSON.parse(xhr.responseText); } catch {}
      if (xhr.status < 200 || xhr.status >= 300) {
        const message = result.detail || result.error;
        reject(failure(typeof message === 'string' ? message : `Could not queue this file (${xhr.status}).`, xhr.status));
      } else {
        const job = result.job || result;
        if (!job || typeof job.id !== 'string' || !job.id) reject(failure('The server did not confirm this job. Retry to check the original submission.'));
        else resolve(job);
      }
    };
    xhr.send(videoBatchFormData(item));
  });
}

export class VideoBatchQueue {
  constructor({ family, getSnapshot, onChange = () => {}, onJob = () => {}, submit = submitVideoBatchJob }) {
    fieldFor(family);
    Object.assign(this, { family, getSnapshot, onChange, onJob, submit });
    this.inputField=fieldFor(family); this.items = []; this.running = false; this.pauseRequested = false; this.disposed = false;
  }
  add(files) {
    if (this.running) throw new Error('Pause adding jobs before changing the batch file list.');
    const keys = new Set(this.items.map(item => item.key));
    const result = { added: 0, duplicates: 0, rejected: [] };
    const ordered = [...files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));
    for (const file of ordered) {
      if (!acceptsInput(file,this.inputField)) { result.rejected.push(file.name); continue; }
      const key = fileKey(file);
      if (keys.has(key)) { result.duplicates++; continue; }
      keys.add(key); this.items.push({ id: identity(), key, file, status: 'pending', snapshot: null, error: '', progress: null }); result.added++;
    }
    this.onChange(); return result;
  }
  addCopies(count) {
    if(this.running)throw new Error('Pause adding jobs before changing the batch.');
    if(!Number.isInteger(count)||count<1||count>1000)throw new Error('Choose 1–1000 copies.');
    for(let i=0;i<count;i++){const id=identity();this.items.push({id,key:id,file:{name:`Copy ${this.items.length+1}`,copy:true},status:'pending',snapshot:null,error:'',progress:null});}
    this.onChange();
  }
  remove(id) { if (!this.running) { this.items = this.items.filter(item => item.id !== id); this.onChange(); } }
  clear() { if (!this.running) { this.items = []; this.onChange(); } }
  clearAccepted() { if (!this.running) { this.items = this.items.filter(item => item.status !== 'queued'); this.onChange(); } }
  pause() { this.pauseRequested = true; this.onChange(); }
  dispose() { this.disposed = true; this.pauseRequested = true; }
  async run({ retry = false } = {}) {
    if (this.running || this.disposed) return;
    const selected = this.items.filter(item => item.status === (retry ? 'error' : 'pending'));
    if (!selected.length) return;
    // Capture once before the first upload, including files which wait behind
    // a pause. Editing the form cannot change an in-progress batch or retry.
    if (selected.some(item => !item.snapshot)) {
      const snapshot = snapshotVideoBatch(this.getSnapshot(), this.family);
      const prepared = new Map();
      for (const item of selected) if (!item.snapshot) {
        if(snapshot.videoField==='copies' ? !item.file.copy : item.file.copy || !acceptsInput(item.file,snapshot.videoField))
          throw new Error('The waiting batch contains a different input type. Switch back to that batch type or clear its waiting files.');
        if (this.family === 'h3' && snapshot.videoField === 'reference_videos') {
          const config={...snapshot.config};
          if(hasReferenceVideoRange(item.file)||!snapshot.autoVideoLength)config.reference_video_ranges=validateReferenceVideoRanges([referenceVideoRange(item.file)]);
          else delete config.reference_video_ranges;
          prepared.set(item,{...snapshot,config});
        } else prepared.set(item,snapshot);
      }
      for(const [item,value] of prepared)item.snapshot=value;
    }
    this.running = true; this.pauseRequested = false; this.onChange();
    try {
      for (const item of selected) {
        if (this.pauseRequested || this.disposed) break;
        item.status = 'uploading'; item.error = ''; item.progress = null; this.onChange();
        try {
          const job = await this.submit(this.family, item, progress => { item.progress = progress; this.onChange(); });
          item.job = job; item.status = 'queued';
          // A rendering/list refresh error must not turn an accepted job into
          // an upload failure and offer an unnecessary duplicate submission.
          try { this.onJob(job); } catch {}
        } catch (error) {
          item.status = 'error'; item.error = error.message || 'Could not queue this file.';
          if (!error.status || error.status === 401 || error.status === 403 || error.status === 409 || error.status === 429 || error.status >= 500) this.pauseRequested = true;
        }
        this.onChange();
      }
    } finally { this.running = false; this.onChange(); }
  }
}

const node = (tag, className = '', text) => {
  const value = document.createElement(tag); value.className = className;
  if (text !== undefined) value.textContent = text;
  return value;
};
const action = text => { const value = node('button', 'memory-toolbar-btn', text); value.type = 'button'; return value; };

export function videoBatchLengthLabel(job) {
  const timing = job?.batch_video_length;
  if (!timing || !Number.isFinite(timing.duration_seconds) || !Number.isFinite(timing.selected_seconds)) return '';
  return timing.preserve_source_duration
    ? `${timing.duration_seconds.toFixed(2)}s source · ${timing.frames} sampling frames · preserves source length`
    : `${timing.duration_seconds.toFixed(2)}s source → ${timing.selected_seconds.toFixed(2)}s · ${timing.frames} frames`;
}

export function createVideoBatchQueue({ family, container, getSnapshot, onJob, onChange = () => {}, sessionKey = family, recipe = null }) {
  const retained = sessions.get(sessionKey);
  const panel = node('section', 'video-batch-panel');
  const label = node('label', 'video-batch-toggle'), toggle = node('input'); toggle.type = 'checkbox'; toggle.dataset.batchToggle = '';
  toggle.checked = retained?.checked || false;
  label.append(toggle, node('strong', '', 'Batch job')); panel.appendChild(label);
  const content = node('div', 'video-batch-content'); content.hidden = !toggle.checked; panel.appendChild(content);
  const explanation = node('p', 'h3-video-muted'); content.appendChild(explanation);
  let videoField = retained?.inputField || fieldFor(family), choices = [], serverAvailable = false;
  const typeLabel=node('label','h3-video-field'), typeSelect=node('select','cookbook-field-input');typeSelect.setAttribute('aria-label','Batch input');typeLabel.append(node('span','','Batch input'),typeSelect);typeLabel.hidden=true;content.append(typeLabel);
  const lengthLabel = node('label', 'video-batch-toggle'), autoLength = node('input'); autoLength.type = 'checkbox'; autoLength.dataset.batchAutoLength = '';
  autoLength.checked = retained?.autoVideoLength ?? true;
  lengthLabel.append(autoLength, node('span', '', 'Automatically match each video’s length'));
  const lengthHelp = node('p', 'h3-video-muted'); content.append(lengthLabel, lengthHelp);
  function describeVideoField(field) {
    videoField = field;
    const kind=inputKind(field),noun=kind==='copy'?'copies':`${kind}s`;
    explanation.textContent = field==='copies' ? 'Repeat this draft as separate jobs, including its attached inputs, prompt, settings and seed.'
      : `One ${kind} per job. Other inputs, prompt and settings are shared. Turn Batch job off to combine multiple references in one job.`;
    if(field==='source_video')explanation.textContent+=recipe==='seed_hunter'?' Each video is a separate continuation source.':' Each video is the VFX source to edit.';
    lengthLabel.hidden = family !== 'h3' || field !== 'reference_videos';
    lengthHelp.hidden = family !== 'h3' || !['reference_videos','source_video'].includes(field);
    lengthHelp.textContent = field === 'source_video'
      ? recipe==='seed_hunter'?'Each source uses the continuation length and overlap chosen above.':'VFX Edit automatically matches each source video’s length.'
      : autoLength.checked ? 'Each video automatically gets its own supported output length. No manual timing is required. Choose a section only when you want a different part.'
        : 'Every video uses the generation length selected in this draft.';
    input.accept={video:'.mp4,.mov,.m4v,.webm,.mkv,.avi',image:'.png,.jpg,.jpeg,.webp',audio:'.wav,.mp3,.flac,.m4a,.aac,.ogg,.opus'}[kind]||'';
    drop.hidden=kind==='copy';repeatControls.hidden=kind!=='copy';dropTitle.textContent=`Drop all your ${noun} here`;dropHelp.textContent=`or click to select multiple ${noun}`;drop.setAttribute('aria-label',`Add batch ${noun}`);
    serverFiles.hidden=!serverAvailable||kind!=='video';list.setAttribute('aria-label',`Batch ${noun}`);
    queue.inputField=field;typeSelect.value=field;sessions.get(sessionKey).inputField=field;
  }
  const drop = node('div', 'video-batch-drop'); drop.tabIndex = 0; drop.setAttribute('role', 'button'); drop.setAttribute('aria-label', 'Add batch videos');
  const dropTitle=node('strong'),dropHelp=node('span','h3-video-muted');drop.append(dropTitle,dropHelp);
  const input = node('input'); input.type = 'file'; input.multiple = true; input.accept = '.mp4,.mov,.m4v,.webm,.mkv,.avi'; input.hidden = true; input.dataset.batchFiles = '';
  content.append(drop, input);
  const repeatControls=node('div','h3-video-actions'),repeatCount=node('input','cookbook-field-input'),addCopies=action('Add copies');repeatCount.type='number';repeatCount.min=1;repeatCount.max=1000;repeatCount.value=1;repeatCount.setAttribute('aria-label','Number of copies');repeatControls.append(repeatCount,addCopies);content.append(repeatControls);
  const serverFiles = action('Server files'); serverFiles.hidden = true; content.appendChild(serverFiles);
  const note = node('p', 'h3-video-muted'); note.setAttribute('role', 'status'); content.appendChild(note);
  const list = node('ol', 'video-batch-list'); list.setAttribute('aria-label', 'Batch videos'); content.appendChild(list);
  const progress = node('progress', 'video-batch-progress'); progress.hidden = true; progress.setAttribute('aria-label', 'Current video upload'); content.appendChild(progress);
  const status = node('p', 'h3-video-muted video-batch-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite'); content.appendChild(status);
  const error = node('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert'); content.appendChild(error);
  const actions = node('div', 'h3-video-actions video-batch-actions');
  const queueButton = action('Queue videos'), retryButton = action('Retry failed'), pauseButton = action('Pause adding'), newBatchButton = action('New batch'), clearButton = action('Clear list');
  queueButton.className = 'cookbook-btn'; actions.append(queueButton, retryButton, pauseButton, newBatchButton, clearButton); content.appendChild(actions);
  content.appendChild(node('p', 'h3-video-muted', 'Uploads run one at a time in the listed order; queued jobs render on the server. Pause or closing this panel stops adding after the current upload finishes. Reopen it to resume with the saved prompt and settings. Refreshing the page loses files not yet queued. Clearing this list does not delete server jobs. You can start another batch with a different prompt and settings while earlier jobs render.'));
  container.appendChild(panel);
  let available = true, enabled = false, reason = '', snapshotEnabled = true, snapshotReason = '', destroyed = false, previousBusy = false;
  const rows = new Map();
  if (!unloadGuardInstalled) {
    window.addEventListener('beforeunload', event => {
      if ([...sessions.values()].some(({ queue }) => queue.running || queue.items.some(item => ['pending', 'error'].includes(item.status)))) {
        event.preventDefault(); event.returnValue = '';
      }
    });
    unloadGuardInstalled = true;
  }
  const notifyHost = () => onChange({ active: available && toggle.checked, busy: queue.running });
  const queue = retained?.queue || new VideoBatchQueue({ family, getSnapshot, onJob });
  queue.disposed = false; queue.getSnapshot = getSnapshot; queue.onJob = onJob || (() => {});
  queue.onChange = () => {
    if (destroyed) return;
    render();
    if (previousBusy !== queue.running) { previousBusy = queue.running; notifyHost(); }
  };
  sessions.set(sessionKey, { queue, checked: toggle.checked, autoVideoLength: autoLength.checked, inputField: videoField });
  function render() {
    if (destroyed) return;
    const pending = queue.items.filter(item => item.status === 'pending').length;
    const failed = queue.items.filter(item => item.status === 'error').length;
    const accepted = queue.items.filter(item => item.status === 'queued').length;
    const uploading = queue.items.find(item => item.status === 'uploading');
    const readyFor = state => enabled || snapshotEnabled && queue.items.filter(item => item.status === state).every(item => item.snapshot);
    toggle.disabled = queue.running; input.disabled = queue.running; autoLength.disabled = queue.running;
    serverFiles.disabled = queue.running;typeSelect.disabled=repeatCount.disabled=addCopies.disabled=queue.running;
    drop.setAttribute('aria-disabled', String(queue.running));
    for (const [id, row] of rows) if (!queue.items.some(item => item.id === id)) { row._range?.destroy(); row.remove(); rows.delete(id); }
    for (const item of queue.items) {
      let row = rows.get(item.id);
      if (!row) {
        row = node('li', 'video-batch-row'); row.dataset.batchId = item.id;
        row._name = node('span', 'video-batch-name', item.file.name);
        row._state = node('span', 'video-batch-state');
        row._remove = action('Remove'); row._remove.setAttribute('aria-label', `Remove ${item.file.name} from batch`); row._remove.onclick = () => queue.remove(item.id);
        row.append(row._name, row._state, row._remove); rows.set(item.id, row); list.appendChild(row);
      }
      if (!row._range && family === 'h3' && videoField==='reference_videos' && acceptsInput(item.file,'reference_videos')) { row._range = createReferenceVideoRangeEditor(item.file, () => {}, {automatic:true}); row.append(row._range.node); }
      if (row._range) { row._range.node.hidden = videoField !== 'reference_videos'; row._range.setDisabled(queue.running || !!item.snapshot); }
      row.dataset.status = item.status; row._remove.disabled = queue.running;
      row._state.textContent = item.status === 'queued' ? ['Queued', videoBatchLengthLabel(item.job), item.job.id].filter(Boolean).join(' · ') : item.status === 'error' ? item.error
        : item.status === 'uploading' ? item.progress?.waiting ? item.snapshot?.autoVideoLength ? 'Detecting video length and queueing…' : 'Waiting for server…' : item.file.serverFile ? 'Adding server video…' : 'Uploading…'
          : item.file.copy ? 'Ready to queue · saved draft copy' : item.file.serverFile ? 'Ready to queue · server file' : 'Ready to upload · click Queue';
    }
    progress.hidden = !uploading;
    if (uploading?.progress?.total) { progress.max = uploading.progress.total; progress.value = uploading.progress.loaded; }
    else progress.removeAttribute('value');
    const count = `${accepted} / ${queue.items.length} queued${pending ? ` · ${pending} waiting` : ''}${failed ? ` · ${failed} failed` : ''}`;
    status.textContent = !queue.items.length ? 'Choose inputs to build the batch.' : uploading
      ? `${queue.pauseRequested ? 'Pausing after this upload' : uploading.progress?.waiting ? 'Waiting for server confirmation' : 'Uploading'}: ${uploading.file.name} · ${count}`
      : `${count}${queue.pauseRequested && pending ? ' · Paused' : ''}${!queue.running && !readyFor('pending') && (snapshotReason || reason) ? ` · ${snapshotReason || reason}` : ''}`;
    const kind=inputKind(videoField),noun=kind==='copy'?(pending===1?'copy':'copies'):`${kind}${pending===1?'':'s'}`;
    queueButton.textContent = `Queue ${pending} ${noun}`;
    queueButton.disabled = !readyFor('pending') || queue.running || !pending;
    retryButton.hidden = !failed; retryButton.disabled = !readyFor('error') || queue.running;
    pauseButton.hidden = !queue.running; pauseButton.disabled = queue.pauseRequested;
    clearButton.disabled = queue.running || !queue.items.length;
    newBatchButton.hidden = !accepted || !!pending || !!failed || queue.running;
  }
  function add(files) {
    error.hidden = true;
    try {
      const result = queue.add(files);
      note.textContent = [`${result.added} file${result.added === 1 ? '' : 's'} added in filename order.`,
        result.duplicates ? `${result.duplicates} already in this list.` : '',
        result.rejected.length ? `${result.rejected.length} empty or unsupported file${result.rejected.length === 1 ? '' : 's'} skipped: ${result.rejected.slice(0,5).join(', ')}${result.rejected.length > 5 ? '…' : ''}` : ''].filter(Boolean).join(' ');
    } catch (e) { error.textContent = e.message; error.hidden = false; }
  }
  toggle.onchange = () => { content.hidden = !toggle.checked; sessions.get(sessionKey).checked = toggle.checked; notifyHost(); render(); };
  autoLength.onchange = () => { sessions.get(sessionKey).autoVideoLength = autoLength.checked; describeVideoField(videoField); notifyHost(); render(); };
  // Parent forms re-render on input; keep the new choice until this handler commits it.
  typeSelect.addEventListener('input', event => event.stopPropagation());
  typeSelect.onchange=()=>{describeVideoField(typeSelect.value);notifyHost();render();};
  addCopies.onclick=()=>{try{queue.addCopies(Number(repeatCount.value));error.hidden=true;}catch(e){error.textContent=e.message;error.hidden=false;}};
  input.onchange = () => { add(input.files || []); input.value = ''; };
  let picker = null;
  serverFiles.onclick = () => { if (!queue.running) picker = showVideoServerFiles({ family, multiple: true, onSelect: add }, serverFiles); };
  drop.onclick = () => { if (!queue.running) input.click(); };
  drop.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); drop.click(); } };
  for (const eventName of ['dragenter', 'dragover', 'dragleave', 'drop']) drop.addEventListener(eventName, event => {
    event.preventDefault(); event.stopPropagation(); drop.classList.toggle('video-batch-dragover', eventName === 'dragenter' || eventName === 'dragover');
    if (eventName === 'drop') add(event.dataTransfer?.files || []);
  });
  const start = async retry => {
    if (queue.running) return;
    if (!enabled && (!snapshotEnabled || queue.items.some(item => item.status === (retry ? 'error' : 'pending') && !item.snapshot))) return;
    error.hidden = true; note.textContent = '';
    try { await queue.run({ retry }); }
    catch (e) { error.textContent = e.message || 'Could not start this batch.'; error.hidden = false; }
  };
  queueButton.onclick = () => start(false); retryButton.onclick = () => start(true);
  newBatchButton.onclick = () => { if (queue.running) return; queue.clearAccepted(); error.hidden = true; note.textContent = 'Previous jobs keep running. Choose the next inputs and change the prompt or settings for this batch.'; };
  pauseButton.onclick = () => queue.pause(); clearButton.onclick = () => { queue.clear(); note.textContent = ''; error.hidden = true; };
  describeVideoField(videoField);render();
  return {
    setServerFilesAvailable(value) { serverAvailable=!!value;serverFiles.hidden=!serverAvailable||inputKind(videoField)!=='video'; },
    setInputChoices(value) {
      const next=value.map(row=>[row.field,row.label]);if(JSON.stringify(next)!==JSON.stringify(choices)){choices=next;typeSelect.replaceChildren(...next.map(([field,label])=>new Option(label,field)));}
      typeLabel.hidden=choices.length<2;describeVideoField(choices.some(([field])=>field===videoField)?videoField:choices[0]?.[0]||fieldFor(family));
    },
    get inputField(){return videoField;},
    setVideoField: describeVideoField,
    setEnabled(value, explanation = '', saved = {}) {
      enabled = !!value; reason = explanation;
      snapshotEnabled = saved.snapshotEnabled !== false; snapshotReason = saved.snapshotReason || ''; render();
    },
    setAvailable(value) {
      available = !!value; panel.hidden = !available;
    },
    get active() { return available && toggle.checked; }, get busy() { return queue.running; },
    get autoVideoLength() { return family === 'h3' && (videoField === 'reference_videos' && autoLength.checked || videoField === 'source_video' && recipe!=='seed_hunter'); },
    destroy() {
      picker?.close();
      rows.forEach(row => row._range?.destroy());
      destroyed = true; queue.dispose(); queue.onChange = () => {}; queue.onJob = () => {};
      queue.getSnapshot = () => { throw new Error('Reopen the workflow panel to queue these files.'); };
    },
  };
}
