// Ranges belong to input objects, not list positions: removing another input
// or restoring a draft must never move a trim to a different video.
const ranges = new WeakMap();
const fallback = () => ({ start_seconds: 0, duration_seconds: 15 });
export function referenceVideoRange(file) { return { ...(ranges.get(file) || fallback()) }; }
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
export function createReferenceVideoRangeEditor(file, onChange = () => {}, { preview = true } = {}) {
  const node = document.createElement('div'); node.className = 'h3-reference-range';
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
  let video, objectUrl, useCurrent, playSelection, mediaDuration = 0, disabled = false, disposed = false, playingSelection = false;
  function render() {
    const begin = Number(start.value), length = Number(duration.value);
    start.setCustomValidity(mediaDuration && begin > mediaDuration - 2 ? 'Choose a start with at least 2 seconds remaining.' : '');
    const end = mediaDuration ? Math.min(mediaDuration, begin + length) : begin + length;
    summary.textContent = `Use ${timestamp(begin)} → ${timestamp(end)}${mediaDuration ? ` of ${timestamp(mediaDuration)}` : ' · ends earlier if the source ends'} · audio follows this section.`;
  }
  function change() { setReferenceVideoRange(file, { start_seconds: Number(start.value), duration_seconds: Number(duration.value) }); render(); onChange(); }
  start.oninput = duration.oninput = change;
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
    const action = text => { const button = document.createElement('button'); button.type = 'button'; button.className = 'memory-toolbar-btn'; button.textContent = text; actions.append(button); return button; };
    useCurrent = action('Use current time as start'); playSelection = action('Play selected section');
    useCurrent.onclick = () => { if (disabled) return; start.value = Math.max(0, Math.min(video.currentTime, mediaDuration ? mediaDuration - 2 : video.currentTime)).toFixed(2); change(); };
    playSelection.onclick = () => { playingSelection = true; video.currentTime = Number(start.value); video.play().catch(() => {}); };
    video.addEventListener('pause', () => { playingSelection = false; });
    video.addEventListener('timeupdate', () => { if (playingSelection && !video.paused && video.currentTime >= Number(start.value) + Number(duration.value)) video.pause(); });
    video.addEventListener('loadedmetadata', () => {
      if (disposed || !Number.isFinite(video.duration)) return;
      mediaDuration = video.duration; start.max = Math.max(0, mediaDuration - 2);
      // Requested length is an upper bound. Keep the user's saved range intact;
      // the server clips at EOF and rejects fewer than two usable seconds.
      render();
    });
    video.addEventListener('error', () => { summary.textContent += ' This browser cannot preview this file; you can still enter its start time.'; });
    details.append(heading, video, actions); node.append(details);
  }
  render();
  return { node,
    setDisabled(value) { disabled = value; start.disabled = duration.disabled = value; if (useCurrent) useCurrent.disabled = value; },
    setValue(value) { start.value = value.start_seconds; duration.value = value.duration_seconds; setReferenceVideoRange(file, value); render(); },
    getValue() { return referenceVideoRange(file); },
    destroy() { disposed = true; if (video) { video.pause(); video.removeAttribute('src'); video.load(); } if (objectUrl) URL.revokeObjectURL(objectUrl); },
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
