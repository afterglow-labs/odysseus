// Editing replaces one queued record in place; it never submits a new job.
export function videoJobEditFormData(edit, config, uploads, retained) {
  const body = new FormData();
  body.append('config', JSON.stringify(config));
  body.append('revision', String(edit.revision));
  body.append('retain_inputs', JSON.stringify(Object.fromEntries(
    Object.entries(retained).map(([key, items]) => [key, items.map(item => item.index)]))));
  for (const [key, items] of Object.entries(uploads)) {
    for (const file of items) body.append(key, file, file.name);
  }
  return body;
}

export function createVideoJobEditor({ form, request, getDraft, applyEdit, restoreDraft, onChange, onError, isBusy, isClosed }) {
  let active = null, draft = null, loading = false, blocked = '';
  const panel = document.createElement('section'); panel.className = 'h3-video-editing'; panel.hidden = true;
  panel.setAttribute('aria-label', 'Edit queued job');
  const title = document.createElement('strong');
  const status = document.createElement('p'); status.className = 'h3-video-muted'; status.setAttribute('role', 'status');
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'memory-toolbar-btn'; cancel.textContent = 'Cancel edit';
  panel.append(title, status, cancel); form.prepend(panel);
  function render() {
    panel.hidden = !active;
    if (!active) return;
    title.textContent = `Editing queued job · ${active.id}`;
    status.textContent = blocked || 'Save changes updates this job in its current queue position. The queue keeps running while you edit. Cancel restores your previous draft.';
    status.classList.toggle('h3-video-error', !!blocked);
    cancel.disabled = isBusy();
  }
  function restore() {
    if (!active) return;
    const previous = draft;
    active = null; draft = null; blocked = ''; panel.hidden = true;
    restoreDraft(previous); onChange();
  }
  cancel.onclick = () => { if (!isBusy()) { restore(); onError(''); } };
  return {
    get active() { return active; }, get loading() { return loading; }, get blocked() { return blocked; },
    async open(id) {
      if (loading || active || isBusy()) return;
      loading = true; onError(''); onChange();
      try {
        const value = await request(`/jobs/${encodeURIComponent(id)}/edit`);
        if (isClosed()) return;
        if (value.id !== id || !Number.isInteger(value.revision) || !value.config || !value.inputs) throw new Error('The server returned an invalid queued job. Refresh jobs and retry.');
        draft = getDraft(); active = value; blocked = '';
        try { applyEdit(value); }
        catch (error) { restore(); throw error; }
        render(); panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
        form.querySelector('textarea')?.focus({ preventScroll: true });
      } catch (error) { if (!isClosed()) onError(`Could not edit queued job: ${error.message}`); }
      finally { loading = false; if (!isClosed()) { render(); onChange(); } }
    },
    observe(jobs) {
      if (active) {
        const job = jobs.find(item => item.id === active.id);
        if (job && job.status !== 'queued') blocked = 'This job has already left the queue. Your edits are still here, but they cannot change a running or finished job. Cancel edit to return to your draft.';
        else if (job && Number.isInteger(job.revision) && job.revision > active.revision) blocked = 'This job changed elsewhere. Your edits are still here. Cancel edit and reopen the job to load its latest settings.';
      }
      render();
    },
    failed(error) {
      if (error.status === 409) blocked = 'The job started or changed before these edits could be saved. Your edits are still here. Cancel edit and reopen the queued job to load its latest settings.';
      render(); onError(error.message); onChange();
    },
    finish: restore,
  };
}
