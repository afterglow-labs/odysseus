// Shared, owner-scoped history for complete workflow prompts. Typing never
// writes history: successful jobs are recorded by the server, and Save current
// is the explicit action for an unsubmitted draft.
const API = '/api/workflows/prompt-history';
const SELECTOR = '[data-workflow-prompt],textarea[name="prompt"],[data-queue-value="prompt"]';
const make = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
let nextId = 0;

async function request(options = {}, offset = 0) {
  const response = await fetch(API + (options.method ? '' : `?limit=100&offset=${offset}`), { credentials: 'same-origin', ...options });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Prompt history is unavailable (${response.status}).`);
  return data;
}

function attach(input, workflow) {
  const node = make('div'); node.className = 'workflow-prompt-history';
  node.setAttribute('aria-label', 'Saved workflow prompts');
  const controls = make('div'); controls.className = 'workflow-prompt-history-controls';
  const select = make('select'); select.className = 'cookbook-field-input'; select.setAttribute('aria-label', 'Prompt history');
  const refresh = make('button', 'Refresh'); refresh.type = 'button'; refresh.className = 'memory-toolbar-btn'; refresh.setAttribute('aria-label', 'Refresh prompt history');
  const save = make('button', 'Save current'); save.type = 'button'; save.className = 'memory-toolbar-btn'; save.setAttribute('aria-label', 'Save current prompt');
  const status = make('p'); status.className = 'workflow-prompt-history-status'; status.setAttribute('role', 'status'); status.hidden = true;
  controls.append(select, refresh, save); node.append(controls, status);

  // Keep the input's accessible label limited to its own title, rather than
  // adding the dropdown/button text to "Prompt". Pipeline layouts move this
  // same field container, so its history controls follow it automatically.
  let host = input.parentElement;
  if (host.tagName === 'LABEL') {
    const label = host; host = make('div'); host.className = label.className; label.className = '';
    if (!input.id) input.id = `workflow-prompt-${++nextId}`;
    label.htmlFor = input.id; label.replaceWith(host); input.remove(); host.append(label, input);
  }
  input.after(node);
  let rows = [], nextOffset = null, loading = false, saving = false, dead = false, generation = 0;
  const clean = value => String(value || '').replaceAll('\r\n', '\n').replaceAll('\r', '\n').trim();
  const locked = () => input.readOnly || input.matches(':disabled');
  function notice(message = '', error = false) { status.textContent = message; status.hidden = !message; status.classList.toggle('h3-video-error', error); }
  function update() { select.disabled = loading || saving || locked(); refresh.disabled = loading || saving; save.disabled = loading || saving || locked(); }
  function render() {
    select.replaceChildren(new Option(loading ? 'Loading prompt history…' : rows.length ? 'Choose a saved prompt…' : 'No saved prompts yet', ''));
    for (const row of rows) { const text = row.prompt.replace(/\s+/g, ' '); const option = new Option(text.length > 110 ? text.slice(0,107) + '…' : text, row.id); option.title = row.prompt; select.add(option); }
    if (nextOffset !== null) select.add(new Option('Load more saved prompts…', '__more__'));
    update();
  }
  async function load(append = false) {
    const token = ++generation, offset = append ? nextOffset : 0;
    if (offset === null) return;
    loading = true; notice(); render();
    try {
      const data = await request({}, offset); if (dead || token !== generation) return;
      if (!Array.isArray(data.prompts)) throw new Error('The server returned invalid prompt history. Try Refresh.');
      const unique = new Set(); rows = [...(append ? rows : []), ...data.prompts].filter(row => {
        const value = clean(row.prompt); if (!row.id || typeof row.prompt !== 'string' || !value || unique.has(value)) return false; unique.add(value); return true;
      });
      nextOffset = Number.isInteger(data.next_offset) && data.next_offset > offset ? data.next_offset : null;
    } catch (error) { if (!dead && token === generation) notice(error.message, true); }
    finally { if (!dead && token === generation) { loading = false; render(); } }
  }
  select.addEventListener('change', event => {
    event.stopPropagation(); const value = select.value; select.value = '';
    if (value === '__more__') { load(true); return; }
    const row = rows.find(item => item.id === value); if (!row || locked()) return;
    if (input.maxLength > 0 && row.prompt.length > input.maxLength) { notice(`This prompt exceeds this workflow’s ${input.maxLength.toLocaleString()} character limit. Your draft was kept.`, true); return; }
    const previous = input.value; input.value = row.prompt;
    if (input.value !== row.prompt) { input.value = previous; notice('This field cannot keep the saved prompt’s formatting. Your draft was kept.', true); return; }
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    notice('Saved prompt applied. Review it before submitting.'); input.focus({ preventScroll: true }); update();
  });
  // History controls must not dirty the workflow merely by opening the list.
  select.addEventListener('input', event => event.stopPropagation());
  refresh.onclick = () => load();
  save.onclick = async () => {
    if (save.disabled || locked()) return;
    if (!input.value.trim()) { notice('Enter a prompt before saving it.', true); return; }
    const prompt = input.value; saving = true; notice(); update();
    try {
      const data = await request({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prompt, workflow }) });
      if (dead) return;
      if (!data.entry?.id || typeof data.entry.prompt !== 'string') throw new Error('The server did not confirm that the prompt was saved. Try again.');
      await load(); if (!dead) notice(data.created === false ? 'Prompt is already saved; no duplicate added.' : 'Prompt saved for all workflows.');
    } catch (error) { if (!dead) notice(error.message, true); }
    finally { if (!dead) { saving = false; update(); } }
  };
  input.addEventListener('input', update); input.addEventListener('change', update);
  const disabled = new MutationObserver(update); disabled.observe(input, { attributes: true, attributeFilter: ['disabled', 'readonly'] });
  for (let parent = input.parentElement; parent; parent = parent.parentElement) if (parent.tagName === 'FIELDSET') disabled.observe(parent, { attributes: true, attributeFilter: ['disabled'] });
  render(); load();
  return { refresh() { if (!saving) load(); }, destroy() { dead = true; generation++; disabled.disconnect(); input.removeEventListener('input', update); input.removeEventListener('change', update); node.remove(); } };
}

// Observe cloned prompt controls in Edit & rerun/bulk editors as well as the
// original form. No settings, file inputs, negative prompts or dialogue helper
// fragments are included. Removed editors release their observers.
export function bindWorkflowPromptHistory(root, { workflow }) {
  if (!document.getElementById('workflow-prompt-history-styles')) {
    const css = make('link'); css.id = 'workflow-prompt-history-styles'; css.rel = 'stylesheet'; css.href = '/static/workflow-prompt-history.css'; document.head.append(css);
  }
  const controls = new Map();
  let firstJobId = root.querySelector('[data-job-id]')?.dataset.jobId || null;
  function scan() {
    for (const [input, control] of controls) if (!root.contains(input)) { control.destroy(); controls.delete(input); }
    // A new accepted job is already recorded in server history. Refresh once
    // when that receipt enters the list, never on every progress/log mutation.
    const currentJobId = root.querySelector('[data-job-id]')?.dataset.jobId || null;
    if (currentJobId !== firstJobId) { firstJobId = currentJobId; for (const control of controls.values()) control.refresh(); }
    for (const input of root.querySelectorAll(SELECTOR)) if (!controls.has(input)) controls.set(input, attach(input, workflow));
  }
  const observer = new MutationObserver(scan); observer.observe(root, { childList: true, subtree: true }); scan();
  return { destroy() { observer.disconnect(); for (const control of controls.values()) control.destroy(); controls.clear(); } };
}
