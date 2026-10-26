import { videoActionMessage } from './videoJobActions.js';

// One visible card per submission. Individual job cards keep their existing
// players and controls, and are mounted inside the expanded batch.
export function createVideoBatchHistory({ list, request, reload, onError, activeOnly, refineBatch }) {
  const batches = new Map();
  let order = [], slots = new Map();
  const button = text => { const n = document.createElement('button'); n.type = 'button'; n.className = 'memory-toolbar-btn'; n.textContent = text; return n; };
  function create(job) {
    const card = document.createElement('article'); card.className = 'h3-video-job video-batch-master'; card.dataset.batchId = job.batch.id;
    const view = button('View videos & prompts'), heading = button(''), counts = document.createElement('p'), tools = document.createElement('div'), content = document.createElement('div'), notice = document.createElement('p');
    notice.className = 'h3-video-muted video-batch-notice'; notice.setAttribute('role', 'status'); notice.hidden = true;
    heading.classList.add('video-batch-heading'); heading.setAttribute('aria-expanded', 'false');
    content.hidden = true; content.className = 'video-batch-children'; tools.className = 'h3-video-job-actions';
    const previous = button('Previous files'), next = button('Next files'), range = document.createElement('span'), pager = document.createElement('nav');
    pager.setAttribute('aria-label', 'Files in batch'); pager.append(previous, range, next); pager.hidden = true;
    const loading = document.createElement('p'), failure = document.createElement('p'), retry = button('Retry loading files');
    loading.setAttribute('role', 'status'); loading.textContent = 'Loading videos and prompts…'; loading.hidden = failure.hidden = retry.hidden = true;
    failure.className = 'h3-video-error'; retry.onclick = () => reload();
    const value = { card, view, heading, counts, tools, content, pager, previous, next, range, loading, failure, retry, open: false, page: 1, busy: false, fetching: false, job };
    function show() {
      heading.textContent = `${value.open ? '▾' : '▸'} ${value.job.batch.name} · ${value.job.batch_count} videos`;
      heading.setAttribute('aria-expanded', String(value.open)); view.setAttribute('aria-expanded', String(value.open));
      view.textContent = value.open ? 'Hide videos & prompts' : `View ${value.job.batch_count} videos & prompts`;
      content.hidden = !value.open; content.style.display = value.open ? '' : 'none';
      const showPages = value.open && !!range.textContent;
      pager.hidden = !showPages; pager.style.display = showPages ? '' : 'none';
      loading.hidden = !value.open || !value.fetching;
      failure.hidden = retry.hidden = !value.open || !failure.textContent;
    }
    value.show = show;
    value.updateControls = () => {
      for (const control of tools.children) control.disabled = value.busy ||
        control.dataset.action === 'stop' && !(value.job.counts?.running || value.job.counts?.queued);
    };
    heading.onclick = view.onclick = () => { value.open = !value.open; value.fetching = value.open; show(); reload(); };
    previous.onclick = () => { if (value.fetching) return; value.page = Math.max(1, value.page - 1); value.fetching = true; show(); reload(); };
    next.onclick = () => { if (value.fetching) return; value.page++; value.fetching = true; show(); reload(); };
    show();
    for (const [label, action] of [['Stop all', 'stop'], ['Rerun all', 'rerun_all'], ['Duplicate batch', 'duplicate'], ['Delete batch', 'delete']]) {
      const control = button(label); control.dataset.action = action; tools.append(control);
      control.onclick = async () => {
        if (value.busy) return;
        if (action === 'delete' && !globalThis.confirm('Delete this entire batch, its queued items, saved inputs, previews and results?')) return;
        value.busy = true; for (const b of tools.children) b.disabled = true;
        notice.hidden = false; notice.textContent = action === 'stop' ? 'Stopping all work in this batch…' : action === 'rerun_all' ? 'Requeuing batch…' : 'Updating batch…';
        const originalLabel = control.textContent; control.textContent = action === 'stop' ? 'Stopping…' : 'Working…';
        try {
          const result = await request(`/batches/${encodeURIComponent(job.batch.id)}/actions`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, request_id: globalThis.crypto.randomUUID()}) });
          if (result.failed) throw new Error(result.errors?.map(e => e.error).join('\n') || 'Some batch items could not be updated.');
          notice.textContent = action === 'rerun_all'
            ? `${result.queued_again ?? result.succeeded ?? 0} queued again in this batch · ${result.skipped_active ?? 0} already queued or running`
            : videoActionMessage(action, result);
          await reload();
        } catch (error) { notice.textContent = error.message || String(error); onError(error); }
        finally { value.busy = false; control.textContent = originalLabel; value.updateControls(); }
      };
    }
    if (refineBatch) {
      for (const [label, defaults] of [['Refine all previews with default settings', true], ['Refine all previews…', false]]) {
        const control = button(label); tools.append(control);
        control.onclick = async () => {
          if (value.busy) return;
          value.busy = true; for (const b of tools.children) b.disabled = true;
          notice.hidden = false; notice.textContent = 'Loading saved previews in this batch…';
          try { await refineBatch(job.batch.id, workspace, defaults); notice.textContent = defaults ? 'Refinement requests processed. Expand the videos to see each result.' : 'Review the refinement settings below.'; }
          catch (error) { notice.textContent = error.message || String(error); }
          finally { value.busy = false; value.updateControls(); }
        };
      }
    }
    const workspace = document.createElement('div'); workspace.className = 'video-job-workspace';
    card.append(heading, counts, view, tools, notice, workspace, loading, failure, retry, content, pager); return value;
  }
  return {
    async prepare(data) {
      order = data.jobs || []; slots = new Map();
      const ids = new Set(order.filter(j => j.id.startsWith('batch-')).map(j => j.id));
      for (const [id, value] of batches) if (!ids.has(id)) { value.card.remove(); batches.delete(id); }
      const rows = [];
      for (const job of order) {
        if (!job.id.startsWith('batch-')) { slots.set(job.id, list); rows.push(job); continue; }
        let value = batches.get(job.id);
        if (!value) { value = create(job); batches.set(job.id, value); }
        value.job = job; value.show(); value.updateControls();
        value.counts.textContent = Object.entries(job.counts || {}).map(([status, count]) => `${count} ${status}`).join(' · ') +
          (job.queue_position ? ` · Next file at queue position ${job.queue_position}` : '');
        if (!value.open) continue;
        value.fetching = true;
        value.previous.disabled = value.next.disabled = true;
        // Only show a loading placeholder for initial expansion, not every poll.
        if (!value.content.childElementCount) value.show();
        try {
          const detail = await request(`/batches/${encodeURIComponent(job.batch.id)}?page=${value.page}&page_size=10${activeOnly() ? '&active_only=true' : ''}`);
          if (!value.open) continue;
          value.failure.textContent = '';
          value.page = detail.pagination.page;
          value.previous.disabled = value.page <= 1; value.next.disabled = value.page >= detail.pagination.total_pages;
          value.range.textContent = `Page ${value.page} of ${detail.pagination.total_pages} · ${detail.pagination.total} file jobs`;
          if (!detail.jobs.length) value.failure.textContent = activeOnly() ? 'No active files in this batch. Turn off Active only to see its history.' : 'No file jobs remain in this batch.';
          for (const item of detail.jobs) { slots.set(item.id, value.content); rows.push(item); }
          value.childOrder = detail.jobs.map(item => item.id);
        } catch (error) { value.failure.textContent = `Could not load files: ${error.message || error}`; }
        finally { value.fetching = false; value.show(); }

      }
      return { ...data, jobs: rows };
    },
    place(job, card) {
      const parent = slots.get(job.id) || list;
      if (card.parentElement !== parent) parent.append(card);
    },
    finish(nodes) {
      for (const value of batches.values()) (value.childOrder || []).forEach((id, index) => {
        const card = nodes.get(id)?.card;
        if (card && value.content.children[index] !== card) value.content.insertBefore(card, value.content.children[index] || null);
      });
      order.forEach((job, index) => {
        const card = batches.get(job.id)?.card || nodes.get(job.id)?.card;
        if (card && list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
      });
    },
    resetPages() { for (const b of batches.values()) b.page = 1; },
    destroy() { for (const b of batches.values()) b.card.remove(); batches.clear(); },
  };
}
