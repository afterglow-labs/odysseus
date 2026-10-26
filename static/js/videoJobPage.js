import { createVideoBatchHistory } from './videoBatchHistory.js';
export function videoJobDateBounds(selection, now = new Date()) {
  if (!['today', 'yesterday'].includes(selection)) return {};
  const start = new Date(now); start.setHours(0, 0, 0, 0);
  if (selection === 'yesterday') start.setDate(start.getDate() - 1);
  const end = new Date(start); end.setDate(end.getDate() + 1);
  return { created_after: start.getTime() / 1000, created_before: end.getTime() / 1000 };
}

export function checkedJobDateResponse(data, bounds) {
  if (Object.keys(bounds).some(key => data.date_filter?.[key] !== bounds[key]))
    throw new Error('Update the Odysseus server before filtering jobs by date.');
  return data;
}
// Job history stays bounded while the queue itself remains shared and unfiltered.
export function createVideoJobPageState({ pageSize = 10, recipe = '' } = {}) {
  let page = 1, before = null, total = 0, totalPages = 1, counts = {};
  return {
    get page() { return page; }, get total() { return total; },
    get totalPages() { return totalPages; }, get counts() { return counts; },
    query(options = {}) {
      const nextPage = options.reset ? 1 : options.page ?? page;
      const query = new URLSearchParams({ page: String(nextPage), page_size: String(pageSize) });
      if (recipe) query.set('recipe', recipe);
      if (!options.reset && before !== null) query.set('before', String(before));
      return `/jobs?${query}`;
    },
    accept(data, options = {}) {
      let jobs = Array.isArray(data.jobs) ? data.jobs : [];
      let legacyJobs;
      const meta = data.pagination;
      if (meta) {
        page = Math.max(1, Number(meta.page) || 1); total = Math.max(0, Number(meta.total) || 0);
        totalPages = Math.max(1, Number(meta.total_pages) || 1); before = meta.before ?? null;
        counts = data.counts || {};
      } else {
        // Older servers ignore page parameters and return the complete list.
        if (recipe) jobs = jobs.filter(job => job.recipe === recipe || recipe === 'seed_hunter' && job.seed_hunter);
        counts = jobs.reduce((out, job) => { out[job.status] = (out[job.status] || 0) + 1; return out; }, {});
        legacyJobs = jobs;
        total = jobs.length; totalPages = Math.max(1, Math.ceil(total / pageSize));
        page = Math.max(1, Math.min(totalPages, options.reset ? 1 : options.page ?? page));
        before = null; jobs = jobs.slice((page - 1) * pageSize, page * pageSize);
      }
      return { ...data, jobs, legacyJobs };
    },
    get label() {
      return total ? `${(page - 1) * pageSize + 1}–${Math.min(page * pageSize, total)} of ${total} jobs · Page ${page} of ${totalPages}` : '0 jobs';
    },
  };
}

export function createVideoJobPager({ container, beforeNode, request, recipe = '', historyKey = recipe || 'video', onData, onError, onFilterChange = () => {}, refineBatch, isClosed = () => false }) {
  const state = createVideoJobPageState({ recipe });
  const activity = document.createElement('div'); activity.className = 'video-queue-activity';
  const status = document.createElement('span'); status.setAttribute('role', 'status');
  const filter = document.createElement('label'), active = document.createElement('input'); active.type = 'checkbox';
  const key = `odysseus-video-active-only:${historyKey}`;
  try { active.checked = localStorage.getItem(key) === 'true'; } catch {}
  filter.append(active, document.createTextNode(' Active only')); activity.append(status, filter);
  filter.title = 'Show running and queued jobs in processing order. Position 1 runs next.';
  const dates = document.createElement('label'), date = document.createElement('select');
  dates.className = 'video-job-date-filter'; date.className = 'cookbook-field-input';
  date.add(new Option('All dates', 'all')); date.add(new Option('Today', 'today')); date.add(new Option('Yesterday', 'yesterday'));
  const dateKey = `odysseus-video-date:${historyKey}`;
  try { const saved = localStorage.getItem(dateKey); if (['all', 'today', 'yesterday'].includes(saved)) date.value = saved; } catch {}
  dates.append(document.createTextNode('Created '), date); activity.append(dates);
  // Outside the scrolling body: no jumping or disappearing with job polling.
  container.parentElement?.insertBefore(activity, container);
  const groups = createVideoBatchHistory({ list: beforeNode, request, reload: () => load({page: state.page}), onError, activeOnly: () => active.checked, refineBatch });
  const changed = () => { onFilterChange(); groups.resetPages(); load({reset:true}); };
  active.onchange = () => { try { localStorage.setItem(key, String(active.checked)); } catch {} changed(); };
  date.onchange = () => { try { localStorage.setItem(dateKey, date.value); } catch {} changed(); };
  function controls(bottom = false) {
    const node = document.createElement('nav'); node.className = 'video-job-pager';
    node.setAttribute('aria-label', bottom ? 'Job history pages below results' : 'Job history pages');
    const previous = document.createElement('button'), next = document.createElement('button'), label = document.createElement('span');
    for (const button of [previous, next]) { button.type = 'button'; button.className = 'memory-toolbar-btn'; }
    previous.textContent = 'Previous'; previous.setAttribute('aria-label', 'Previous page of jobs');
    next.textContent = 'Next'; next.setAttribute('aria-label', 'Next page of jobs');
    label.className = 'h3-video-muted'; label.setAttribute('role', 'status'); label.setAttribute('aria-live', 'polite');
    previous.onclick = () => load({ page: state.page - 1 }); next.onclick = () => load({ page: state.page + 1 });
    node.append(previous, label, next);
    return { node, previous, next, label };
  }
  const top = controls(), bottom = controls(true), pagers = [top, bottom];
  container.insertBefore(top.node, beforeNode || null);
  container.insertBefore(bottom.node, beforeNode?.nextSibling || null);
  let pending = null, task = null, destroyed = false;
  const alive = () => !destroyed && !isClosed();
  function render() {
    for (const { node, previous, next, label } of pagers) {
      label.textContent = state.label;
      previous.disabled = !!task || state.page <= 1; next.disabled = !!task || state.page >= state.totalPages;
      node.setAttribute('aria-busy', String(!!task));
    }
  }
  function load(options = {}) {
    if (!alive()) return Promise.resolve();
    // Explicit refreshes/submissions are never lost behind an in-flight poll.
    // Polls can reuse the current request; an explicit navigation wins next.
    if (options.reset || options.page !== undefined || !task) pending = { ...options };
    if (task) return task;
    task = Promise.resolve().then(async () => {
      while (pending && alive()) {
        const options = pending; pending = null;
        try {
          const bounds = videoJobDateBounds(date.value), dateQuery = new URLSearchParams(bounds).toString();
          const response = await request(state.query(options) + '&group_batches=true' + (active.checked ? '&active_only=true' : '') + (dateQuery ? '&' + dateQuery : ''));
          if (!alive()) return;
          if (pending) continue;
          checkedJobDateResponse(response, bounds);
          const data = state.accept(response, options);
          const grouped = await groups.prepare(data);
          if (!alive() || pending) continue;
          const q = data.queue || {}, current = q.current_jobs?.[0];
          status.textContent = [q.paused ? 'Queue paused' : `${q.total_running ?? q.running ?? 0} running`, `${q.total_queued ?? q.queued ?? 0} waiting`,
            current?.source_name, current?.phase, current?.total_steps ? `Step ${current.step || 0}/${current.total_steps}` : ''].filter(Boolean).join(' · ');
          await onData(grouped);
        } catch (error) { if (alive() && !pending) onError(error); }
      }
    }).finally(() => { task = null; if (alive()) render(); });
    render(); return task;
  }
  render();
  return { node: top.node, bottomNode: bottom.node, activity, load, place: groups.place, finish: groups.finish,
    get filters() { return { ...videoJobDateBounds(date.value), ...(active.checked ? { active_only: 'true' } : {}) }; },
    get counts() { return state.counts; }, get total() { return state.total; },
    destroy() { destroyed = true; pending = null; groups.destroy(); activity.remove(); } };
}

export async function observePagedVideoJobEditor(editor, jobs, request, isClosed = () => false) {
  editor?.observe(jobs);
  const id = editor?.active?.id;
  if (!id || jobs.some(job => job.id === id)) return;
  try {
    const data = await request(`/jobs/${encodeURIComponent(id)}`);
    if (!isClosed() && editor.active?.id === id) editor.observe([data.job || data]);
  } catch (error) {
    if (!isClosed() && editor.active?.id === id && error.status === 404)
      editor.observe([{ id, status: 'deleted' }]);
    // A transient detail failure must not discard the user's active draft.
  }
}
