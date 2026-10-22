import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';

const make = (tag, text = '', cls = '') => {
  const node = document.createElement(tag); node.textContent = text; node.className = cls; return node;
};
const button = text => { const node = make('button', text, 'memory-toolbar-btn'); node.type = 'button'; return node; };

// Reconcile by job ID so status polls never replace a playing video or its controls.
export function createVideoRefinements({ stop, edit, rerun, retry, remove, state, onError }) {
  const node = make('div', '', 'h3-video-refinements'), rows = new Map();
  function update(jobs) {
    node.hidden = !jobs.length;
    for (const [id, row] of rows) if (!jobs.some(job => job.id === id)) {
      releaseVideoJobMedia(row.card); row.card.remove(); rows.delete(id);
    }
    for (const [index, job] of jobs.entries()) {
      let row = rows.get(job.id);
      if (!row) {
        const card = make('section', '', 'h3-video-refinement'), title = make('h6'), progress = make('p', '', 'h3-video-muted');
        card.dataset.refinementId = job.id;
        const error = make('p', '', 'h3-video-error'), tools = make('div', '', 'h3-video-job-actions');
        const output = make('div'), details = make('details'), log = make('pre', '', 'h3-video-log');
        details.append(make('summary', 'Refinement log'), log);
        const cancel = button('Cancel refinement'), change = button('Edit refinement'), again = button('Edit & rerun'), repeat = button('Retry'), del = button('Delete refinement');
        tools.append(cancel, change, again, repeat, del);
        const confirm = make('div'), yes = button('Delete permanently'), no = button('Keep refinement');
        confirm.hidden = true; confirm.append(make('p', 'Delete this refinement and its output? The source previews and other refinements are kept.'), yes, no);
        card.append(title, progress, error, output, tools, confirm, details); node.append(card);
        row = { card, title, progress, error, output, log, cancel, change, again, repeat, del, confirm, job, pending: false };
        rows.set(job.id, row);
        const act = action => async () => {
          if (row.pending) return;
          row.pending = true;
          try { await action(row.job); }
          catch (error) { onError(error); }
          finally { row.pending = false; }
        };
        cancel.onclick = act(stop); change.onclick = act(edit); again.onclick = act(rerun); repeat.onclick = act(retry);
        del.onclick = () => { confirm.hidden = false; }; no.onclick = () => { confirm.hidden = true; };
        yes.onclick = act(remove);
      }
      row.job = job;
      if (node.children[index] !== row.card) node.insertBefore(row.card, node.children[index] || null);
      row.title.textContent = `Refinement ${index + 1} · ${job.status}`;
      row.progress.textContent = [job.status === 'queued' && job.queue_position ? `Queue position ${job.queue_position}` : job.phase,
        job.status === 'running' && job.total_steps ? `Step ${job.step || 0}/${job.total_steps}` : ''].filter(Boolean).join(' · ');
      const run = state(job);
      row.error.textContent = job.error || run.error || run.message || ''; row.error.hidden = !row.error.textContent;
      row.log.textContent = Array.isArray(job.log_tail) ? job.log_tail.join('\n') : job.log_tail || '';
      row.cancel.hidden = !['running', 'queued'].includes(job.status); row.cancel.textContent = job.status === 'queued' ? 'Cancel refinement' : 'Stop refinement';
      row.change.hidden = job.status !== 'queued'; row.again.hidden = row.repeat.hidden = run.hidden;
      row.again.disabled = row.repeat.disabled = run.disabled;
      row.repeat.textContent = run.label; row.del.hidden = !['completed', 'failed', 'stopped'].includes(job.status);
      if (job.status === 'completed' && job.url && !row.output.childElementCount) {
        const player = createVideoJobMedia(job.url, { label: 'Play refined video' });
        const download = make('a', 'Download refinement', 'memory-toolbar-btn'); download.href = job.url; download.download = `h3-refinement-${job.id}.mp4`;
        row.output.append(player.node, download);
      }
    }
  }
  return { node, update };
}
