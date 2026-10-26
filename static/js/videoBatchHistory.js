// One visible card per submission. Individual job cards keep their existing
// players and controls, and are mounted inside the expanded batch.
export function createVideoBatchHistory({ list, request, reload, onError, activeOnly }) {
  const batches = new Map();
  let order = [], slots = new Map();
  const button = text => { const n = document.createElement('button'); n.type = 'button'; n.className = 'memory-toolbar-btn'; n.textContent = text; return n; };
  function create(job) {
    const card = document.createElement('article'); card.className = 'h3-video-job video-batch-master'; card.dataset.batchId = job.batch.id;
    const heading = button(''), counts = document.createElement('p'), tools = document.createElement('div'), content = document.createElement('div');
    heading.classList.add('video-batch-heading'); heading.setAttribute('aria-expanded', 'false');
    content.hidden = true; content.className = 'video-batch-children'; tools.className = 'h3-video-job-actions';
    const previous = button('Previous files'), next = button('Next files'), range = document.createElement('span'), pager = document.createElement('nav');
    pager.setAttribute('aria-label', 'Files in batch'); pager.append(previous, range, next); pager.hidden = true;
    const value = { card, heading, counts, tools, content, pager, previous, next, range, open: false, page: 1, busy: false };
    heading.onclick = () => { value.open = !value.open; heading.setAttribute('aria-expanded', String(value.open)); content.hidden = pager.hidden = !value.open; reload(); };
    previous.onclick = () => { value.page = Math.max(1, value.page - 1); reload(); };
    next.onclick = () => { value.page++; reload(); };
    for (const [label, action] of [['Stop all', 'stop'], ['Rerun all', 'rerun_all'], ['Duplicate batch', 'duplicate'], ['Delete batch', 'delete']]) {
      const control = button(label); tools.append(control);
      control.onclick = async () => {
        if (value.busy) return;
        if (action === 'delete' && !globalThis.confirm('Delete this entire batch, its queued items, saved inputs, previews and results?')) return;
        value.busy = true; for (const b of tools.children) b.disabled = true;
        try {
          const result = await request(`/batches/${encodeURIComponent(job.batch.id)}/actions`, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, request_id: globalThis.crypto.randomUUID()}) });
          if (result.failed) throw new Error(result.errors?.map(e => e.error).join('\n') || 'Some batch items could not be updated.');
          await reload();
        } catch (error) { onError(error); }
        finally { value.busy = false; for (const b of tools.children) b.disabled = false; }
      };
    }
    card.append(heading, counts, tools, content, pager); return value;
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
        value.heading.textContent = `${value.open ? '▾' : '▸'} ${job.batch.name} · ${job.batch_count} videos`;
        value.counts.textContent = Object.entries(job.counts || {}).map(([status, count]) => `${count} ${status}`).join(' · ');
        if (!value.open) continue;
        const detail = await request(`/batches/${encodeURIComponent(job.batch.id)}?page=${value.page}&page_size=10${activeOnly() ? '&active_only=true' : ''}`);
        value.page = detail.pagination.page;
        value.previous.disabled = value.page <= 1; value.next.disabled = value.page >= detail.pagination.total_pages;
        value.range.textContent = `Page ${value.page} of ${detail.pagination.total_pages} · ${detail.pagination.total} file jobs`;
        for (const item of detail.jobs) { slots.set(item.id, value.content); rows.push(item); }
      }
      return { ...data, jobs: rows };
    },
    place(job, card) {
      const parent = slots.get(job.id) || list;
      if (card.parentElement !== parent) parent.append(card);
    },
    finish(nodes) {
      order.forEach((job, index) => {
        const card = batches.get(job.id)?.card || nodes.get(job.id)?.card;
        if (card && list.children[index] !== card) list.insertBefore(card, list.children[index] || null);
      });
    },
    resetPages() { for (const b of batches.values()) b.page = 1; },
    destroy() { for (const b of batches.values()) b.card.remove(); batches.clear(); },
  };
}
