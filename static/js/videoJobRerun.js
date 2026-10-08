// A per-job rerun always uses the saved server inputs/settings. Pending request
// identities survive panel reopens and refreshes so an interrupted response
// cannot turn a retry click into a second copy.
const rerunSessions = new Map();

export function createVideoJobReruns({ family, request, onChange = () => {}, onQueued = async () => {}, isClosed = () => false, isBlocked = () => false }) {
  const terminal = new Set(['completed', 'failed', 'stopped']);
  const storageKey = id => `odysseus-video-rerun-v1:${family}:${id}`;
  function entry(id) {
    const key = storageKey(id);
    if (!rerunSessions.has(key)) {
      let body = null;
      try {
        const saved = JSON.parse(globalThis.sessionStorage?.getItem(key) || 'null');
        if (saved?.jobs?.length === 1 && saved.jobs[0].id === id && Number.isInteger(saved.jobs[0].revision)
          && saved.jobs[0].revision >= 0 && saved.patch && typeof saved.patch === 'object' && !Array.isArray(saved.patch)
          && /^[0-9a-f-]{36}$/i.test(saved.request_id || '')) body = saved;
      } catch {}
      rerunSessions.set(key, { body, busy: false, message: '', error: '' });
    }
    return rerunSessions.get(key);
  }
  function persist(id, body) {
    entry(id).body = body;
    try {
      if (body) globalThis.sessionStorage?.setItem(storageKey(id), JSON.stringify(body));
      else globalThis.sessionStorage?.removeItem(storageKey(id));
    } catch {}
  }
  function changed() { if (!isClosed()) onChange(); }
  function state(job) {
    const value = entry(job.id);
    return {
      hidden: !terminal.has(job.status) && !value.body,
      disabled: value.busy || isBlocked(),
      label: value.busy ? 'Queuing…' : value.body ? 'Retry request' : job.status === 'completed' ? 'Reprocess' : 'Retry',
      message: value.message || (value.body ? 'Previous request is unconfirmed. Retry checks the same request without adding a duplicate.' : ''),
      error: value.error,
      pending: !!value.body,
    };
  }
  async function run(job, patch = {}, fromEditor = false) {
    if (!job?.id || isClosed() || state(job).hidden || entry(job.id).busy || (!fromEditor && isBlocked())) return;
    const value = entry(job.id);
    value.busy = true; value.error = ''; value.message = ''; changed();
    let result;
    try {
      if (!value.body) {
        const source = Number.isInteger(job.revision) && job.revision >= 0 ? job
          : (await request('/queue/jobs?status=finished')).jobs?.find(item => item.id === job.id);
        if (!source || source.id !== job.id || !terminal.has(source.status) || !Number.isInteger(source.revision) || source.revision < 0) {
          throw new Error('This job is no longer ready to run again. Refresh jobs and retry.');
        }
        if (isClosed()) return;
        persist(job.id, { jobs: [{ id: job.id, revision: source.revision }], patch: JSON.parse(JSON.stringify(patch)), request_id: globalThis.crypto.randomUUID() });
      }
      result = await request('/queue/rerun', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value.body) });
      const copy = Array.isArray(result.jobs) && result.jobs.length === 1 ? result.jobs[0] : null;
      const sameRequest = String(result.request_id || '').replaceAll('-', '').toLowerCase() === value.body.request_id.replaceAll('-', '').toLowerCase();
      if (!sameRequest || Number(result.succeeded ?? result.rerun) !== 1 || Number(result.failed || 0)
        || !copy || copy.source_id !== job.id || !/^[a-f0-9]{32}$/i.test(copy.id || '') || copy.id === job.id) {
        const detail = result.errors?.map(item => item.error || item.message || '').filter(Boolean).join('\n');
        throw new Error(detail || 'The server has not confirmed the new copy. Retry the same request safely.');
      }
      const edited = Object.keys(value.body.patch).length > 0;
      persist(job.id, null);
      value.message = `New job ${copy.id} added to the queue using this job’s saved inputs${edited ? ' and updated parameters' : ', prompt and settings'}. The original is kept.`;
    } catch (error) {
      // The source-revision conflict is raised before the server stages any
      // copy. A fresh click can safely fetch the edited source's new revision.
      if (error.status === 400 || (error.status === 409 && error.message === 'A selected job was edited elsewhere. Refresh to load its latest settings.')) persist(job.id, null);
      value.error = error.message || 'Could not queue this job again.';
      if (value.body) value.message = 'Retry request uses the same submission ID, so an already accepted copy will not be duplicated.';
    } finally {
      value.busy = false; changed();
    }
    if (result && !value.body && !isClosed()) {
      try { await onQueued(result); }
      catch { value.message += ' Refresh jobs to see the new copy.'; changed(); }
    }
    return result && !value.body && !value.error ? result : null;
  }
  return { state, run, busy: id => entry(id).busy };
}
