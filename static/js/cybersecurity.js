// Odysseus Cybersecurity mode: a native, same-origin workbench for Afterglow.
// All backend paths are fixed by the server gateway. Backend text is rendered
// only through textContent, including findings and intercepted tool inputs.

const MODE_KEY = 'odysseus-product-mode';
const TASK_KEY = 'odysseus-cyber-task';
const TABS = new Set(['overview', 'tasks', 'findings', 'scope', 'activity', 'intercept', 'traffic']);
const byId = (id) => document.getElementById(id);

function element(tag, className = '', text = '') {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function button(label, onClick, className = '') {
  const node = element('button', className, label);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

function panel(title) {
  const node = element('section', 'admin-card cyber-panel');
  if (title) node.append(element('h3', '', title));
  return node;
}

function empty(message) { return element('div', 'cyber-empty', message); }

function pill(value) {
  const label = String(value || 'unknown');
  return element('span', `cyber-pill ${label.toLowerCase().replace(/[^a-z]/g, '')}`, label.replaceAll('_', ' '));
}

function row(title, subtitle, badge, onClick) {
  const outer = element(onClick ? 'button' : 'div', 'cyber-row');
  if (onClick) { outer.type = 'button'; outer.addEventListener('click', onClick); }
  const main = element('div', 'cyber-row-main');
  main.append(element('div', 'cyber-row-title', title), element('div', 'cyber-row-sub', subtitle));
  outer.append(main);
  if (badge !== undefined && badge !== null) {
    const meta = element('div', 'cyber-row-meta');
    meta.append(pill(badge));
    outer.append(meta);
  }
  return outer;
}

function fmtDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

async function api(path, options = {}) {
  const headers = { Accept: 'application/json' };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    headers['X-Requested-With'] = 'XMLHttpRequest';
  }
  const response = await fetch('/api/cybersecurity' + path, {
    method: options.method || 'GET', credentials: 'same-origin', headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
  });
  let data;
  try { data = await response.json(); } catch (_) { throw new Error('Invalid response from Odysseus'); }
  if (!response.ok) throw new Error(data.detail || data.error || `Request failed (${response.status})`);
  return data;
}

export function initCybersecurityMode() {
  const workbench = byId('cyber-workbench');
  const content = byId('cyber-content');
  const odyButton = byId('product-mode-odysseus');
  const cyberButton = byId('product-mode-cybersecurity');
  if (!workbench || !content || !odyButton || !cyberButton || workbench.dataset.initialized) return;
  workbench.dataset.initialized = 'true';

  let mode = document.documentElement.classList.contains('cyber-mode') ? 'cybersecurity' : 'odysseus';
  let tab = 'overview';
  let generation = 0;
  let selectedTask = '';
  let taskList = [];
  let findingPage = 1;
  let findingFilters = { severity: '', status: '', q: '' };
  try { selectedTask = localStorage.getItem(TASK_KEY) || ''; } catch (_) {}

  const notice = byId('cyber-notice');
  const connection = byId('cyber-connection');
  const tabs = [...workbench.querySelectorAll('[data-cyber-tab]')];

  function showNotice(message, error = false) {
    notice.hidden = !message;
    notice.classList.toggle('error', error);
    notice.textContent = message || '';
  }

  function setConnection(value, label) {
    connection.classList.toggle('connected', value === 'connected');
    connection.classList.toggle('disconnected', value === 'disconnected');
    connection.textContent = label;
  }

  function setMode(next) {
    mode = next;
    generation += 1;
    const isCyber = next === 'cybersecurity';
    document.documentElement.classList.toggle('cyber-mode', isCyber);
    workbench.hidden = !isCyber;
    odyButton.setAttribute('aria-pressed', String(!isCyber));
    cyberButton.setAttribute('aria-pressed', String(isCyber));
    const main = byId('chat-container');
    if (main) main.setAttribute('aria-label', isCyber ? 'Cybersecurity workbench' : 'Chat area');
    const heading = main?.querySelector('h1');
    if (heading) heading.textContent = isCyber ? 'Afterglow Cybersecurity' : 'Odysseus';
    document.title = isCyber ? 'Cybersecurity · Odysseus' : 'Odysseus Chat';
    try { localStorage.setItem(MODE_KEY, next); } catch (_) {}
    if (isCyber) refresh();
    else showNotice('');
  }

  function selectTab(next) {
    if (!TABS.has(next)) return Promise.resolve();
    tab = next;
    tabs.forEach((item) => {
      if (item.dataset.cyberTab === next) item.setAttribute('aria-current', 'page');
      else item.removeAttribute('aria-current');
    });
    return refresh();
  }

  async function loadTasks() {
    const data = await api('/tasks');
    taskList = Array.isArray(data.tasks) ? data.tasks : [];
    if (!taskList.some((task) => task.id === selectedTask)) {
      selectedTask = taskList.find((task) => task.id === data.active)?.id || taskList[0]?.id || '';
    }
    try { localStorage.setItem(TASK_KEY, selectedTask); } catch (_) {}
    return data;
  }

  function taskPicker(onChange) {
    const toolbar = element('div', 'cyber-toolbar');
    const label = element('label', '', 'Task');
    const select = element('select');
    select.setAttribute('aria-label', 'Task');
    for (const task of taskList) {
      const option = element('option', '', `${task.name || task.description || 'Task'} · #${task.id}`);
      option.value = task.id;
      select.append(option);
    }
    select.value = selectedTask;
    select.addEventListener('change', () => {
      selectedTask = select.value;
      try { localStorage.setItem(TASK_KEY, selectedTask); } catch (_) {}
      onChange();
    });
    toolbar.append(label, select);
    return toolbar;
  }

  function card(label, value, detail = '') {
    const node = element('div', 'admin-card cyber-card');
    node.append(element('div', 'cyber-card-label', label), element('div', 'cyber-card-value', value));
    if (detail) node.append(element('div', 'cyber-card-detail', detail));
    return node;
  }

  async function renderOverview(status) {
    const [tasks, findings, intercept, traffic] = await Promise.all([
      loadTasks(), api('/findings?page=1&limit=5'),
      api('/intercept/pending'), api('/traffic?page=0&size=5'),
    ]);
    const wrap = element('div');
    const metrics = element('div', 'cyber-grid');
    const pending = Array.isArray(intercept.pending) ? intercept.pending : [];
    const recentFindings = Array.isArray(findings.items) ? findings.items : [];
    metrics.append(
      card('LLM', status.stats?.llm_configured ? 'Ready' : 'Not set', 'Afterglow engine configuration'),
      card('Tasks', taskList.length, tasks.active ? `Active task #${tasks.active}` : 'No active task'),
      card('Findings', findings.total ?? 0, 'All recorded findings'),
      card('Pending interception', pending.length, 'Requires a decision in Afterglow'),
      card('Traffic', traffic.count ?? 0, traffic.enabled ? 'Capture enabled' : 'Capture disabled'),
    );
    wrap.append(metrics);
    const taskPanel = panel('Recent tasks');
    const taskRows = element('div', 'cyber-list');
    if (!taskList.length) taskRows.append(empty('No tasks yet. Create the first task in Afterglow.'));
    for (const task of taskList.slice(0, 5)) {
      taskRows.append(row(task.name || task.description || `Task #${task.id}`,
        `#${task.id} · ${task.goal || task.description || ''}`, task.status,
        () => { selectedTask = task.id; selectTab('tasks'); }));
    }
    taskPanel.append(taskRows);
    wrap.append(taskPanel);
    const findingPanel = panel('Recent findings');
    const findingRows = element('div', 'cyber-list');
    if (!recentFindings.length) findingRows.append(empty('No findings recorded.'));
    for (const finding of recentFindings) {
      findingRows.append(row(finding.name || finding.vulnclass || `Finding #${finding.id}`,
        finding.summary || `Task #${finding.task_id || '—'}`, finding.severity,
        () => { selectTab('findings').then(() => openFinding(finding.id)); }));
    }
    findingPanel.append(findingRows);
    wrap.append(findingPanel);
    return wrap;
  }

  async function renderTasks() {
    await loadTasks();
    const wrap = element('div');
    const listPanel = panel('Tasks');
    const list = element('div', 'cyber-list');
    if (!taskList.length) list.append(empty('No tasks yet. Task creation remains in Afterglow.'));
    for (const task of taskList) {
      const count = task.findings || {};
      const findings = Object.values(count).reduce((sum, value) => sum + (Number(value) || 0), 0);
      const item = row(task.name || task.description || `Task #${task.id}`,
        `#${task.id} · ${findings} findings · ${fmtDate(task.created_at)}`, task.status,
        () => { selectedTask = task.id; try { localStorage.setItem(TASK_KEY, selectedTask); } catch (_) {} refresh(); });
      if (task.id === selectedTask) item.classList.add('selected');
      list.append(item);
    }
    listPanel.append(list);
    wrap.append(listPanel);
    if (selectedTask) {
      const task = await api(`/tasks/${selectedTask}`);
      const detail = panel(`Task #${task.id}${task.name ? ` · ${task.name}` : ''}`);
      detail.append(element('p', 'cyber-detail', task.description || 'No description'));
      if (task.goal) detail.append(element('p', 'cyber-detail', `Goal: ${task.goal}`));
      const actions = element('div', 'cyber-actions');
      actions.append(pill(task.status));
      if (task.status === 'running' || task.status === 'paused') {
        const action = task.status === 'running' ? 'pause' : 'resume';
        actions.append(button(action === 'pause' ? 'Pause task' : 'Resume task', async () => {
          await act(() => api(`/tasks/${task.id}/control`, { method: 'POST', body: { action } }),
            `Task ${action}d`);
        }, 'primary'));
      }
      actions.append(button('View scope', () => selectTab('scope')),
        button('View activity', () => selectTab('activity')));
      detail.append(actions);
      wrap.append(detail);
    }
    return wrap;
  }

  async function openFinding(id) {
    try {
      const finding = await api(`/findings/${encodeURIComponent(id)}`);
      if (mode !== 'cybersecurity' || tab !== 'findings') return;
      const existing = byId('cyber-finding-detail');
      if (existing) existing.remove();
      const detail = panel(`Finding #${finding.id} · ${finding.name || finding.vulnclass || 'Untitled'}`);
      detail.id = 'cyber-finding-detail';
      const meta = element('div', 'cyber-actions');
      meta.append(pill(finding.severity), pill(finding.status));
      detail.append(meta);
      if (finding.summary) detail.append(element('p', 'cyber-detail', finding.summary));
      if (finding.evidence) detail.append(element('pre', 'cyber-detail', finding.evidence));
      if (finding.report) detail.append(element('pre', 'cyber-detail', finding.report));
      if (!finding.evidence && !finding.report) detail.append(empty('No evidence or report attached.'));
      content.prepend(detail);
    } catch (error) { showNotice(error.message, true); }
  }

  async function renderFindings() {
    const params = new URLSearchParams({ page: String(findingPage), limit: '50', ...findingFilters });
    const data = await api(`/findings?${params}`);
    const wrap = element('div');
    const toolbar = element('form', 'cyber-toolbar');
    const search = element('input');
    search.type = 'search'; search.placeholder = 'Search findings'; search.setAttribute('aria-label', 'Search findings');
    search.value = findingFilters.q;
    const severity = element('select'); severity.setAttribute('aria-label', 'Severity');
    for (const value of ['', 'critical', 'high', 'medium', 'low']) {
      const option = element('option', '', value || 'All severities'); option.value = value; severity.append(option);
    }
    severity.value = findingFilters.severity;
    const status = element('select'); status.setAttribute('aria-label', 'Finding status');
    for (const value of ['', 'pending', 'in_progress', 'confirmed', 'resolved', 'fixed', 'false_positive', 'ignored', 'duplicate', 'risk_accepted']) {
      const option = element('option', '', value ? value.replaceAll('_', ' ') : 'All statuses');
      option.value = value; status.append(option);
    }
    status.value = findingFilters.status;
    const submit = element('button', '', 'Filter'); submit.type = 'submit';
    toolbar.append(search, severity, status, submit);
    toolbar.addEventListener('submit', (event) => {
      event.preventDefault();
      findingFilters = { q: search.value.trim(), severity: severity.value, status: status.value };
      findingPage = 1;
      refresh();
    });
    wrap.append(toolbar);
    const listPanel = panel(`Findings · ${data.total ?? 0}`);
    const list = element('div', 'cyber-list');
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) list.append(empty('No findings match these filters.'));
    for (const finding of items) {
      list.append(row(finding.name || finding.vulnclass || `Finding #${finding.id}`,
        `#${finding.id} · ${finding.summary || ''}`, finding.severity, () => openFinding(finding.id)));
    }
    listPanel.append(list);
    const actions = element('div', 'cyber-actions');
    if (findingPage > 1) actions.append(button('Previous', () => { findingPage -= 1; refresh(); }));
    if (findingPage * 50 < (data.total || 0)) actions.append(button('Next', () => { findingPage += 1; refresh(); }));
    listPanel.append(actions);
    wrap.append(listPanel);
    return wrap;
  }

  async function renderScope() {
    await loadTasks();
    const wrap = element('div');
    if (!selectedTask) return empty('Select a task after one has been created in Afterglow.');
    wrap.append(taskPicker(refresh));
    const [scope, coverage] = await Promise.all([
      api(`/tasks/${selectedTask}/scope`), api(`/tasks/${selectedTask}/coverage`),
    ]);
    const coveragePanel = panel('Coverage');
    coveragePanel.append(element('p', 'cyber-detail',
      'Task scope is a coverage and asset filter. It does not enforce a network boundary; interception rules govern tool operations.'));
    const metrics = element('div', 'cyber-grid');
    metrics.append(card('In scope', coverage.denominator ?? '—'),
      card('Tested', coverage.tested ?? '—'),
      card('Coverage', coverage.pct == null ? '—' : `${Math.round(Number(coverage.pct) * 100)}%`));
    coveragePanel.append(metrics);
    if (coverage.enabled === false) coveragePanel.append(empty('Coverage is disabled for this task.'));
    wrap.append(coveragePanel);
    const scopePanel = panel('Declared task scope');
    const list = element('div', 'cyber-list');
    const entries = Array.isArray(scope.scope) ? scope.scope : [];
    if (!entries.length) list.append(empty('No scope rows recorded.'));
    for (const entry of entries) {
      list.append(row(entry.company_name || entry.domain || entry.net || entry.value || `Scope #${entry.id}`,
        `${entry.kind} · ${entry.source}${entry.reason ? ` · ${entry.reason}` : ''}`, null));
    }
    scopePanel.append(list);
    wrap.append(scopePanel);
    return wrap;
  }

  async function renderActivity() {
    await loadTasks();
    if (!selectedTask) return empty('No task selected.');
    const wrap = element('div');
    wrap.append(taskPicker(refresh));
    const data = await api(`/tasks/${selectedTask}/activity?limit=100`);
    const listPanel = panel('Current main-session activity');
    const list = element('div', 'cyber-list');
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) list.append(empty('No activity recorded for this task.'));
    for (const item of [...items].reverse()) {
      const title = item.summary || item.tool || item.kind || `Event #${item.seq}`;
      list.append(row(title, `${fmtDate(item.ts)} · ${item.worker || item.kind || ''}`, item.is_error ? 'error' : item.kind));
    }
    listPanel.append(list);
    wrap.append(listPanel);
    return wrap;
  }

  async function renderIntercept() {
    const data = await api('/intercept/pending');
    const wrap = element('div');
    const listPanel = panel('Pending interception decisions');
    const list = element('div', 'cyber-list');
    const items = Array.isArray(data.pending) ? data.pending : [];
    if (!items.length) list.append(empty('No pending interception decisions.'));
    for (const item of items) {
      const entry = element('div', 'cyber-row');
      const main = element('div', 'cyber-row-main');
      main.append(element('div', 'cyber-row-title', `${item.tool_name || 'Tool'} · #${item.id}`),
        element('div', 'cyber-row-sub', `Task #${item.task_id || '—'} · ${item.reason || 'No rule reason'}`));
      if (item.tool_input !== undefined) {
        main.append(element('pre', 'cyber-detail',
          typeof item.tool_input === 'string' ? item.tool_input : JSON.stringify(item.tool_input, null, 2)));
      }
      const actions = element('div', 'cyber-actions');
      actions.append(button('Allow', () => decide(item.id, 'allowed'), 'primary'),
        button('Deny', () => decide(item.id, 'denied')));
      main.append(actions);
      entry.append(main);
      list.append(entry);
    }
    listPanel.append(list);
    wrap.append(listPanel);
    return wrap;
  }

  async function decide(id, decision) {
    await act(() => api(`/intercept/pending/${id}/decide`, { method: 'POST', body: { decision } }),
      `Interception ${decision}`);
  }

  async function renderTraffic() {
    const data = await api('/traffic?page=0&size=100');
    const wrap = element('div');
    const metrics = element('div', 'cyber-grid');
    metrics.append(card('Recorded exchanges', data.count ?? 0),
      card('Capture', data.enabled ? 'On' : 'Off'),
      card('Proxy', data.proxy || 'Unavailable'));
    wrap.append(metrics);
    const trafficPanel = panel('Recent traffic');
    const list = element('div', 'cyber-list');
    const items = Array.isArray(data.exchanges) ? data.exchanges : [];
    if (!items.length) list.append(empty('No traffic recorded.'));
    for (const exchange of items) {
      list.append(row(`${exchange.method || 'GET'} ${exchange.url || exchange.host || ''}`,
        `${fmtDate(exchange.ts)} · ${exchange.host || ''}`, exchange.status));
    }
    trafficPanel.append(list);
    wrap.append(trafficPanel);
    return wrap;
  }

  async function act(operation, message) {
    try {
      await operation();
      await refresh();
      showNotice(message);
    } catch (error) { showNotice(error.message, true); }
  }

  async function refresh() {
    if (mode !== 'cybersecurity') return;
    const run = ++generation;
    content.replaceChildren(empty('Loading Afterglow…'));
    setConnection('connecting', 'Connecting');
    let connected = false;
    try {
      const status = await api('/status');
      if (run !== generation || mode !== 'cybersecurity') return;
      connected = true;
      setConnection('connected', status.health?.ok ? 'Connected' : 'Connected · degraded');
      let view;
      if (tab === 'overview') view = await renderOverview(status);
      else if (tab === 'tasks') view = await renderTasks();
      else if (tab === 'findings') view = await renderFindings();
      else if (tab === 'scope') view = await renderScope();
      else if (tab === 'activity') view = await renderActivity();
      else if (tab === 'intercept') view = await renderIntercept();
      else view = await renderTraffic();
      if (run !== generation || mode !== 'cybersecurity') return;
      content.replaceChildren(view);
      showNotice('');
    } catch (error) {
      if (run !== generation || mode !== 'cybersecurity') return;
      if (!connected) setConnection('disconnected', 'Unavailable');
      content.replaceChildren(empty('Afterglow data is unavailable. Check the server connection and credential.'));
      showNotice(error.message, true);
    }
  }

  odyButton.addEventListener('click', () => setMode('odysseus'));
  cyberButton.addEventListener('click', () => setMode('cybersecurity'));
  byId('cyber-refresh').addEventListener('click', refresh);
  tabs.forEach((item) => item.addEventListener('click', () => selectTab(item.dataset.cyberTab)));
  setMode(mode);
}
