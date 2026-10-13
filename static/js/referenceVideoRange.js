// Ranges belong to input objects, not list positions: removing another input
// or restoring a draft must never move a trim to a different video.
const ranges = new WeakMap();
const durations = new WeakMap(), durationQueue = [];
let activeDurationProbes = 0;
const fallback = () => ({ start_seconds: 0, duration_seconds: 15 });
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
  const value = Math.max(0, Number(seconds) || 0), minutes = Math.floor(value / 60);
  return `${minutes}:${(value % 60).toFixed(2).padStart(5, '0')}`;
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
        const result=await response.json(),value=Number(result.duration_seconds);
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
  const start = makeNumber('Start at (seconds)', initial.start_seconds, 0);
  const duration = makeNumber('Length (seconds)', initial.duration_seconds, 2, 15);
  const summary = document.createElement('p'); summary.className = 'h3-video-muted'; summary.setAttribute('aria-live', 'polite');
  node.append(controls, summary);
  let video, objectUrl, useCurrent, playSelection, mediaDuration = durations.get(file) || 0, disabled = false, disposed = false, playingSelection = false, previewOffset = 0, compatible = false;
  const durationController = new AbortController();
  function render() {
    controls.hidden = automatic && !manual.checked;
    start.disabled = duration.disabled = disabled || automatic && !manual.checked;
    manual.disabled = disabled;
    const begin = Number(start.value), length = Number(duration.value);
    start.setCustomValidity(mediaDuration && begin > mediaDuration - 2 ? 'Choose a start with at least 2 seconds remaining.' : '');
    const end = mediaDuration ? Math.min(mediaDuration, begin + length) : begin + length;
    summary.textContent = automatic && !manual.checked
      ? mediaDuration ? mediaDuration <= 15 ? `Auto: use the full ${mediaDuration.toFixed(2)} second video.` : `Auto: use the first 15 seconds of ${timestamp(mediaDuration)}. Choose a section to use another part.`
        : 'Auto: video length will be detected when queued; use the whole clip up to 15 seconds.'
      : `Use ${timestamp(begin)} → ${timestamp(end)}${mediaDuration ? ` of ${timestamp(mediaDuration)}` : ' · ends earlier if the source ends'} · audio follows this section.`;
  }
  function change() { setReferenceVideoRange(file, { start_seconds: Number(start.value), duration_seconds: Number(duration.value) }); render(); onChange(); }
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
    // A batch can contain hundreds of videos. Do not open local files or issue
    // server range requests until this particular preview is expanded.
    details.addEventListener('toggle', () => {
      if (!details.open) { video.pause(); return; }
      if (disposed || video.hasAttribute('src')) return;
      video.preload = 'metadata';
      video.src = file.preview_url || (file.serverFile ? `/api/video/h3/server-files/video?${new URLSearchParams(file.serverFile)}` : (objectUrl = URL.createObjectURL(file)));
    });
    const actions = document.createElement('div'); actions.className = 'h3-video-actions';
    const previewStatus = document.createElement('p'); previewStatus.className = 'h3-video-muted'; previewStatus.setAttribute('role','status');
    const action = text => { const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = text; actions.append(button); return button; };
    useCurrent = action('Use current time as start'); playSelection = action('Play selected section');
    useCurrent.onclick = () => { if (disabled) return; const time=previewOffset+video.currentTime;manual.checked = true; start.value = Math.max(0, Math.min(time, mediaDuration ? mediaDuration - 2 : time)).toFixed(2); change(); };
    playSelection.onclick = () => { playingSelection = true; video.currentTime = Math.max(0,Number(start.value)-previewOffset); video.play().catch(() => {}); };
    video.addEventListener('pause', () => { playingSelection = false; });
    video.addEventListener('timeupdate', () => { if (playingSelection && !video.paused && video.currentTime+previewOffset >= Number(start.value) + Number(duration.value)) video.pause(); });
    video.addEventListener('loadedmetadata', () => {
      if (disposed || !Number.isFinite(video.duration)) return;
      if(!compatible){mediaDuration = video.duration; durations.set(file, mediaDuration); start.max = Math.max(0, mediaDuration - 2);}
      // Requested length is an upper bound. Keep the user's saved range intact;
      // the server clips at EOF and rejects fewer than two usable seconds.
      render();
    });
    if(file.serverFile){
      const fallback=action('Compatible preview'),previous=action('Previous 30 seconds'),next=action('Next 30 seconds');previous.hidden=next.hidden=true;
      const loadWindow=offset=>{compatible=true;playingSelection=false;previewOffset=Math.max(0,mediaDuration?Math.min(offset,Math.max(0,mediaDuration-2)):offset);previous.hidden=next.hidden=false;video.preload='metadata';video.src=`/api/video/h3/server-files/preview?${new URLSearchParams({...file.serverFile,start_seconds:previewOffset,duration_seconds:30})}`;previewStatus.textContent=`Compatible preview starts at ${timestamp(previewOffset)} in the original video. Use current time keeps that original position.`;};
      fallback.onclick=()=>loadWindow(Number(start.value)||0);previous.onclick=()=>loadWindow(previewOffset-30);next.onclick=()=>loadWindow(previewOffset+30);
    }
    video.addEventListener('error', () => { previewStatus.textContent = file.serverFile?'This file’s codec cannot play directly here. Use Compatible preview to browse it.':'This browser cannot preview this file. Its length will still be detected on the server when queued; you can enter a start time manually.'; });
    details.append(heading, video, actions,previewStatus); node.append(details);
  }
  render();
  if (automatic) detectLocalDuration(file, durationController.signal, value => { if (!disposed && value > 0) { mediaDuration = value; start.max = Math.max(0, mediaDuration - 2); render(); } });
  return { node,
    setDisabled(value) { disabled = value; if (useCurrent) useCurrent.disabled = value; render(); },
    setValue(value) { manual.checked = true; start.value = value.start_seconds; duration.value = value.duration_seconds; setReferenceVideoRange(file, value); render(); },
    getValue() { return referenceVideoRange(file); },
    destroy() { disposed = true; durationController.abort(); if (video) { video.pause(); video.removeAttribute('src'); video.load(); } if (objectUrl) URL.revokeObjectURL(objectUrl); },
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
