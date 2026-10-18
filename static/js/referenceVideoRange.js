// Ranges belong to input objects, not list positions: removing another input
// or restoring a draft must never move a trim to a different video.
import { acquireLocalVideoPreview } from './videoLocalPreview.js';
const ranges = new WeakMap();
const durations = new WeakMap(), serverMetadata = new WeakMap(), durationQueue = [];
let activeDurationProbes = 0;
const fallback = () => ({ start_seconds: 0, duration_seconds: 15 });
export function parseReferenceVideoStart(value) {
  const text = String(value ?? '').trim(), parts = text.split(':');
  const seconds = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
  if (parts.length > 3 || !seconds.test(parts.at(-1))) return null;
  if (parts.length > 1 && (parts.slice(0,-1).some(part=>!/^\d+$/.test(part)) || Number(parts.at(-1)) >= 60
      || parts.length === 3 && Number(parts[1]) >= 60)) return null;
  const result = parts.reduce((total,part)=>total*60+Number(part),0);
  return Number.isFinite(result) && result >= 0 ? result : null;
}
export function referenceVideoRange(file) { return { ...(ranges.get(file) || fallback()) }; }
export function hasReferenceVideoRange(file) { return ranges.has(file); }
export function setReferenceVideoRange(file, range) {
  ranges.set(file, { start_seconds: Number(range.start_seconds), duration_seconds: Number(range.duration_seconds) });
}
export function referenceVideoRanges(state) {
  return [...(state?.retained || []), ...(state?.files || [])].map(referenceVideoRange);
}
export function restoreReferenceVideoRanges(state, values = []) {
  [...state.retained, ...state.files].forEach((file, i) => setReferenceVideoRange(file, values[i] || fallback()));
}
export function validateReferenceVideoRanges(values) {
  if (!Array.isArray(values) || values.length > 3 || values.some(row => !row || typeof row !== 'object'
    || !Number.isFinite(row.start_seconds) || row.start_seconds < 0
    || !Number.isFinite(row.duration_seconds) || row.duration_seconds < 2 || row.duration_seconds > 15)) {
    throw new Error('Each reference video needs a start time of 0 or later and a length of 2–15 seconds.');
  }
  return values.map(row => ({ start_seconds: row.start_seconds, duration_seconds: row.duration_seconds }));
}
function timestamp(seconds) {
  const hundredths = Math.round(Math.max(0, Number(seconds) || 0)*100), minutes = Math.floor(hundredths / 6000);
  return `${minutes}:${((hundredths % 6000)/100).toFixed(2).padStart(5, '0')}`;
}
function pumpDurationProbes() {
  while (activeDurationProbes < 2 && durationQueue.length) {
    const next = durationQueue.shift(); if (next.signal.aborted) continue;
    activeDurationProbes++;
    next.run().finally(() => { activeDurationProbes--; pumpDurationProbes(); });
  }
}
function detectLocalDuration(file, signal, receive) {
  if (durations.has(file)) { receive(durations.get(file)); return; }
  if (file.serverFile) {
    durationQueue.push({signal,run:async()=>{
      const controller=new AbortController(),abort=()=>controller.abort(),timer=setTimeout(abort,35000);signal.addEventListener('abort',abort,{once:true});
      try {
        const response=await fetch(`/api/video/h3/server-files/metadata?${new URLSearchParams(file.serverFile)}`,{credentials:'same-origin',signal:controller.signal});
        if(!response.ok)throw new Error('Metadata unavailable');
        const result=await response.json(),value=Number(result.duration_seconds);serverMetadata.set(file,result);
        if(Number.isFinite(value)&&value>0){durations.set(file,value);if(!signal.aborted)receive(value);}
      } catch { if(!signal.aborted)receive(0); }
      finally {clearTimeout(timer);signal.removeEventListener('abort',abort);}
    }});pumpDurationProbes();return;
  }
  if (!(typeof File !== 'undefined' && file instanceof File)) { receive(0); return; }
  durationQueue.push({ signal, run: () => new Promise(resolve => {
    if (signal.aborted) { resolve(); return; }
    const media = document.createElement('video'), url = URL.createObjectURL(file);
    let finished = false;
    const finish = value => {
      if (finished) return; finished = true; clearTimeout(timer); signal.removeEventListener('abort', abort);
      media.removeEventListener('loadedmetadata', loaded); media.removeEventListener('error', failed);
      media.removeAttribute('src'); media.load(); URL.revokeObjectURL(url);
      if (value > 0) durations.set(file, value);
      if (!signal.aborted) receive(value); resolve();
    };
    const loaded = () => finish(Number.isFinite(media.duration) ? media.duration : 0);
    const failed = () => finish(0), abort = () => finish(0), timer = setTimeout(failed, 8000);
    signal.addEventListener('abort', abort, { once: true }); media.addEventListener('loadedmetadata', loaded); media.addEventListener('error', failed);
    media.preload = 'metadata'; media.src = url;
  }) }); pumpDurationProbes();
}
export function createReferenceVideoRangeEditor(file, onChange = () => {}, { preview = true, automatic = false } = {}) {
  const node = document.createElement('div'); node.className = 'h3-reference-range';
  const manualLabel = document.createElement('label'); manualLabel.className = 'video-batch-toggle';
  const manual = document.createElement('input'); manual.type = 'checkbox'; manual.checked = ranges.has(file);
  manual.setAttribute('aria-label', `Choose a section for ${file.name}`);
  const manualText = document.createElement('span'); manualText.textContent = 'Choose a section (optional)'; manualLabel.append(manual, manualText);
  if (automatic) node.append(manualLabel);
  const controls = document.createElement('div'); controls.className = 'h3-reference-range-controls';
  const makeNumber = (label, value, min, max) => {
    const wrap = document.createElement('label'); wrap.className = 'h3-video-field';
    const text = document.createElement('span'); text.textContent = label;
    const input = document.createElement('input'); input.className = 'cookbook-field-input'; input.type = 'number';
    input.step = '.01'; input.min = min; input.required = true; if (max !== undefined) input.max = max; input.value = value;
    input.setAttribute('aria-label', `${label} for ${file.name}`); wrap.append(text, input); controls.append(wrap); return input;
  };
  const initial = referenceVideoRange(file);
  const start = makeNumber('Start at (seconds or time)', initial.start_seconds, 0);
  start.type = 'text'; start.inputMode = 'text'; start.placeholder = '90 or 01:30';
  const timeHelp = document.createElement('small'); timeHelp.className = 'h3-video-muted';
  timeHelp.textContent = 'Seconds, MM:SS or HH:MM:SS; for example 23:14.5.'; start.parentElement.append(timeHelp);
  const duration = makeNumber('Length (seconds)', initial.duration_seconds, 2, 15);
  const summary = document.createElement('p'); summary.className = 'h3-video-muted'; summary.setAttribute('aria-live', 'polite');
  node.append(controls, summary);
  let video, objectUrl, useCurrent, playSelection, mediaDuration = durations.get(file) || 0, disabled = false, disposed = false, playingSelection = false, previewOffset = 0, compatible = false;
  let localPreview, previewController, previewGeneration = 0;
  const durationController = new AbortController();
  function render() {
    controls.hidden = automatic && !manual.checked;
    start.disabled = duration.disabled = disabled || automatic && !manual.checked;
    manual.disabled = disabled;
    const begin = parseReferenceVideoStart(start.value), length = Number(duration.value);
    start.setCustomValidity(begin === null ? 'Enter seconds, MM:SS or HH:MM:SS, with seconds below 60 in a timestamp.'
      : mediaDuration && begin > mediaDuration - 2 ? 'Choose a start with at least 2 seconds remaining.' : '');
    if (begin === null) { summary.textContent = start.validationMessage; return; }
    const end = mediaDuration ? Math.min(mediaDuration, begin + length) : begin + length;
    summary.textContent = automatic && !manual.checked
      ? mediaDuration ? mediaDuration <= 15 ? `Auto: use the full ${mediaDuration.toFixed(2)} second video.` : `Auto: use the first 15 seconds of ${timestamp(mediaDuration)}. Choose a section to use another part.`
        : 'Auto: video length will be detected when queued; use the whole clip up to 15 seconds.'
      : `Use ${timestamp(begin)} → ${timestamp(end)}${mediaDuration ? ` of ${timestamp(mediaDuration)}` : ' · ends earlier if the source ends'} · audio follows this section.`;
  }
  function change() { const begin = parseReferenceVideoStart(start.value); if (begin !== null) setReferenceVideoRange(file, { start_seconds: begin, duration_seconds: Number(duration.value) }); render(); onChange(); }
  start.oninput = duration.oninput = change;
  manual.onchange = () => {
    if (manual.checked) change();
    else { ranges.delete(file); start.value = 0; duration.value = 15; render(); onChange(); }
  };
  if (preview && (typeof File !== 'undefined' && file instanceof File || file.preview_url || file.serverFile)) {
    const details = document.createElement('details'); details.className = 'h3-reference-preview';
    const heading = document.createElement('summary'); heading.textContent = 'Preview video and choose a section';
    video = document.createElement('video'); video.controls = true; video.playsInline = true; video.preload = 'none';
    video.setAttribute('aria-label', `Preview ${file.name}`);
    const actions = document.createElement('div'); actions.className = 'h3-video-actions';
    const previewStatus = document.createElement('p'); previewStatus.className = 'h3-video-muted'; previewStatus.setAttribute('role','status');
    const action = text => { const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = text; actions.append(button); return button; };
    useCurrent = action('Use current time as start'); playSelection = action('Play selected section');
    const local = typeof File !== 'undefined' && file instanceof File;
    if (local) localPreview = acquireLocalVideoPreview(file);
    const canConvert = !!(file.serverFile || localPreview);
    const fallbackButton = canConvert ? action('Compatible preview') : null;
    const cancelUpload = local ? action('Cancel preview upload') : null;
    const jumps = document.createElement('div'); jumps.className = 'h3-reference-jumps';
    const jumpButtons = [];
    for (const [direction,sign] of [['backward',-1],['forward',1]]) {
      const group = document.createElement('div'); group.className = 'h3-reference-jump-group';
      group.setAttribute('role','group'); group.setAttribute('aria-label',`Jump ${direction}`);
      const label = document.createElement('span'); label.textContent = `Jump ${direction}`;
      const row = document.createElement('div'); row.className = 'h3-reference-jump-row';
      for (const [seconds,text,spoken] of [[30,'30s','30 seconds'],[60,'60s','60 seconds'],[300,'5min','5 minutes'],[600,'10min','10 minutes'],[1800,'30min','30 minutes']]) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn';
        button.textContent = `${sign < 0 ? '−' : '+'}${text}`; button.setAttribute('aria-label',`Jump ${direction} ${spoken}`);
        button.onclick = () => jump(sign*seconds); row.append(button); jumpButtons.push({button,sign});
      }
      group.append(label,row); jumps.append(group);
    }
    if (cancelUpload) cancelUpload.hidden = true;
    let busy = false, localProxyUrl = null, localPreviewId = null, playWhenReady = false, selectWhenReady = false, windowTime = 0;
    const incompatibleContainer = /\.(mkv|avi)$/i.test(file.name);
    function updateJumps() {
      const current = previewOffset+(Number(video.currentTime)||0);
      for (const {button,sign} of jumpButtons) button.disabled = busy || video.readyState < 1 || (sign < 0 ? current <= .001 : mediaDuration > 0 && current >= Math.max(0,mediaDuration-2)-.001);
    }
    function setBusy(value) { busy = value; if (fallbackButton) fallbackButton.disabled = value; if (cancelUpload) cancelUpload.hidden = !value; updateJumps(); }
    function useMetadata(metadata) {
      const value = Number(metadata?.duration_seconds);
      if (Number.isFinite(value) && value > 0) { mediaDuration = value; durations.set(file,value); start.max = Math.max(0,value-2); render(); }
    }
    function loadWindow(offset, play = false, selection = play) {
      if (disposed) return;
      compatible = true; playingSelection = false; playWhenReady = play; selectWhenReady = selection;
      windowTime = Math.max(0,mediaDuration ? Math.min(offset,mediaDuration) : offset);
      previewOffset = Math.max(0,mediaDuration ? Math.min(offset,Math.max(0,mediaDuration-2)) : offset);
      const query = new URLSearchParams({...(file.serverFile || {}),start_seconds:previewOffset,duration_seconds:30});
      video.preload = 'metadata'; video.src = `${localProxyUrl || '/api/video/h3/server-files/preview'}?${query}`;
      updateJumps();
      previewStatus.textContent = `Compatible preview starts at ${timestamp(previewOffset)} in the original video. Use current time keeps that original position.`;
    }
    async function compatiblePreview(offset = parseReferenceVideoStart(start.value), play = false, selection = play) {
      if (!canConvert || disposed || busy) return;
      if (offset === null) { start.reportValidity(); return; }
      if (file.serverFile) { loadWindow(offset,play,selection); return; }
      const token = ++previewGeneration; setBusy(true);
      try {
        const result = await localPreview.load(progress => {
          if (disposed || token !== previewGeneration) return;
          previewStatus.textContent = progress.phase === 'preparing' ? 'Preparing a compatible preview…'
            : `Uploading a temporary preview copy… ${progress.total > 0 ? Math.min(100,Math.round(progress.loaded/progress.total*100)) + '%' : ''} Your original attachment is unchanged.`;
        });
        if (disposed || token !== previewGeneration) return;
        localProxyUrl = result.preview_url; localPreviewId = result.id; useMetadata(result.metadata); loadWindow(offset,play,selection);
      } catch (error) {
        if (!disposed && token === previewGeneration) previewStatus.textContent = error.name === 'AbortError'
          ? 'Preview upload cancelled. Use Compatible preview to try again.' : `${error.message} Your selected section is unchanged.`;
      } finally { if (!disposed && token === previewGeneration) setBusy(false); }
    }
    if (fallbackButton) fallbackButton.onclick = () => compatiblePreview();
    if (cancelUpload) cancelUpload.onclick = () => localPreview.cancel();
    function jump(delta) {
      if (disposed || busy || video.readyState < 1) return;
      const target = Math.max(0,Math.min(previewOffset+(Number(video.currentTime)||0)+delta,mediaDuration ? Math.max(0,mediaDuration-2) : Infinity));
      playingSelection = false; video.pause();
      if (compatible) compatiblePreview(target,false,false);
      else { video.currentTime = target; updateJumps(); }
    }
    updateJumps();
    // Do not upload a local file or fetch video windows until this particular
    // preview is expanded. Hundreds of unopened batch rows stay lightweight.
    details.addEventListener('toggle', async () => {
      if (!details.open) { video.pause(); previewController?.abort(); previewController = null; return; }
      if (disposed || video.hasAttribute('src') || busy || previewController) return;
      if (incompatibleContainer && canConvert) { compatiblePreview(); if (!file.serverFile) return; }
      if (file.serverFile) {
        const controller = new AbortController(); previewController = controller;
        const timer = setTimeout(() => controller.abort(),35000);
        if (!compatible) previewStatus.textContent = 'Checking video format…';
        try {
          let metadata = serverMetadata.get(file);
          if (!metadata) { const response = await fetch(`/api/video/h3/server-files/metadata?${new URLSearchParams(file.serverFile)}`,{credentials:'same-origin',signal:controller.signal}); if (response.ok) { metadata = await response.json(); serverMetadata.set(file,metadata); } }
          if (disposed || !details.open || previewController !== controller) return;
          useMetadata(metadata);
          if (metadata?.browser_compatible === false && !video.hasAttribute('src')) { compatiblePreview(); return; }
        } catch { if (disposed || !details.open || previewController !== controller) return; }
        finally { clearTimeout(timer); if (previewController === controller) previewController = null; }
      }
      if (disposed || video.hasAttribute('src')) return;
      previewStatus.textContent = ''; video.preload = 'metadata';
      video.src = file.preview_url || (file.serverFile ? `/api/video/h3/server-files/video?${new URLSearchParams(file.serverFile)}` : (objectUrl = URL.createObjectURL(file)));
    });
    useCurrent.onclick = () => { if (disabled) return; const time=previewOffset+video.currentTime;manual.checked = true; start.value = Math.max(0, Math.min(time, mediaDuration ? mediaDuration - 2 : time)).toFixed(2); change(); };
    playSelection.onclick = () => {
      if (!start.reportValidity() || !duration.reportValidity()) return;
      const begin = parseReferenceVideoStart(start.value); if (begin === null) { start.reportValidity(); return; }
      const end = begin + Number(duration.value);
      if (compatible && (begin < previewOffset || !Number.isFinite(video.duration) || end > previewOffset + video.duration)) { compatiblePreview(begin,true); return; }
      playingSelection = true; video.currentTime = Math.max(0,begin-previewOffset); video.play().catch(() => {});
    };
    video.addEventListener('pause', () => { playingSelection = false; });
    video.addEventListener('timeupdate', () => { updateJumps(); const begin = parseReferenceVideoStart(start.value); if (playingSelection && !video.paused && begin !== null && video.currentTime+previewOffset >= begin + Number(duration.value)) video.pause(); });
    video.addEventListener('loadedmetadata', () => {
      if (disposed || !Number.isFinite(video.duration)) return;
      if(!compatible){mediaDuration = video.duration; durations.set(file, mediaDuration); start.max = Math.max(0, mediaDuration - 2);}
      // Requested length is an upper bound. Keep the user's saved range intact;
      // the server clips at EOF and rejects fewer than two usable seconds.
      render();
      if (compatible) video.currentTime = Math.max(0,Math.min(video.duration,windowTime-previewOffset));
      updateJumps();
      if (playWhenReady) { playWhenReady = false; playingSelection = selectWhenReady; video.play().catch(() => {}); }
    });
    video.addEventListener('error', async () => {
      if (disposed) return;
      if (canConvert && !compatible && !busy) { compatiblePreview(); return; }
      previewStatus.textContent = canConvert ? 'Compatible preview could not load. Use Compatible preview to retry; your selected section is unchanged.'
        : 'This browser cannot preview this file. Its length will still be detected on the server when queued; you can enter a start time manually.';
      if (compatible && localPreviewId) {
        const failedUrl = video.src, failedId = localPreviewId;
        try {
          const response = await fetch(failedUrl,{method:'HEAD',credentials:'same-origin'});
          if (!disposed && video.src === failedUrl && [404,410].includes(response.status)) {
            localPreview.invalidate(failedId);
            previewStatus.textContent = 'The temporary preview expired. Use Compatible preview to upload it again; your selected section is unchanged.';
          }
        } catch { /* The visible retry remains available after a connection failure. */ }
      }
    });
    details.append(heading, video, jumps, actions,previewStatus); node.append(details);
  }
  render();
  if (automatic) detectLocalDuration(file, durationController.signal, value => { if (!disposed && value > 0) { mediaDuration = value; start.max = Math.max(0, mediaDuration - 2); render(); } });
  return { node,
    setDisabled(value) { disabled = value; if (useCurrent) useCurrent.disabled = value; render(); },
    setValue(value) { manual.checked = true; start.value = value.start_seconds; duration.value = value.duration_seconds; setReferenceVideoRange(file, value); render(); },
    getValue() { return referenceVideoRange(file); },
    destroy() { disposed = true; previewGeneration++; previewController?.abort(); durationController.abort(); localPreview?.release(); if (video) { video.pause(); video.removeAttribute('src'); video.load(); } if (objectUrl) URL.revokeObjectURL(objectUrl); },
  };
}
export function referenceVideoRangesField(values) {
  return { key: 'reference_video_ranges', label: 'Reference video sections', create: onChange => {
    const node = document.createElement('div'), editors = values.map((value, i) => {
      const file = { name: `reference video ${i + 1}` }; setReferenceVideoRange(file, value);
      const editor = createReferenceVideoRangeEditor(file, onChange, { preview: false }); node.append(editor.node); return editor;
    });
    return { node, setDisabled: value => editors.forEach(editor => editor.setDisabled(value)),
      setValue: value => editors.forEach((editor, i) => editor.setValue(value?.[i] || values[i])),
      getValue: () => validateReferenceVideoRanges(editors.map(editor => editor.getValue())) };
  } };
}
