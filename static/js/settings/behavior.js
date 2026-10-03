// Per-user interaction preferences; keep these separate from server settings.
const URL = '/api/prefs/tooltip-trigger';
let tooltipTrigger = 'hover';
let loaded = false;
let pendingLoad = null;
let revision = 0;

export function getTooltipTrigger() {
  return tooltipTrigger;
}

function applyTrigger(value) {
  tooltipTrigger = value;
  document.dispatchEvent(new CustomEvent('odysseus:tooltip-trigger-change', {
    detail: { value },
  }));
}

export function loadTooltipTrigger() {
  if (loaded) return Promise.resolve(tooltipTrigger);
  if (pendingLoad) return pendingLoad;
  const startingRevision = revision;
  pendingLoad = (async () => {
    const response = await fetch(URL, { credentials: 'same-origin' });
    if (!response.ok) throw new Error('Could not load tooltip preference');
    const data = await response.json();
    // A slow initial read must not undo a choice already saved by the user.
    if (revision === startingRevision) applyTrigger(data?.value === 'click' ? 'click' : 'hover');
    loaded = true;
    return tooltipTrigger;
  })().finally(() => { pendingLoad = null; });
  return pendingLoad;
}

export async function saveTooltipTrigger(value) {
  if (value !== 'hover' && value !== 'click') throw new Error('Invalid tooltip preference');
  const response = await fetch(URL, {
    method: 'PUT',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  });
  if (!response.ok) throw new Error('Could not save tooltip preference');
  revision += 1;
  loaded = true;
  applyTrigger(value);
  return tooltipTrigger;
}
