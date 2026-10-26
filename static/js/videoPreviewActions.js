// Explicit one-click actions. Each candidate has its own durable request ID;
// retrying an interrupted response cannot enqueue the same refinement twice.
export function createPreviewActions({ request, defaults, onChange, onQueued, isClosed }) {
  const states = new Map();
  const state = id => {
    if (!states.has(id)) states.set(id, { busy: false, message: '', error: '' });
    return states.get(id);
  };
  async function refine(job, only) {
    const value = state(job.id);
    if (value.busy || isClosed()) return;
    value.busy = true; value.error = ''; value.message = 'Queuing saved previews…'; onChange();
    let queued = 0, skipped = 0;
    try {
      const scout = job.seed_hunter?.stage === 'scout';
      const candidates = scout ? (job.scout?.previews || []).map(item => ({ index: (job.scout?.seeds || []).indexOf(item.seed) + 1, seed: item.seed, can_refine: true })).filter(item => item.index > 0)
        : (job.seed_hunter?.candidates || []).filter(item => item.can_refine);
      const chosen = candidates.filter(item => only === undefined || item.index === only);
      if (!chosen.length) throw new Error('No saved preview is ready to refine yet.');
      const data = scout ? null : await request(`/jobs/${job.id}/seed-hunter`);
      for (const candidate of chosen) {
        const key = `odysseus-default-refine:${job.id}:${candidate.index}`;
        let saved; try { saved = JSON.parse(sessionStorage.getItem(key) || 'null'); } catch {}
        const existing = (job.refinements || []).some(item => ['queued', 'running', 'completed'].includes(item.status)
          && (scout ? item.seed_hunter?.source_seed === candidate.seed : item.seed_hunter?.source_candidate === candidate.index));
        if (existing) { skipped++; continue; }
        if (saved?.accepted) {
          try {
            const found = await request(`/jobs/${saved.accepted}`);
            if (['queued', 'running', 'completed'].includes((found.job || found).status)) { skipped++; continue; }
          } catch (error) { if (error.status !== 404) throw error; }
          saved = null; sessionStorage.removeItem(key);
        }
        const path = scout ? `/jobs/${job.id}/scout/render` : `/jobs/${job.id}/seed-hunter/finalize`;
        const body = saved?.body || { request_id: crypto.randomUUID(), ...(scout ? { seed: candidate.seed } : { candidate: candidate.index, config: defaults(data, candidate) }) };
        sessionStorage.setItem(key, JSON.stringify({ body }));
        let result;
        try { result = await request(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); }
        catch (error) {
          if ([400, 404, 422].includes(error.status)) sessionStorage.removeItem(key);
          throw error;
        }
        if (!result.id && !result.job?.id) throw new Error('The server did not confirm this refinement. Click again to retry safely.');
        sessionStorage.setItem(key, JSON.stringify({ body, accepted: result.id || result.job.id })); queued++;
        value.message = `${queued} refinement${queued === 1 ? '' : 's'} queued${skipped ? ` · ${skipped} already refined or queued` : ''}.`; onChange();
      }
      value.message = `${queued} refinement${queued === 1 ? '' : 's'} queued${skipped ? ` · ${skipped} already refined or queued` : ''}.`;
    } catch (error) { value.error = error.message; }
    finally { value.busy = false; if (!isClosed()) { onChange(); await onQueued(); } }
  }
  return { state, refine };
}
