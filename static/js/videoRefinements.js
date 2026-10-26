import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';
import { videoActionMessage } from './videoJobActions.js';
import { BASE_RERUN_LABEL, BASE_RERUN_HELP } from './videoJobRerun.js';

const make = (tag, text = '', cls = '') => {
  const node = document.createElement(tag); node.textContent = text; node.className = cls; return node;
};
const button = text => { const node = make('button', text, 'memory-toolbar-btn'); node.type = 'button'; return node; };

// Reconcile by job ID so status polls never replace a playing video or its controls.
export function createVideoRefinements({ stop, edit, rerun, retry, reprocessBase, remove, state, onError, ask, processNow, duplicate }) {
  const node = make('div', '', 'h3-video-refinements'), rows = new Map();
  let currentJobs = [];
  function update(jobs) {
    currentJobs = jobs;
    node.hidden = !jobs.length;
    for (const [id, row] of rows) if (!jobs.some(job => job.id === id)) {
      releaseVideoJobMedia(row.card); row.card.remove(); rows.delete(id);
    }
    for (const [index, job] of jobs.entries()) {
      let row = rows.get(job.id);
      if (!row) {
        const card = make('section', '', 'h3-video-refinement'), title = make('h6'), progress = make('p', '', 'h3-video-muted');
        card.dataset.refinementId = job.id;
        const notice = make('p', '', 'h3-video-muted'); notice.setAttribute('role', 'status');
        const error = make('p', '', 'h3-video-error'), tools = make('div', '', 'h3-video-job-actions');
        const output = make('div'), details = make('details'), log = make('pre', '', 'h3-video-log');
        details.append(make('summary', 'Refinement log'), log);
        const workspace = make('div', '', 'video-job-workspace');
        const cancel = button('Cancel refinement'), change = button('Quick edit'), again = button('Quick edit & retry'), repeat = button('Retry'), base = button(BASE_RERUN_LABEL), del = button('Delete refinement');
        const askButton=button('Ask for a change'), now=button('Process now'), rerunButton=button('Rerun');
        base.title = BASE_RERUN_HELP;
        tools.append(cancel, now, repeat, rerunButton, change, again, base, askButton, del);
        const confirm = make('div'), yes = button('Delete permanently'), no = button('Keep refinement');
        confirm.hidden = true; confirm.append(make('p', 'Delete this refinement and its output? The source previews and other refinements are kept.'), yes, no);
        card.append(title, progress, notice, error, tools, workspace, output, confirm, details); node.append(card);
        row = { notice, tools, workspace, askButton, now, rerunButton, card, title, progress, error, output, log, cancel, change, again, repeat, base, del, confirm, job, pending: false };
        rows.set(job.id, row);
        const act = (action, kind = '') => async () => {
          if (row.pending) return;
          row.pending = true; row.actionKind = kind; row.actionError = '';
          row.message = kind === 'stop' ? 'Stopping…' : kind === 'process_now' ? 'Moving to the front of the queue…' : 'Updating…';
          update(currentJobs);
          try {
            const result = await action(row.job, workspace);
            row.message = kind ? videoActionMessage(kind, result) : '';
          } catch (failure) { row.actionError = failure.message; row.message = ''; }
          finally { row.pending = false; update(currentJobs); }
        };
        cancel.onclick = act(stop, 'stop'); change.onclick = act(edit); again.onclick = act(rerun); repeat.onclick = act(retry);
        base.onclick = act(reprocessBase);
        askButton.onclick=act(ask);now.onclick=act(processNow,'process_now');rerunButton.onclick=act(duplicate,'duplicate');
        del.onclick = () => { confirm.hidden = false; }; no.onclick = () => { confirm.hidden = true; };
        yes.onclick = act(remove, 'delete');
      }
      row.job = job;
      if (node.children[index] !== row.card) node.insertBefore(row.card, node.children[index] || null);
      row.title.textContent = `${job.result_edit ? 'Edited result' : 'Refinement'} ${index + 1} · ${job.status}`;
      row.progress.textContent = [job.status === 'queued' && job.queue_position ? `Queue position ${job.queue_position}` : job.phase,
        job.status === 'running' && job.total_steps ? `Step ${job.step || 0}/${job.total_steps}` : '',
        job.seed_hunter?.source_candidate ? `From preview ${job.seed_hunter.source_candidate}` : '',
        job.seed_hunter?.source_seed != null ? `Seed ${job.seed_hunter.source_seed}` : '',
        job.seed_hunter?.frames ? `${(job.seed_hunter.frames/24).toFixed(2)}s` : '',
        job.seed_hunter?.width ? `${job.seed_hunter.width} × ${job.seed_hunter.height}` : ''].filter(Boolean).join(' · ');
      const run = state(job);
      row.error.textContent = row.actionError || run.error || job.error || '';
      row.notice.textContent = row.message || run.message || ''; row.notice.hidden = !row.notice.textContent; row.error.hidden = !row.error.textContent;
      row.log.textContent = Array.isArray(job.log_tail) ? job.log_tail.join('\n') : job.log_tail || '';
      row.cancel.hidden = !['running', 'queued'].includes(job.status); row.cancel.textContent = job.status === 'queued' ? 'Cancel refinement' : 'Stop refinement';
      row.change.hidden = job.status !== 'queued'; row.again.hidden = row.repeat.hidden = run.hidden;
      for (const control of row.tools.querySelectorAll('button')) control.disabled = row.pending;
      row.repeat.disabled = row.pending || run.disabled; row.again.disabled = row.pending || run.disabled || run.pending;
      row.base.hidden = run.hidden || !reprocessBase; row.base.disabled = row.pending || run.baseDisabled;
      row.repeat.textContent = run.label; row.del.hidden = false;
      row.again.textContent=job.status==='completed'?'Quick edit & rerun':'Quick edit & retry';
      row.askButton.hidden=!ask||job.status!=='completed';row.now.hidden=!processNow||job.status!=='queued';
      row.rerunButton.hidden=!duplicate||!['failed','stopped'].includes(job.status);
      if (row.pending && row.actionKind==='stop') row.cancel.textContent='Stopping…';
      row.now.textContent=row.pending&&row.actionKind==='process_now'?'Moving…':'Process now';
      if (job.status === 'completed' && job.url && !row.output.childElementCount) {
        const player = createVideoJobMedia(job.url, { label: 'Play refined video' });
        const download = make('a', 'Download refinement', 'memory-toolbar-btn'); download.href = job.url; download.download = `h3-refinement-${job.id}.mp4`;
        row.output.append(player.node, download);
      }
    }
  }
  return { node, update };
}
