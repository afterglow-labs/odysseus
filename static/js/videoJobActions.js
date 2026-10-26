// Action feedback belongs to the job, not to a single status-poll response.
export function videoActionMessage(action, result = {}) {
  const queue = result.queue || {}, current = queue.current_jobs?.[0];
  if (action === 'stop') return 'Stopped. Saved files are kept.' +
    (current ? ` Now rendering: ${current.source_name || current.id}.` : ' Other queued jobs can continue.');
  if (action === 'process_now') return queue.paused
    ? 'Moved to queue position 1. The queue is paused; resume it to start.'
    : current?.id === result.prioritized ? 'This job is now running.'
    : 'Moved to queue position 1.' + (current ? ` Starts after ${current.source_name || 'the current render'} finishes.` : ' Waiting for the worker to start.');
  if (action === 'duplicate') return 'Fresh job queued with the saved settings and files.';
  if (action === 'rerun_all') return `${result.queued_again ?? result.succeeded ?? 0} queued again · ${result.skipped_active ?? 0} already queued or running`;
  return 'Deleted.';
}

export function createVideoJobActions({request, onChange, onUpdated}) {
  const states = new Map();
  const state = id => states.get(id) || {};
  async function run(job, action) {
    if (state(job.id).busy) return;
    const value = {busy: true, action, error: '', message: action === 'stop' ? 'Stopping…' : action === 'process_now' ? 'Moving to the front of the queue…' : 'Updating job…'};
    states.set(job.id, value); onChange();
    try {
      const result = await request(`/jobs/${encodeURIComponent(job.id)}/actions`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action, request_id: crypto.randomUUID()})
      });
      if (result.failed) throw new Error(result.errors?.map(e => e.error).join(' · ') || 'The server could not finish this action.');
      value.message = videoActionMessage(action, result);
      value.stopped = action === 'stop';
      await onUpdated(result);
      return result;
    } catch (error) {
      value.message = ''; value.error = error.message || String(error);
      throw error;
    } finally { value.busy = false; onChange(); }
  }
  return {state, run};
}
