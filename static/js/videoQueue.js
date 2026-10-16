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

export function createVideoQueueControls({ container, family, request, getFields, reload, onChange = () => {}, isClosed = () => false, filterJobs = () => true, targetName = null, historyContainer = null, historyFilter = {} }) {
  const familyName = family === 'h3' ? 'H3' : 'BFS';
  const selectedName = targetName || familyName;
  let queue = null, busy = false, blocked = false, dead = false, mode = null, targets = [], rows = [];
  let requestKey = '', requestId = '', selectionVersion = 0, retryRerun = false;
  let singleJob = null, singleSubmit = null;
  let historySnapshot = null, historyRetryErrors = false, historyOperation = false, confirmationTrigger = null;
  const panel = el('section', 'video-queue-panel'); panel.setAttribute('aria-label', `${familyName} queue controls`);
  const heading = el('div', 'h3-video-toolbar'), title = el('h3', '', 'Queue controls');
  const pause = button('Pause queue', 'pause'); heading.append(title, pause); panel.appendChild(heading);
  const state = el('p', 'h3-video-muted', 'Checking queue…'); state.setAttribute('role', 'status'); panel.appendChild(state);
  const help = el('details', 'h3-workflow-help'); help.append(el('summary', '', 'Queue help'), el('p', 'h3-video-muted', 'Jobs render one at a time across H3 and BFS. Pause prevents the next job from starting; the current render finishes. Closing this workflow leaves queued jobs running. Each job keeps its saved inputs and settings.')); panel.appendChild(help);
  const actions = el('div', 'h3-video-job-actions');
  const scope = el('select', 'cookbook-field-input'); scope.setAttribute('aria-label', 'Queued job scope');
  scope.add(new Option(`${familyName} queued jobs`, 'family')); scope.add(new Option('All H3 and BFS queued jobs', 'all'));
  const cancel = button('Cancel queued jobs', 'cancel'), remove = button('Delete queued jobs', 'delete'); remove.classList.add('h3-video-delete-button');
  actions.append(scope, cancel, remove); panel.appendChild(actions);
  const edits = el('div', 'h3-video-job-actions video-queue-edit-actions');
  const edit = button('Edit queued parameters', 'edit'), rerun = button('Run again with new parameters', 'rerun'); edits.append(edit, rerun); panel.appendChild(edits);
  const message = el('p', 'h3-video-muted video-queue-message'); message.setAttribute('role', 'status'); message.hidden = true; panel.appendChild(message);
  const errors = el('p', 'h3-video-error'); errors.hidden = true; errors.setAttribute('role', 'alert'); panel.appendChild(errors);
  const history = el('section', 'video-history-actions'); history.setAttribute('aria-label', 'Delete job history');
  const historyActions = el('div', 'h3-video-job-actions');
  const historyScope = el('select', 'cookbook-field-input'); historyScope.setAttribute('aria-label', 'Job history deletion scope');
  historyScope.add(new Option(`${selectedName} job history`, 'workflow')); historyScope.add(new Option('All H3 and BFS job history', 'all'));
  const deleteHistory = button('Delete all job history', 'delete-history'); deleteHistory.classList.add('h3-video-delete-button');
  historyActions.append(historyScope, deleteHistory);
  const historyMessage = el('p', 'h3-video-muted video-queue-message'); historyMessage.hidden = true; historyMessage.setAttribute('role', 'status');
  const historyError = el('p', 'h3-video-error'); historyError.hidden = true; historyError.setAttribute('role', 'alert');
  history.append(historyActions, historyMessage, historyError); (historyContainer || panel).appendChild(history);
  const confirmation = el('dialog', 'video-queue-confirmation'); confirmation.hidden = true;
  const confirmationTitle = el('h3'), confirmationText = el('p'), confirmationActions = el('div', 'h3-video-job-actions');
  const confirmationProgress = el('p', 'h3-video-muted video-queue-message'); confirmationProgress.hidden = true; confirmationProgress.setAttribute('role', 'status');
  const confirmationError = el('p', 'h3-video-error'); confirmationError.hidden = true; confirmationError.setAttribute('role', 'alert');
  const keep = button('Keep queued jobs', 'keep'), confirm = button('Confirm', 'confirm'); confirmationActions.append(keep, confirm); confirmation.append(confirmationText, confirmationActions); (document.body || panel).appendChild(confirmation);
  confirm.classList.add('h3-video-delete-button'); confirmation.replaceChildren(confirmationTitle, confirmationText, confirmationProgress, confirmationError, confirmationActions);
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
  function error(text = '') {
    errors.textContent = text; errors.hidden = !text;
    confirmationError.textContent = text; confirmationError.hidden = !text;
    if (historyOperation) { historyError.textContent = text; historyError.hidden = !text; }
  }
  function notice(text = '') {
    message.textContent = text; message.hidden = !text;
    confirmationProgress.textContent = text; confirmationProgress.hidden = !text;
    if (historyOperation) { historyMessage.textContent = text; historyMessage.hidden = !text; }
  }
  function count() { return Number(scope.value === 'all' ? queue?.total_queued : queue?.queued) || 0; }
  function render() {
    const locked = busy || blocked;
    panel.setAttribute('aria-busy', String(busy));
    pause.textContent = queue?.paused ? 'Resume queue' : 'Pause queue'; pause.disabled = locked || !queue;
    pause.setAttribute('aria-pressed', String(!!queue?.paused));
    if (queue) state.textContent = `${queue.paused ? 'Paused' : 'Queue running'} · ${queue.queued || 0} ${familyName} queued · ${queue.total_queued || 0} queued across H3 and BFS · ${queue.total_running || 0} rendering${blocked ? ' · Queue changes are unavailable while another workflow operation is in progress.' : ''}`;
    scope.disabled = locked || !confirmation.hidden;
    historyScope.disabled = deleteHistory.disabled = busy || !!mode || !confirmation.hidden;
    history.setAttribute('aria-busy', String(busy && historyOperation));
    confirmation.setAttribute('aria-busy', String(busy));
    cancel.disabled = remove.disabled = locked || !!mode || !queue || !count();
    edit.disabled = locked || !!mode || !queue?.queued; rerun.disabled = locked || !!mode;
    keep.disabled = confirm.disabled = busy || (blocked && !historyOperation);
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
  async function run(operation, allowBlocked = false) {
    if (busy || (blocked && !allowBlocked) || !alive()) return;
    busy = true; error(); render(); onChange();
    try { return await operation(); }
    catch (e) { if (alive()) error(e.message || String(e)); return { error: e.message || String(e) }; }
    finally { busy = false; if (alive()) { render(); onChange(); if (!confirmation.hidden && confirmationProgress.hidden) keep.focus(); } }
  }
  function receipt(result, verb) {
    const changed = Number(result.succeeded ?? result.updated ?? result.rerun ?? result.canceled ?? result.deleted ?? 0);
    notice(`${changed} ${changed === 1 ? 'job' : 'jobs'} ${verb}.${result.message ? ` ${result.message}` : ''}`);
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
    historyOperation = false; historySnapshot = null; historyRetryErrors = false;
    const noun = scope.value === 'all' ? 'H3 and BFS' : familyName;
    confirmation.dataset.action = action;
    confirmationTitle.textContent = action === 'delete' ? 'Delete queued jobs?' : 'Cancel queued jobs?';
    confirmationText.textContent = action === 'delete'
      ? `Permanently delete all ${noun} jobs that are still queued (currently ${count()}), including their saved input files and prompts? This cannot be undone. Running and finished jobs are kept.`
      : `Cancel all ${noun} jobs that are still queued (currently ${count()})? Their records and inputs are kept so you can run them again. The current render continues.`;
    confirm.textContent = action === 'delete' ? 'Delete queued jobs permanently' : 'Cancel all queued jobs';
    keep.textContent = 'Keep queued jobs'; openConfirmation(action === 'delete' ? remove : cancel);
  }
  function openConfirmation(trigger) {
    confirmationTrigger = trigger; error(); notice(); confirmation.hidden = false;
    confirmation.setAttribute('aria-label', confirmationTitle.textContent);
    confirmation.showModal?.(); render(); keep.focus();
  }
  function closeConfirmation() {
    confirmation.close?.(); confirmation.hidden = true; render(); confirmationTrigger?.focus();
  }
  confirmation.addEventListener('cancel', event => { event.preventDefault(); if (!busy) closeConfirmation(); });
  const onConfirmationKey = event => {
    if (!confirmation.hidden && event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); if (!busy) closeConfirmation();
    }
  };
  // Run before the application's document-level Escape arbiter, which otherwise
  // dismisses the underlying workflow and loses its draft.
  globalThis.window?.addEventListener('keydown', onConfirmationKey, true);
  deleteHistory.onclick = () => run(async () => {
    historyOperation = true; historyRetryErrors = false; historySnapshot = null; error(); notice('Counting finished jobs across every page…');
    const body = historyScope.value === 'all' ? { scope: 'all' } : { scope: 'workflow', ...historyFilter };
    const snapshot = await post('/history/delete-preview', body); if (!alive()) return;
    if (!snapshot.snapshot_id || !Number.isInteger(snapshot.count) || snapshot.count < 0) throw new Error('Could not confirm which jobs would be deleted. Refresh jobs and try again.');
    if (!snapshot.count) { notice(`No finished jobs to delete in this scope. ${Number(snapshot.preserved_active || 0)} queued or running jobs kept.`); return; }
    historySnapshot = snapshot; confirmation.dataset.action = 'history'; confirmationTitle.textContent = 'Delete all job history?';
    const counts = snapshot.counts || {}, scopeName = historyScope.value === 'all' ? 'all H3 and BFS workflows' : selectedName;
    confirmationText.textContent = `Permanently delete ${snapshot.count} finished ${snapshot.count === 1 ? 'job' : 'jobs'} from ${scopeName}, across every page? ${Number(counts.completed || 0)} completed, ${Number(counts.failed || 0)} failed, ${Number(counts.stopped || 0)} stopped. This removes their saved inputs, previews, output videos and gallery entries. This cannot be undone. ${Number(snapshot.preserved_active || 0)} queued or running jobs are kept.`;
    keep.textContent = 'Keep job history'; confirm.textContent = `Delete ${snapshot.count} finished jobs permanently`;
    openConfirmation(deleteHistory);
  }, true);
  cancel.onclick = () => confirmAction('cancel'); remove.onclick = () => confirmAction('delete');
  keep.onclick = () => { if (!busy) closeConfirmation(); };
  confirm.onclick = () => run(async () => {
    if (confirmation.dataset.action === 'history') {
      if (!historySnapshot) throw new Error('The deletion selection is unavailable. Close this confirmation and select Delete all job history again.');
      const snapshot = historySnapshot; confirm.textContent = 'Deleting job history…'; keep.textContent = 'Close';
      notice(`Deleting finished job history… 0 / ${snapshot.count} checked. Queued and running jobs are kept.`);
      try {
        let retryErrors = historyRetryErrors, previousProcessed = 0;
        while (alive()) {
          historyRetryErrors = false;
          const result = await post('/history/delete', { snapshot_id: snapshot.snapshot_id, confirm: true, ...(retryErrors ? { retry_errors: true } : {}) });
          retryErrors = false; if (!alive()) return;
          if (!result || result.snapshot_id !== snapshot.snapshot_id || result.total !== snapshot.count
            || !['total', 'processed', 'pending', 'deleted', 'skipped'].every(key => Number.isInteger(result[key]) && result[key] >= 0)
            || typeof result.done !== 'boolean' || !Array.isArray(result.errors)
            || !result.errors.every(item => item && typeof item.id === 'string' && typeof item.error === 'string')
            || result.processed + result.pending !== result.total || result.done !== (result.pending === 0)
            || result.deleted + result.skipped + result.errors.length !== result.processed) {
            throw new Error('The server returned an incomplete deletion receipt. Retry this same selection to check its progress safely.');
          }
          const { deleted, skipped, errors: details, processed } = result;
          if (!result.done && processed <= previousProcessed) throw new Error('Deletion stopped making progress. Retry this same selection shortly.');
          previousProcessed = processed;
          notice(`${deleted} finished ${deleted === 1 ? 'job' : 'jobs'} deleted · ${processed} / ${result.total} checked.${skipped ? ` ${skipped} changed or unavailable jobs skipped.` : ''} ${Number(result.preserved_active ?? snapshot.preserved_active ?? 0)} queued or running jobs kept.`);
          if (!result.done) continue;
          historyRetryErrors = details.length > 0;
          if (historyRetryErrors) {
            error(`${details.length} jobs could not be deleted. Retry failed deletions, or close and try again later.\n${details.map(item => `${item.id || 'Job'}: ${item.error || 'Could not delete job'}`).join('\n')}`);
            confirm.textContent = 'Retry failed deletions';
          } else { historySnapshot = null; closeConfirmation(); }
          await reload(); return;
        }
      } catch (failure) {
        confirm.textContent = 'Retry job history deletion';
        notice('Deletion was interrupted. Retry this same selection safely; jobs already deleted will not be deleted again.');
        throw failure;
      }
      return;
    }
    historyOperation = false;
    const label = confirmation.dataset.action === 'delete' ? 'Deleting queued jobs…' : 'Canceling queued jobs…'; notice(label); confirm.textContent = label;
    const action = confirmation.dataset.action;
    let result;
    try { result = await post(`/queue/${action}`, { scope: scope.value }); }
    catch (failure) { confirm.textContent = action === 'delete' ? 'Retry deleting queued jobs' : 'Retry canceling queued jobs'; throw failure; }
    if (!alive()) return;
    closeConfirmation(); receipt(result, action === 'delete' ? 'deleted' : 'canceled'); await reload();
  }, historyOperation);
  function buildFields(savedConfig = null, savedJob = null) {
    rows = []; fieldGrid.replaceChildren();
    const seen = new Set();
    for (const descriptor of getFields(savedConfig, savedJob)) {
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
    targets = result.jobs.filter(filterJobs); updateQueue(result.queue);
    targetStatus.textContent = `${targets.length} ${selectedName} ${mode === 'edit' ? 'queued ' : ''}${targets.length === 1 ? 'job' : 'jobs'} selected.${mode === 'edit' ? ' Pause the queue first to keep waiting jobs from starting while you edit.' : ' Original jobs and results are preserved; each copy goes to the end of the queue.'}`;
    approveText.textContent = mode === 'rerun' ? `Create ${targets.length} new jobs using these saved inputs and checked settings.` : `Change the checked parameters on these ${targets.length} queued jobs.`;
    for (const job of targets) targetList.appendChild(el('li', '', `${job.source_name || job.id} · ${job.status}`));
    render();
  }
  function closeEditor() { selectionVersion++; retryRerun = false; requestKey = ''; requestId = ''; singleJob = null; singleSubmit = null; mode = null; targets = []; editor.hidden = true; approve.checked = false; render(); onChange(); }
  function openEditor(action) {
    run(async () => {
      mode = action; editor.hidden = false; closeConfirmation(); notice(); targetScope.value = 'finished'; buildFields();
      approveLabel.hidden = false; refreshSelection.hidden = false; targetNames.hidden = false;
      fieldHelp.textContent = 'Check only the parameters to change. Values start from your current draft or workflow defaults. Each job keeps its other settings, workflow, and saved images, videos and audio.';
      editorTitle.textContent = action === 'edit' ? `Edit queued ${selectedName} jobs` : `Run ${selectedName} jobs again`;
      editorHelp.textContent = action === 'edit' ? 'Update waiting jobs in place while keeping their queue order. Running and finished jobs are unchanged.' : 'Reuse saved inputs without uploading again. Choose the original jobs below and adjust any parameters before adding fresh copies to the queue.';
      targetLabel.hidden = action !== 'rerun'; await loadTargets(); editor.scrollIntoView({ block: 'start', behavior: 'smooth' });
    });
  }
  edit.onclick = () => openEditor('edit'); rerun.onclick = () => openEditor('rerun');
  function openRerun(id, submit) {
    if (!alive() || busy || blocked || mode || typeof submit !== 'function') return Promise.resolve({ error: 'Finish the current workflow operation or close the parameter editor, then try Edit & rerun again.' });
    return run(async () => {
      const result = await request('/queue/jobs?status=finished');
      if (!alive()) return;
      const job = result.jobs?.filter(filterJobs).find(item => item.id === id);
      if (!job || !Number.isInteger(job.revision) || !job.config || !['completed', 'failed', 'stopped'].includes(job.status)) throw new Error('This finished job is unavailable. Refresh jobs and retry.');
      singleJob = job; singleSubmit = submit; mode = 'rerun'; targets = [job]; retryRerun = false;
      editor.hidden = false; closeConfirmation(); notice(); updateQueue(result.queue);
      targetLabel.hidden = approveLabel.hidden = refreshSelection.hidden = targetNames.hidden = true;
      editorTitle.textContent = `Edit & rerun · ${job.source_name || job.id}`;
      editorHelp.textContent = 'These are this job’s saved parameters. Edit them to queue a new copy; its existing result and your current draft stay unchanged.';
      fieldHelp.textContent = 'Change a parameter, then choose Queue edited copy. Saved images, videos and audio are reused automatically; opening this editor does not start a job.';
      targetStatus.textContent = `One ${selectedName} job selected · ${job.id}`;
      buildFields(job.config, job); render(); editor.scrollIntoView({ block: 'start', behavior: 'smooth' });
      editor.querySelector('textarea')?.focus({ preventScroll: true });
      return { opened: true };
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
      await reload({ reset: operation === 'rerun' });
    });
  };
  scope.onchange = render; render();
  return {
    get busy() { return busy; }, get editing() { return !!mode; }, get blocked() { return blocked; },
    update(value) { if (!busy) updateQueue(value); },
    setBlocked(value) { blocked = !!value; render(); },
    destroy() { dead = true; selectionVersion++; globalThis.window?.removeEventListener('keydown', onConfirmationKey, true); confirmation.close?.(); confirmation.remove?.(); },
    openRerun,
  };
}
