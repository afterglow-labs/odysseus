// Portable workflow transport. Large archives stream to the chosen file or
// the browser downloader, never through a multi-GB response.blob().
const FORMAT = 'odysseus-video-workflow';
const H3_SETTINGS = ['mode', 'width', 'height', 'frames', 'steps', 'seed', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'lora_scale', 'reference_size', 'prompt'];
const H3_COMPONENTS = ['model', 'encoder', 'video_vae', 'audio_vae', 'lora'];
const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const api = family => {
  if (!['h3', 'bfs'].includes(family)) throw new Error('Unknown video workflow family.');
  return `/api/video/${family}`;
};

async function jsonResponse(response) {
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) {
    const error = data.detail || data.error;
    throw new Error(typeof error === 'string' ? error : `Workflow request failed (${response.status}).`);
  }
  return data;
}

function localUrl(value, family) {
  const url = new URL(value, window.location.origin);
  if (url.origin !== window.location.origin || !url.pathname.startsWith(api(family) + '/')) {
    throw new Error('The server returned an invalid workflow download address.');
  }
  return url.href;
}

function download(url, filename = '') {
  const link = document.createElement('a');
  link.href = url;
  if (filename) link.download = filename;
  document.body.appendChild(link); link.click(); link.remove();
}

export function canChooseWorkflowDestination() {
  return typeof window.showSaveFilePicker === 'function' && window.isSecureContext !== false;
}

// Call this from the final export click, before any network request or await,
// so Chromium's save dialog retains the user's transient activation.
function chooseDestination(destination, filename, signal, onProgress) {
  signal?.throwIfAborted();
  if (destination === 'browser') return null;
  if (destination !== 'file' || !canChooseWorkflowDestination()) {
    throw new Error('This browser cannot choose a save location here. Use Chrome or Edge over HTTPS, or select the browser download option.');
  }
  onProgress?.({ phase: 'choosing', filename });
  return window.showSaveFilePicker({
    id: 'odysseus-video-workflow', suggestedName: filename,
    types: [{ description: 'Odysseus workflow ZIP', accept: { 'application/zip': ['.zip'] } }],
  });
}

async function saveDownload(url, { handle, filename, totalBytes, signal, onProgress }) {
  signal?.throwIfAborted();
  if (!handle) {
    download(url, filename);
    onProgress?.({ phase: 'download-started', filename });
    return { status: 'download-started', filename };
  }
  filename = handle.name || filename;
  const leaving = event => { event.preventDefault(); event.returnValue = ''; };
  window.addEventListener('beforeunload', leaving);
  let response;
  try {
    onProgress?.({ phase: 'saving', loaded: 0, total: totalBytes, totalIsEstimate: true, filename });
    response = await fetch(url, { credentials: 'same-origin', signal });
    if (!response.ok) await jsonResponse(response);
    if (!response.body) throw new Error('The workflow download has no file contents.');
    const length = Number(response.headers.get('Content-Length'));
    const total = length > 0 ? length : totalBytes;
    let loaded = 0, lastUpdate = 0, tail = new Uint8Array(0);
    const meter = new TransformStream({
      transform(chunk, controller) {
        loaded += chunk.byteLength;
        // Our ZIP writer emits a comment-free end record. Check that a clean
        // HTTP EOF is also a complete archive before committing the file.
        if (chunk.byteLength >= 22) tail = chunk.slice(-22);
        else {
          const joined = new Uint8Array(tail.length + chunk.byteLength);
          joined.set(tail); joined.set(chunk, tail.length); tail = joined.slice(-22);
        }
        const now = Date.now();
        if (now - lastUpdate >= 150) {
          onProgress?.({ phase: 'saving', loaded, total, totalIsEstimate: !(length > 0), filename });
          lastUpdate = now;
        }
        controller.enqueue(chunk);
      },
      flush() {
        if (length > 0 && loaded !== length || tail.length !== 22
          || new DataView(tail.buffer, tail.byteOffset, tail.byteLength).getUint32(0, true) !== 0x06054b50
          || tail[20] !== 0 || tail[21] !== 0) {
          throw new Error('The workflow download was incomplete. Choose a location and retry the export.');
        }
      },
    });
    const writable = await handle.createWritable();
    await response.body.pipeThrough(meter).pipeTo(writable, { signal });
    onProgress?.({ phase: 'saved', loaded, total: loaded, filename });
    return { status: 'saved', filename, bytes: loaded };
  } finally {
    // If opening the destination failed, release the server's still-unread
    // stream as well. pipeTo handles cancellation once it owns the body.
    if (response?.body && !response.body.locked) await response.body.cancel().catch(() => {});
    window.removeEventListener('beforeunload', leaving);
  }
}

export async function exportVideoWorkflow({ family, config, uploads = {}, includeWeights = false, includeAttachments = false,
  destination = canChooseWorkflowDestination() ? 'file' : 'browser', signal, onProgress }) {
  const endpoint = api(family);
  const handle = await chooseDestination(destination, `${family}.odysseus-workflow.zip`, signal, onProgress);
  signal?.throwIfAborted();
  onProgress?.({ phase: 'preparing', filename: handle?.name });
  const body = new FormData();
  body.append('config', JSON.stringify(config));
  body.append('options', JSON.stringify({ include_weights: includeWeights, include_attachments: includeAttachments }));
  if (includeAttachments) {
    for (const [field, files] of Object.entries(uploads)) {
      for (const file of files || []) body.append(field, file, file.name);
    }
  }
  const result = await jsonResponse(await fetch(endpoint + '/workflow/export', { method: 'POST', credentials: 'same-origin', body, signal }));
  const saved = await saveDownload(localUrl(result.download_url, family), { handle, filename: result.filename,
    totalBytes: result.total_bytes, signal, onProgress });
  return { ...result, ...saved };
}

export async function downloadJobWorkflow(family, jobId, { includeWeights = false, includeAttachments = false,
  destination = canChooseWorkflowDestination() ? 'file' : 'browser', signal, onProgress } = {}) {
  const endpoint = api(family);
  if (!/^[a-f0-9]{32}$/.test(jobId)) throw new Error('Invalid saved video job.');
  const filename = `${family}-${jobId}.odysseus-workflow.zip`;
  const handle = await chooseDestination(destination, filename, signal, onProgress);
  const query = new URLSearchParams({ include_weights: String(includeWeights), include_attachments: String(includeAttachments) });
  return saveDownload(`${endpoint}/jobs/${jobId}/workflow?${query}`, { handle, filename, signal, onProgress });
}

function identity(component) {
  const path = String(component.path || '').replaceAll('\\', '/');
  const match = path.match(/(?:^|\/)models--([^/]+?)--([^/]+)\/snapshots\/([^/]+)\/(.+)$/);
  return { name: component.name, repository: match ? `${match[1]}/${match[2]}` : '', relative_path: match?.[4] || '', revision: match?.[3] || '' };
}

export function resolveVideoWorkflow(workflow, { family, inventory, resolvedComponents = {} }) {
  api(family);
  if (!object(workflow) || workflow.format !== FORMAT || workflow.version !== 1) throw new Error('This is not a supported Odysseus video workflow (version 1).');
  if (workflow.family !== family) throw new Error(`Open the ${workflow.family === 'h3' ? 'MiniMax H3 Video' : 'BFS Video Workflows'} panel to import this workflow.`);
  if (!object(workflow.config) || !object(workflow.components)) throw new Error('The workflow is missing its settings or model selections.');
  const definition = family === 'bfs' ? inventory.workflows?.find(item => item.id === workflow.config.workflow_id) : null;
  if (family === 'bfs' && !definition) throw new Error('This BFS workflow is unavailable in this Odysseus installation. Update Odysseus and try again.');
  const keys = family === 'h3' ? H3_SETTINGS : ['workflow_id', 'prompt', ...(definition.controls || []).map(item => item.key)];
  const config = {}, warnings = [];
  for (const key of keys) {
    if (!own(workflow.config, key)) continue;
    const value = workflow.config[key];
    if (!['string', 'number', 'boolean'].includes(typeof value) || (typeof value === 'number' && !Number.isFinite(value))) throw new Error(`Invalid workflow setting: ${key}.`);
    config[key] = value;
  }
  if (own(config, 'prompt') && (typeof config.prompt !== 'string' || config.prompt.length > 16000)) throw new Error('Invalid workflow prompt.');
  if (family === 'h3' && !['t2va', 'fl2va', 'ref2va'].includes(config.mode)) throw new Error('Unknown MiniMax H3 mode in this workflow.');
  const stacked = family === 'h3' && own(workflow.config, 'lora_strengths');
  const strengths = stacked ? workflow.config.lora_strengths : [];
  if (stacked && (!Array.isArray(strengths) || strengths.length > 8 || strengths.some(value => typeof value !== 'number' || !Number.isFinite(value) || value < -4 || value > 4))) throw new Error('Invalid workflow LoRA strengths.');
  const loraRoles = stacked ? strengths.map((_, index) => `lora_${index}`) : [];
  if (family === 'h3' && Object.keys(workflow.components).some(key => /^lora_/.test(key) && !loraRoles.includes(key))) throw new Error('The workflow LoRA references do not match its strengths.');
  const roles = family === 'h3' ? stacked ? [...H3_COMPONENTS.filter(role => role !== 'lora'), ...loraRoles] : H3_COMPONENTS : (definition.slots || []).map(item => item.key);
  const selections = family === 'h3' ? config : (config.components = {});
  for (const role of roles) {
    selections[role] = '';
    const ref = workflow.components[role];
    const componentRole = loraRoles.includes(role) ? 'lora' : role;
    if (ref === undefined || ref === null) {
      if (loraRoles.includes(role)) throw new Error(`Missing ${role} model reference.`);
      continue;
    }
    if (!object(ref) || typeof ref.name !== 'string' || !ref.name || /[\\/\x00]/.test(ref.name)) throw new Error(`Invalid ${role} model reference.`);
    const slot = definition?.slots?.find(item => item.key === role);
    let candidates = (inventory.components || []).filter(component => component.name === ref.name
      && (family === 'h3' ? component.role === componentRole && (role !== 'model' || component.variant === (config.mode === 'ref2va' ? 'ref2va' : 'fl2va')) : slot?.component_ids?.includes(component.id)));
    const imported = candidates.find(component => component.id === resolvedComponents[role]);
    if (imported) candidates = [imported];
    for (const key of ['repository', 'relative_path', 'revision']) {
      if (ref[key] && !imported) candidates = candidates.filter(component => identity(component)[key] === ref[key]);
    }
    // Duplicate scans of the same resolved file may share an ID.
    candidates = [...new Map(candidates.map(component => [component.id, component])).values()];
    if (candidates.length === 1) {
      selections[role] = candidates[0].id;
    } else {
      warnings.push(candidates.length ? `${ref.name}: more than one local copy matches; choose the intended file.` : `${ref.name}: not installed here; download it or import an export that includes its weights.`);
      // Keep unresolved rows visible. Silently dropping an adapter would alter
      // the imported workflow and allow a different job to be submitted.
      if (family === 'h3' && componentRole === 'lora') selections[role] = `unavailable:${role}:${ref.name}`;
    }
  }
  if (family === 'h3') {
    config.loras = stacked ? loraRoles.map((role, index) => ({ id: selections[role], strength: strengths[index] }))
      : config.lora ? [{ id: config.lora, strength: config.lora_scale ?? 1 }] : [];
    for (const role of loraRoles) delete config[role];
    config.lora = config.loras[0]?.id || ''; config.lora_scale = config.loras[0]?.strength ?? 1;
  }
  // GPU identities, absolute paths, endpoints, and unknown properties never
  // enter the form. Each installation retains its own GPU selections.
  return { config, warnings, inventory, uploads: {} };
}

async function uploadWorkflow(file, family, signal, onProgress) {
  if (!onProgress || typeof XMLHttpRequest === 'undefined') {
    return jsonResponse(await fetch(api(family) + '/workflow/import', {
      method: 'POST', credentials: 'same-origin', signal,
      headers: { 'Content-Type': 'application/octet-stream' }, body: file,
    }));
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const cleanup = () => signal?.removeEventListener('abort', abort);
    xhr.open('POST', api(family) + '/workflow/import');
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.upload.onprogress = event => onProgress({ phase: 'uploading', loaded: event.loaded, total: event.lengthComputable ? event.total : file.size });
    xhr.upload.onload = () => onProgress({ phase: 'installing', loaded: file.size, total: file.size });
    xhr.onerror = () => { cleanup(); reject(new Error('Workflow upload failed. Check the server connection and try again.')); };
    xhr.onabort = () => { cleanup(); reject(new DOMException('Workflow import cancelled.', 'AbortError')); };
    xhr.onload = async () => {
      cleanup();
      try { resolve(await jsonResponse(new Response(xhr.responseText, { status: xhr.status }))); }
      catch (error) { reject(error); }
    };
    if (signal?.aborted) { reject(new DOMException('Workflow import cancelled.', 'AbortError')); return; }
    signal?.addEventListener('abort', abort, { once: true });
    xhr.send(file);
  });
}

export async function importVideoWorkflow(file, { family, inventory, signal, onProgress }) {
  if (!file || !file.size) throw new Error('Choose an Odysseus workflow file.');
  const result = await uploadWorkflow(file, family, signal, onProgress);
  const resolved = resolveVideoWorkflow(result.workflow, { family, inventory: result.inventory || inventory, resolvedComponents: result.resolved_components || {} });
  for (const attachment of result.attachments || []) {
    const allowed = family === 'h3' ? ['first_frame', 'last_frame', 'reference_images', 'reference_videos', 'reference_audio'] : ['identity_image', 'source_video', 'last_frame', 'mask_video'];
    if (!allowed.includes(attachment.field) || typeof attachment.name !== 'string' || /[\\/\x00]/.test(attachment.name)) throw new Error('Invalid workflow attachment.');
    const response = await fetch(localUrl(attachment.url, family), { credentials: 'same-origin', signal });
    if (!response.ok) throw new Error(`Could not restore ${attachment.name}.`);
    const blob = await response.blob();
    const restored = new File([blob], attachment.name, { type: blob.type });
    (resolved.uploads[attachment.field] ||= []).push(restored);
  }
  resolved.warnings.push(...(Array.isArray(result.warnings) ? result.warnings.filter(value => typeof value === 'string') : []));
  return resolved;
}
