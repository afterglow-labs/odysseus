// One catalogue is bundled by the native client and served to the desktop UI.
import { bindSettingsHelp, dismissSettingsHelp } from './settings/helpHints.js';

let cataloguePromise;
function loadCatalogue() {
  return cataloguePromise ||= fetch('/static/seed-hunter-help.json', { credentials: 'same-origin' })
    .then(response => { if (!response.ok) throw new Error('Help unavailable'); return response.json(); })
    .then(value => { if (value?.version !== 1 || !value.fields) throw new Error('Invalid help catalogue'); return value; })
    .catch(error => { cataloguePromise = null; throw error; });
}

export function seedHunterHelpText(catalogue, key, value) {
  const entry = catalogue?.fields?.[key];
  if (!entry) return '';
  const selected = entry.choices?.[String(value)];
  const choices = Object.entries(entry.choices || {}).filter(([choice]) => choice !== String(value));
  return [entry.body, selected && `Selected: ${selected.title}\n${selected.body}`,
    ...choices.map(([, choice]) => `${choice.title}\n${choice.body}`)].filter(Boolean).join('\n\n');
}

export function seedHunterHelpKey(control) {
  const id = control.id || '', label = control.getAttribute('aria-label') || '';
  if (id.startsWith('seed-hunter-input-')) return id.slice('seed-hunter-input-'.length);
  if (/^seed-hunter(?:-(?:final|queue))?-lora(?:_scale|_age)?(?:-\d+)?$/.test(id)) return /lora_(scale|age)/.test(id) ? 'lora_strength' : 'loras';
  if (id.startsWith('seed-hunter-option-')) return id.slice('seed-hunter-option-'.length);
  if (id.startsWith('seed-hunter-final-')) return 'final_' + id.slice('seed-hunter-final-'.length);
  if (id.startsWith('seed-hunter-')) return id.slice('seed-hunter-'.length);
  if (control.hasAttribute('data-batch-toggle')) return 'batch_job';
  if (control.hasAttribute('data-batch-auto-length')) return 'auto_video_length';
  if (control.hasAttribute('data-batch-files')) return 'batch_input';
  if (label.startsWith('Start at (seconds')) return 'reference_start';
  if (label.startsWith('Length (seconds)') || label.startsWith('Choose a section')) return 'reference_duration';
  if (label.startsWith('New ')) return label.slice(4).replaceAll(' ', '_');
  if (label.startsWith('Strength for ')) return 'refmod_strength';
  return { 'Enhancement model':'enhancement_model', 'Preview megapixels':'preview_megapixels',
    'Preview aspect ratio':'preview_aspect', 'Preview seconds':'preview_seconds',
    'Batch input':'batch_input', 'Number of copies':'batch_copies' }[label] || '';
}

export function bindSeedHunterHelp(modal, fieldOverrides = {}) {
  bindSettingsHelp(modal);
  const decorated = new Map();
  let catalogue = null, disposed = false;
  function attach(control, key, label) {
    if (!key || control.type === 'hidden') return;
    const existing = decorated.get(control);
    if (existing) { if (!existing.hint.isConnected) place(control, existing.hint); return; }
    const hint = document.createElement('button'); hint.type = 'button';
    hint.className = 'settings-help-hint h3-seed-hunter-help'; hint.dataset.seedHunterHelp = key;
    hint.dataset.helpMultiline = 'true'; hint.setAttribute('aria-expanded', 'false');
    const mark = document.createElement('span'); mark.setAttribute('aria-hidden', 'true'); mark.textContent = '?'; hint.append(mark);
    const update = () => {
      const entry = catalogue?.fields?.[key];
      hint.setAttribute('aria-label', `Help for ${label || entry?.title || key.replaceAll('_', ' ')}`);
      hint.dataset.helpHint = seedHunterHelpText(catalogue, key, control.value)
        || (catalogue ? 'Help for this option is unavailable. Refresh this client.' : 'Loading option help…');
    };
    update();
    for (const event of ['pointerover','focusin','click']) hint.addEventListener(event, update);
    control.addEventListener('change', update); decorated.set(control, {hint, update});
    place(control, hint);
  }
  function place(control, hint) {
    const wrap = control.closest('label');
    const title = wrap?.querySelector(':scope > span, :scope > strong')
      || (control.id && [...modal.querySelectorAll('label[for]')].find(node => node.htmlFor === control.id));
    if (title) title.append(hint);
    else control.insertAdjacentElement('afterend', hint);
  }
  function decorate() {
    for (const control of decorated.keys()) if (!control.isConnected) decorated.delete(control);
    for (const control of modal.querySelectorAll('input, select, textarea')) attach(control, seedHunterHelpKey(control));
    for (const control of modal.querySelectorAll('[data-seed-hunter-help-action]')) attach(control, control.dataset.seedHunterHelpAction, control.textContent);
  }
  const observer = new MutationObserver(decorate);
  observer.observe(modal, { childList: true, subtree: true });
  decorate();
  loadCatalogue().then(value => { if (!disposed) { catalogue = {...value,fields:{...value.fields,...fieldOverrides}}; decorated.forEach(({update}) => update()); } })
    .catch(() => { if (!disposed) for (const hint of modal.querySelectorAll('[data-seed-hunter-help]')) hint.dataset.helpHint = 'Option help could not be loaded. Close and reopen this workflow to retry.'; });
  return () => { disposed = true; observer.disconnect(); decorated.clear(); dismissSettingsHelp(); };
}
