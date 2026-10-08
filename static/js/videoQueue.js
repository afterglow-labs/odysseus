// Shared queue controls. Parameter patches deliberately exclude workflow modes
// and uploaded inputs: each job keeps its own media and unchecked settings.
const el = (tag, className = '', text) => {
  const node = document.createElement(tag); node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (label, action) => {
  const node = el('button', 'memory-toolbar-btn', label); node.type = 'button'; node.dataset.queueAction = action; return node;
};

export function videoQueuePatch(rows) {
  const patch = {};
  for (const { key, control, check, numeric, custom } of rows) {
    if (!check.checked) continue;
    if (['mode', 'workflow_id', 'inputs'].includes(key)) throw new Error('A bulk edit cannot replace a workflow or its inputs.');
    let value = custom ? custom.getValue() : control.type === 'checkbox' ? control.checked : control.value;
    if (numeric) {
      if (String(value).trim() === '' || !Number.isFinite(Number(value))) throw new Error(`Enter a number for ${key}.`);
      value = Number(value);
    }
    if (key.startsWith('components.')) (patch.components ||= {})[key.slice(11)] = value;
    else patch[key] = value;
  }
  return patch;
}

// Each descriptor wraps an existing draft control or describes another workflow's
// control. Clones preserve the selected value without changing the user's draft.
export function videoQueueField(key, label, control, numeric = false) {
  return { key, label, control, numeric };
}

export function createVideoQueueControls({ container, family, request, getFields, reload, onChange = () => {}, isClosed = () => false }) {
  const familyName = family === 'h3' ? 'H3' : 'BFS';
  let queue = null, busy = false, blocked = false, dead = false, mode = null, targets = [], rows = [];
  let requestKey = '', requestId = '', selectionVersion = 0, retryRerun = false;
  let singleJob = null, singleSubmit = null;
  const panel = el('section', 'video-queue-panel'); panel.setAttribute('aria-label', `${familyName} queue controls`);
  const heading = el('div', 'h3-video-toolbar'), title = el('h3', '', 'Queue controls');
  const pause = button('Pause queue', 'pause'); heading.append(title, pause); panel.appendChild(heading);
  const state = el('p', 'h3-video-muted', 'Checking queue…'); state.setAttribute('role', 'status'); panel.appendChild(state);
  panel.appendChild(el('p', 'h3-video-muted', 'Pause stops the next job from starting. A video already rendering finishes normally. Pause applies to your H3 and BFS jobs together.'));
  const actions = el('div', 'h3-video-job-actions');
  const scope = el('select', 'cookbook-field-input'); scope.setAttribute('aria-label', 'Queued job scope');
  scope.add(new Option(`${familyName} queued jobs`, 'family')); scope.add(new Option('All H3 and BFS queued jobs', 'all'));
  const cancel = button('Cancel queued jobs', 'cancel'), remove = button('Delete queued jobs', 'delete'); remove.classList.add('h3-video-delete-button');
  actions.append(scope, cancel, remove); panel.appendChild(actions);
  const edits = el('div', 'h3-video-job-actions video-queue-edit-actions');
  const edit = button('Edit queued parameters', 'edit'), rerun = button('Run again with new parameters', 'rerun'); edits.append(edit, rerun); panel.appendChild(edits);
  const message = el('p', 'h3-video-muted video-queue-message'); message.setAttribute('role', 'status'); message.hidden = true; panel.appendChild(message);
  const errors = el('p', 'h3-video-error'); errors.hidden = true; errors.setAttribute('role', 'alert'); panel.appendChild(errors);
  const confirmation = el('section', 'h3-video-delete-confirmation'); confirmation.hidden = true;
  const confirmationText = el('p'), confirmationActions = el('div', 'h3-video-job-actions');
  const keep = button('Keep queued jobs', 'keep'), confirm = button('Confirm', 'confirm'); confirmationActions.append(keep, confirm); confirmation.append(confirmationText, confirmationActions); panel.appendChild(confirmation);
  const editor = el('form', 'video-queue-editor'); editor.hidden = true;
  const editorTitle = el('h3'); editor.appendChild(editorTitle);
  const editorHelp = el('p', 'h3-video-muted'); editor.appendChild(editorHelp);
  const targetLabel = el('label', 'h3-video-field'); targetLabel.appendChild(el('span', '', 'Jobs to run again'));
  const targetScope = el('select', 'cookbook-field-input'); targetScope.setAttribute('aria-label', 'Jobs to run again');
  targetScope.add(new Option('Finished jobs (completed, failed or canceled)', 'finished'));
  targetScope.add(new Option('Queued jobs', 'queued')); targetScope.add(new Option('All jobs, including the current render', 'all'));
  targetScope.value = 'finished'; targetLabel.appendChild(targetScope); editor.appendChild(targetLabel);
  const targetStatus = el('p', 'h3-video-muted'); targetStatus.setAttribute('role', 'status'); editor.appendChild(targetStatus);
  const refreshSelection = button('Refresh selected jobs', 'refresh-selection'); editor.appendChild(refreshSelection);
  const targetNames = el('details', 'video-queue-targets'); targetNames.appendChild(el('summary', '', 'Review jobs'));
  const targetList = el('ul'); targetNames.appendChild(targetList); editor.appendChild(targetNames);
  const fieldHelp = el('p', 'h3-video-muted', 'Check only the parameters to change. Values start from your current draft or workflow defaults. Each job keeps its other settings, workflow, and saved images, videos and audio.'); editor.appendChild(fieldHelp);
  const fieldGrid = el('div', 'video-queue-fields'); editor.appendChild(fieldGrid);
  const approveLabel = el('label', 'video-queue-approval'), approve = el('input'); approve.type = 'checkbox';
  const approveText = el('span'); approveLabel.append(approve, approveText); editor.appendChild(approveLabel);
  const editActions = el('div', 'h3-video-job-actions');
  const abandon = button('Close parameter editor', 'close-editor'), apply = button('Apply', 'apply'); apply.type = 'submit';
  editActions.append(abandon, apply); editor.appendChild(editActions); panel.appendChild(editor); container.appendChild(panel);
  function alive() { return !dead && !isClosed(); }
  function error(text = '') { errors.textContent = text; errors.hidden = !text; }
  function notice(text = '') { message.textContent = text; message.hidden = !text; }
  function count() { return Number(scope.value === 'all' ? queue?.total_queued : queue?.queued) || 0; }
  function render() {
    const locked = busy || blocked;
    panel.setAttribute('aria-busy', String(busy));
    pause.textContent = queue?.paused ? 'Resume queue' : 'Pause queue'; pause.disabled = locked || !queue;
    pause.setAttribute('aria-pressed', String(!!queue?.paused));
    if (queue) state.textContent = `${queue.paused ? 'Paused' : 'Queue running'} · ${queue.queued || 0} ${familyName} queued · ${queue.total_queued || 0} queued across H3 and BFS · ${queue.total_running || 0} rendering`;
    scope.disabled = locked || !confirmation.hidden;
    cancel.disabled = remove.disabled = locked || !!mode || !queue || !count();
    edit.disabled = locked || !!mode || !queue?.queued; rerun.disabled = locked || !!mode;
    keep.disabled = confirm.disabled = locked;
    targetScope.disabled = refreshSelection.disabled = locked || retryRerun; abandon.disabled = busy;
    approve.disabled = locked || !targets.length;
    for (const row of rows) {
      row.check.disabled = locked || retryRerun || !!singleJob;
      const disabled = locked || retryRerun || (!singleJob && !row.check.checked);
      if (row.custom) row.custom.setDisabled(disabled);
      else row.control.disabled = disabled;
    }
    apply.textContent = retryRerun ? singleJob ? 'Retry request' : 'Retry remaining copies safely' : singleJob ? 'Queue edited copy' : mode === 'rerun' ? `Queue ${targets.length} new ${targets.length === 1 ? 'job' : 'jobs'}` : `Apply to ${targets.length} queued ${targets.length === 1 ? 'job' : 'jobs'}`;
    apply.disabled = locked || !targets.length || (!singleJob && !approve.checked)
      || ((mode === 'edit' || (singleJob && !retryRerun)) && !rows.some(row => row.check.checked));
  }
  function updateQueue(value) { if (value && typeof value.paused === 'boolean') queue = value; render(); }
  async function run(operation) {
    if (busy || blocked || !alive()) return;
    busy = true; error(); render(); onChange();
    try { await operation(); }
    catch (e) { if (alive()) error(e.message || String(e)); }
    finally { busy = false; if (alive()) { render(); onChange(); } }
  }
  function receipt(result, verb) {
    const changed = Number(result.succeeded ?? result.updated ?? result.rerun ?? result.canceled ?? result.deleted ?? 0);
    notice(`${changed} ${changed === 1 ? 'job' : 'jobs'} ${verb}.`);
    const failed = Number(result.failed || 0), details = result.errors || [];
    const texts = Array.isArray(details) ? details.map(item => typeof item === 'string' ? item : `${item.id || item.job_id || 'Job'}: ${item.error || item.message || item.reason || 'Could not complete action'}`) : [String(details)];
    if (failed || texts.length) error(`${failed || texts.length} ${failed === 1 ? 'job was' : 'jobs were'} not changed.\n${texts.join('\n')}`);
    updateQueue(result.queue);
  }
  const post = (path, body) => request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  pause.onclick = () => run(async () => {
    const paused = !queue.paused, result = await post('/queue/pause', { paused }); if (!alive()) return;
    updateQueue(result.queue || result); notice(paused ? 'Queue paused. The current render will finish; waiting jobs will stay queued.' : 'Queue resumed. Waiting jobs can start.'); await reload();
  });
  function confirmAction(action) {
    if (busy || blocked) return;
    const noun = scope.value === 'all' ? 'H3 and BFS' : familyName;
    confirmation.hidden = false; confirmation.dataset.action = action;
    confirmationText.textContent = action === 'delete'
      ? `Permanently delete all ${noun} jobs that are still queued (currently ${count()}), including their saved input files and prompts? This cannot be undone. Running and finished jobs are kept.`
      : `Cancel all ${noun} jobs that are still queued (currently ${count()})? Their records and inputs are kept so you can run them again. The current render continues.`;
    confirm.textContent = action === 'delete' ? 'Delete queued jobs permanently' : 'Cancel all queued jobs';
    render(); keep.focus();
  }
  cancel.onclick = () => confirmAction('cancel'); remove.onclick = () => confirmAction('delete');
  keep.onclick = () => { confirmation.hidden = true; render(); };
  confirm.onclick = () => run(async () => {
    const action = confirmation.dataset.action, result = await post(`/queue/${action}`, { scope: scope.value }); if (!alive()) return;
    confirmation.hidden = true; receipt(result, action === 'delete' ? 'deleted' : 'canceled'); await reload();
  });
  function buildFields(savedConfig = null) {
    rows = []; fieldGrid.replaceChildren();
    const seen = new Set();
    for (const descriptor of getFields(savedConfig)) {
      const { key, label, control: original, numeric } = descriptor;
      if ((!original && !descriptor.create) || seen.has(key) || ['mode', 'workflow_id', 'inputs'].includes(key)) continue;
      const savedValue = savedConfig ? descriptor.getSavedValue ? descriptor.getSavedValue(savedConfig)
        : key.startsWith('components.') ? savedConfig.components?.[key.slice(11)] : savedConfig[key] : undefined;
      if (savedConfig && savedValue === undefined) continue;
      seen.add(key);
      const row = el('div', 'video-queue-field'), toggle = el('label', 'video-queue-field-toggle'), check = el('input'); check.type = 'checkbox'; check.dataset.queueField = key;
      check.hidden = !!singleJob;
      check.setAttribute('aria-label', `Change ${label}`); toggle.append(check, el('span', '', label));
      if (descriptor.create) {
        const custom = descriptor.create(() => { if (singleJob) check.checked = true; approve.checked = false; render(); });
        if (savedConfig) custom.setValue(savedValue);
        row.classList.add('video-queue-field-wide'); row.append(toggle, custom.node);
        check.onchange = () => { approve.checked = false; render(); };
        fieldGrid.appendChild(row); rows.push({ key, check, custom });
        continue;
      }
      const control = original.cloneNode(true); control.removeAttribute('id'); control.removeAttribute('name'); control.classList.add('cookbook-field-input');
      control.value = original.value; if (original.type === 'checkbox') control.checked = original.checked;
      if (savedConfig) {
        if (control.type === 'checkbox') control.checked = !!savedValue;
        else {
          if (control.tagName === 'SELECT' && ![...control.options].some(option => option.value === String(savedValue))) control.add(new Option('Saved value (currently unavailable)', String(savedValue)));
          control.value = String(savedValue ?? '');
        }
      }
      control.setAttribute('aria-label', `New ${label}`); control.dataset.queueValue = key; control.disabled = true;
      check.onchange = () => { approve.checked = false; render(); };
      control.addEventListener('input', () => { if (singleJob) check.checked = true; approve.checked = false; render(); });
      control.addEventListener('change', () => { if (singleJob) check.checked = true; approve.checked = false; render(); });
      row.append(toggle, control); fieldGrid.appendChild(row); rows.push({ key, control, check, numeric });
    }
  }
  async function loadTargets() {
    const version = ++selectionVersion;
    targets = []; approve.checked = false; targetList.replaceChildren(); targetStatus.textContent = 'Loading job selection…'; render();
    const result = await request(`/queue/jobs?status=${mode === 'edit' ? 'queued' : targetScope.value}`);
    if (!alive() || version !== selectionVersion) return;
    if (!Array.isArray(result.jobs) || result.jobs.some(job => !job.id || !Number.isInteger(job.revision))) throw new Error('The server returned an invalid job selection. Refresh and retry.');
    targets = result.jobs; updateQueue(result.queue);
    targetStatus.textContent = `${targets.length} ${familyName} ${mode === 'edit' ? 'queued ' : ''}${targets.length === 1 ? 'job' : 'jobs'} selected.${mode === 'edit' ? ' Pause the queue first to keep waiting jobs from starting while you edit.' : ' Original jobs and results are preserved; each copy goes to the end of the queue.'}`;
    approveText.textContent = mode === 'rerun' ? `Create ${targets.length} new jobs using these saved inputs and checked settings.` : `Change the checked parameters on these ${targets.length} queued jobs.`;
    for (const job of targets) targetList.appendChild(el('li', '', `${job.source_name || job.id} · ${job.status}`));
    render();
  }
  function closeEditor() { selectionVersion++; retryRerun = false; requestKey = ''; requestId = ''; singleJob = null; singleSubmit = null; mode = null; targets = []; editor.hidden = true; approve.checked = false; render(); onChange(); }
  function openEditor(action) {
    run(async () => {
      mode = action; editor.hidden = false; confirmation.hidden = true; notice(); targetScope.value = 'finished'; buildFields();
      approveLabel.hidden = false; refreshSelection.hidden = false; targetNames.hidden = false;
      fieldHelp.textContent = 'Check only the parameters to change. Values start from your current draft or workflow defaults. Each job keeps its other settings, workflow, and saved images, videos and audio.';
      editorTitle.textContent = action === 'edit' ? `Edit all queued ${familyName} jobs` : `Run ${familyName} jobs again`;
      editorHelp.textContent = action === 'edit' ? 'Update waiting jobs in place while keeping their queue order. Running and finished jobs are unchanged.' : 'Reuse saved inputs without uploading again. Choose the original jobs below and adjust any parameters before adding fresh copies to the queue.';
      targetLabel.hidden = action !== 'rerun'; await loadTargets(); editor.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  }
  edit.onclick = () => openEditor('edit'); rerun.onclick = () => openEditor('rerun');
  function openRerun(id, submit) {
    if (mode || typeof submit !== 'function') return;
    return run(async () => {
      const result = await request('/queue/jobs?status=finished');
      if (!alive()) return;
      const job = result.jobs?.find(item => item.id === id);
      if (!job || !Number.isInteger(job.revision) || !job.config || !['completed', 'failed', 'stopped'].includes(job.status)) throw new Error('This finished job is unavailable. Refresh jobs and retry.');
      singleJob = job; singleSubmit = submit; mode = 'rerun'; targets = [job]; retryRerun = false;
      editor.hidden = false; confirmation.hidden = true; notice(); updateQueue(result.queue);
      targetLabel.hidden = approveLabel.hidden = refreshSelection.hidden = targetNames.hidden = true;
      editorTitle.textContent = `Edit & rerun · ${job.source_name || job.id}`;
      editorHelp.textContent = 'These are this job’s saved parameters. Edit them to queue a new copy; its existing result and your current draft stay unchanged.';
      fieldHelp.textContent = 'Change a parameter, then choose Queue edited copy. Saved images, videos and audio are reused automatically; opening this editor does not start a job.';
      targetStatus.textContent = `One ${familyName} job selected · ${job.id}`;
      buildFields(job.config); render(); editor.scrollIntoView({ block: 'start', behavior: 'smooth' });
      editor.querySelector('textarea')?.focus({ preventScroll: true });
    });
  }
  targetScope.onchange = () => run(loadTargets); refreshSelection.onclick = () => run(loadTargets); approve.onchange = render; abandon.onclick = () => { if (!busy) closeEditor(); };
  editor.onsubmit = event => {
    event.preventDefault(); if (apply.disabled || !editor.reportValidity()) return;
    run(async () => {
      const patch = videoQueuePatch(rows), selection = targets.map(({ id, revision }) => ({ id, revision }));
      if (singleJob) {
        const outcome = await singleSubmit(singleJob, patch);
        if (!alive()) return;
        if (outcome?.result) { closeEditor(); receipt(outcome.result, 'added as a new copy'); }
        else { retryRerun = !!outcome?.pending; error(outcome?.error || 'Could not queue this job again.'); }
        return;
      }
      if (mode === 'edit' && !Object.keys(patch).length) throw new Error('Check at least one parameter to change.');
      const operation = mode, body = { patch, jobs: selection };
      if (operation === 'rerun') {
        const key = JSON.stringify(body);
        if (key !== requestKey) { requestKey = key; requestId = globalThis.crypto.randomUUID(); }
        body.request_id = requestId;
      }
      let result;
      try { result = await post(`/queue/${operation}`, body); }
      catch (error) {
        if (operation === 'rerun' && (!error.status || error.status >= 500)) {
          retryRerun = true;
          notice('The response was interrupted. Retry this same request safely to finish creating copies without duplicating any already accepted. Close the editor to abandon this request.');
        }
        throw error;
      }
      if (!alive()) return;
      if (operation === 'rerun' && Number(result.failed || 0) > 0) {
        retryRerun = true;
        targetStatus.textContent = 'Some copies could not be created. Retry this same request to finish; already accepted copies will not be duplicated. Close the editor to abandon this request.';
      } else closeEditor();
      receipt(result, operation === 'rerun' ? 'added as new copies' : 'updated');
      await reload();
    });
  };
  scope.onchange = render; render();
  return {
    get busy() { return busy; }, get editing() { return !!mode; },
    update(value) { if (!busy) updateQueue(value); },
    setBlocked(value) { blocked = !!value; render(); },
    destroy() { dead = true; selectionVersion++; },
    openRerun,
  };
}
