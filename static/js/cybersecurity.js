// Odysseus Cybersecurity mode: a native, same-origin workbench for APTEX.
// All backend paths are fixed by the server gateway. Backend text is rendered
// only through textContent, including findings and intercepted tool inputs.

const MODE_KEY = 'odysseus-product-mode';
const TASK_KEY = 'odysseus-cyber-task';
const TABS = new Set(['overview', 'tasks', 'learning', 'chat', 'findings', 'scope', 'assets', 'activity', 'intercept', 'traffic', 'reports', 'models']);
const LOCAL_OLLAMA_ENDPOINTS = new Set(['http://127.0.0.1:11434', 'http://host.docker.internal:11434', 'http://ollama:11434']);
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

function input(placeholder, required = false) {
  const node = element('input');
  node.placeholder = placeholder;
  node.setAttribute('aria-label', placeholder);
  node.required = required;
  return node;
}

function textarea(placeholder, required = false) {
  const node = element('textarea');
  node.placeholder = placeholder;
  node.setAttribute('aria-label', placeholder);
  node.required = required;
  return node;
}

function selectOptions(label, values) {
  const node = element('select');
  node.setAttribute('aria-label', label);
  for (const [value, text] of values) {
    const option = element('option', '', text);
    option.value = value;
    node.append(option);
  }
  return node;
}

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
  const method = options.method || 'GET';
  if (method !== 'GET') {
    headers['X-Requested-With'] = 'XMLHttpRequest';
  }
  if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  const response = await fetch('/api/cybersecurity' + path, {
    method, credentials: 'same-origin', headers,
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
  try { if (localStorage.getItem(MODE_KEY) === 'cybersecurity') mode = 'cybersecurity'; } catch (_) {}
  let tab = 'overview';
  let generation = 0;
  let selectedTask = '';
  let taskList = [];
  let findingPage = 1;
  let findingFilters = { severity: '', status: '', q: '' };
  let chatDraft = '';
  let chatPollUntil = 0;
  let chatgptAccount = '';
  let chatgptModel = '';
  let chatgptName = '';
  const chatgptCatalog = new Map();
  let assetType = 'root_domain';
  let assetOffset = 0;
  try { selectedTask = localStorage.getItem(TASK_KEY) || ''; } catch (_) {}

  const notice = byId('cyber-notice');
  const connection = byId('cyber-connection');
  const consoleLink = byId('cyber-open-console');
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
    if (heading) heading.textContent = isCyber ? 'APTEX Cybersecurity' : 'Odysseus';
    document.title = isCyber ? 'APTEX · Odysseus' : 'Odysseus Chat';
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
      card('LLM', status.stats?.llm_configured ? 'Ready' : 'Not set', 'APTEX engine configuration'),
      card('Tasks', taskList.length, tasks.active ? `Active task #${tasks.active}` : 'No active task'),
      card('Findings', findings.total ?? 0, 'All recorded findings'),
      card('Pending interception', pending.length, 'Requires a decision in APTEX'),
      card('Traffic', traffic.count ?? 0, traffic.enabled ? 'Capture enabled' : 'Capture disabled'),
    );
    wrap.append(metrics);
    const taskPanel = panel('Recent tasks');
    const taskRows = element('div', 'cyber-list');
    if (!taskList.length) taskRows.append(empty('No tasks yet. Open Tasks to create one.'));
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
    const createPanel = panel('Create task');
    createPanel.append(element('p', 'cyber-detail',
      'A new task starts on the APTEX server. Choose a model profile when the default is not the one you want.'));
    const form = element('form', 'cyber-stack');
    const name = input('Task name'); name.maxLength = 160;
    const description = textarea('Description', true); description.maxLength = 5000;
    const goal = textarea('Goal', true); goal.maxLength = 5000;
    const profile = selectOptions('Model profile', [['', 'Active model profile']]);
    try {
      const data = await api('/models/profiles');
      for (const item of Array.isArray(data.profiles) ? data.profiles : []) {
        const option = element('option', '', `${item.name || item.model || 'Profile'} · ${item.model || item.format || ''}`);
        option.value = String(item.id);
        profile.append(option);
      }
    } catch (_) { /* The active profile remains a valid task default. */ }
    const coverage = input('Track asset coverage'); coverage.type = 'checkbox'; coverage.checked = true;
    const seed = input('Start a first work item'); seed.type = 'checkbox';
    const coverageLabel = element('label', 'cyber-check'); coverageLabel.append(coverage, document.createTextNode('Track asset coverage'));
    const seedLabel = element('label', 'cyber-check'); seedLabel.append(seed, document.createTextNode('Start a first work item'));
    const checks = element('div', 'cyber-actions'); checks.append(coverageLabel, seedLabel);
    const submit = element('button', 'primary', 'Create task'); submit.type = 'submit';
    form.append(name, description, goal, profile, checks, submit);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      submit.disabled = true;
      try {
        const data = await api('/tasks', { method: 'POST', body: {
          name: name.value.trim(), description: description.value.trim(), goal: goal.value.trim(),
          coverage_enabled: coverage.checked, seed_first_intent: seed.checked,
          ...(profile.value ? { llm_profile_id: Number(profile.value) } : {}),
        } });
        if (data.id) selectedTask = String(data.id);
        await refresh();
        showNotice('Task created');
      } catch (error) { showNotice(error.message, true); }
      finally { submit.disabled = false; }
    });
    createPanel.append(form);
    wrap.append(createPanel);
    const listPanel = panel('Tasks');
    const list = element('div', 'cyber-list');
    if (!taskList.length) list.append(empty('No tasks yet. Create one above.'));
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
      actions.append(button('Open task chat', () => selectTab('chat')),
        button('View scope', () => selectTab('scope')),
        button('View activity', () => selectTab('activity')));
      detail.append(actions);
      wrap.append(detail);

      const [goalsData, constraintsData] = await Promise.all([
        api(`/tasks/${selectedTask}/goals`), api(`/tasks/${selectedTask}/constraints`),
      ]);
      const goalsPanel = panel('Goals');
      goalsPanel.append(element('p', 'cyber-detail',
        'Adding or editing a goal wakes the planner and can resume a completed or paused task.'));
      const goalForm = element('form', 'cyber-stack');
      const goalText = textarea('Goal text', true); goalText.maxLength = 5000;
      const goalClass = input('Vulnerability class (optional)'); goalClass.maxLength = 160;
      const addGoal = element('button', 'primary', 'Add goal'); addGoal.type = 'submit';
      goalForm.append(goalText, goalClass, addGoal);
      goalForm.addEventListener('submit', async (event) => {
        event.preventDefault();
        addGoal.disabled = true;
        try {
          await act(() => api(`/tasks/${selectedTask}/goals`, { method: 'POST', body: {
            text: goalText.value.trim(), vulnclass: goalClass.value.trim(),
          } }), 'Goal added');
        } finally { addGoal.disabled = false; }
      });
      goalsPanel.append(goalForm);
      const goalsList = element('div', 'cyber-list');
      const goals = Array.isArray(goalsData.goals) ? goalsData.goals : [];
      if (!goals.length) goalsList.append(empty('No goals recorded.'));
      for (const goalItem of goals) {
        const entry = panel('');
        entry.dataset.goalId = String(goalItem.id);
        entry.append(row(goalItem.text || `Goal #${goalItem.id}`,
          goalItem.vulnclass || `#${goalItem.id}`, goalItem.state));
        const controls = element('div', 'cyber-actions');
        const edit = element('form', 'cyber-stack'); edit.hidden = true;
        const textField = textarea('Goal text', true); textField.maxLength = 5000; textField.value = goalItem.text || '';
        const classField = input('Vulnerability class (optional)'); classField.maxLength = 160;
        classField.value = goalItem.vulnclass || '';
        const save = element('button', 'primary', 'Save goal'); save.type = 'submit';
        edit.append(textField, classField, save);
        edit.addEventListener('submit', async (event) => {
          event.preventDefault(); save.disabled = true;
          try {
            await act(() => api(`/tasks/${selectedTask}/goals/${goalItem.id}`, { method: 'PATCH', body: {
              text: textField.value.trim(), vulnclass: classField.value.trim(),
            } }), 'Goal updated');
          } finally { save.disabled = false; }
        });
        controls.append(button('Edit', () => { edit.hidden = !edit.hidden; }),
          button('Delete', () => {
            if (window.confirm('Delete this goal permanently?')) {
              act(() => api(`/tasks/${selectedTask}/goals/${goalItem.id}`, { method: 'DELETE' }), 'Goal deleted');
            }
          }));
        entry.append(controls, edit);
        goalsList.append(entry);
      }
      goalsPanel.append(goalsList);
      wrap.append(goalsPanel);

      const constraintsPanel = panel('Task constraints');
      constraintsPanel.append(element('p', 'cyber-detail',
        'These allow/deny instructions enter agent prompts. They are guidance, not enforced network or tool boundaries.'));
      const constraintForm = element('form', 'cyber-form');
      const constraintKind = selectOptions('Constraint type', [['allow', 'Allow'], ['deny', 'Deny']]);
      const constraintText = input('Constraint text', true); constraintText.maxLength = 5000;
      const addConstraint = element('button', 'primary', 'Add constraint'); addConstraint.type = 'submit';
      constraintForm.append(constraintKind, constraintText, addConstraint);
      constraintForm.addEventListener('submit', async (event) => {
        event.preventDefault(); addConstraint.disabled = true;
        try {
          await act(() => api(`/tasks/${selectedTask}/constraints`, { method: 'POST', body: {
            kind: constraintKind.value, text: constraintText.value.trim(),
          } }), 'Constraint added');
        } finally { addConstraint.disabled = false; }
      });
      constraintsPanel.append(constraintForm);
      const constraintsList = element('div', 'cyber-list');
      const constraints = Array.isArray(constraintsData.constraints) ? constraintsData.constraints : [];
      if (!constraints.length) constraintsList.append(empty('No task constraints recorded.'));
      for (const constraint of constraints) {
        const entry = panel('');
        entry.dataset.constraintId = String(constraint.id);
        entry.append(row(constraint.text || `Constraint #${constraint.id}`,
          `#${constraint.id} · ${constraint.origin || 'manual'}`, constraint.kind));
        const controls = element('div', 'cyber-actions');
        const edit = element('form', 'cyber-form'); edit.hidden = true;
        const kindField = selectOptions('Constraint type', [['allow', 'Allow'], ['deny', 'Deny']]);
        kindField.value = constraint.kind;
        const textField = input('Constraint text', true); textField.maxLength = 5000; textField.value = constraint.text || '';
        const save = element('button', 'primary', 'Save constraint'); save.type = 'submit';
        edit.append(kindField, textField, save);
        edit.addEventListener('submit', async (event) => {
          event.preventDefault(); save.disabled = true;
          try {
            await act(() => api(`/tasks/${selectedTask}/constraints/${constraint.id}`, { method: 'PATCH', body: {
              kind: kindField.value, text: textField.value.trim(),
            } }), 'Constraint updated');
          } finally { save.disabled = false; }
        });
        controls.append(button('Edit', () => { edit.hidden = !edit.hidden; }),
          button('Delete', () => {
            if (window.confirm('Delete this constraint?')) {
              act(() => api(`/tasks/${selectedTask}/constraints/${constraint.id}`, { method: 'DELETE' }), 'Constraint deleted');
            }
          }));
        entry.append(controls, edit);
        constraintsList.append(entry);
      }
      constraintsPanel.append(constraintsList);
      wrap.append(constraintsPanel);
    }
    return wrap;
  }

  async function renderLearning() {
    await loadTasks();
    if (!selectedTask) return empty('Create or select a task to view its learning record.');
    const taskId = selectedTask;
    const [outcome, lessonData, waveData, artifactData, handoffData, mapData, campaignData, huntData, chainData] = await Promise.all([
      api(`/tasks/${taskId}/outcome`), api(`/tasks/${taskId}/lessons`),
      api(`/tasks/${taskId}/waves`), api(`/tasks/${taskId}/artifacts`),
      api(`/tasks/${taskId}/c2-handoffs`),
      api(`/tasks/${taskId}/business-map`), api(`/tasks/${taskId}/campaign`),
      api(`/tasks/${taskId}/hunt-classes`), api(`/tasks/${taskId}/attack-chains`),
    ]);
    const wrap = element('div');
    wrap.append(taskPicker(refresh));

    const metrics = element('div', 'cyber-grid');
    metrics.append(
      card('Confirmed findings', outcome.findings?.confirmed ?? 0),
      card('Open findings', outcome.findings?.open ?? 0),
      card('Tool calls', outcome.tool_calls ?? 0),
      card('Skill gaps', outcome.skill_misses ?? 0),
      card('Output tokens', outcome.tokens?.output_tokens ?? 0),
      card('Wall time · seconds', outcome.wall_seconds ?? 0),
    );
    wrap.append(metrics);

    function listSection(title, items, emptyText, renderItem) {
      const section = panel(title);
      const list = element('div', 'cyber-list');
      if (!items.length) list.append(empty(emptyText));
      for (const item of items) list.append(renderItem(item));
      section.append(list);
      wrap.append(section);
      return section;
    }

    const lessons = Array.isArray(lessonData.lessons) ? lessonData.lessons : [];
    const lessonPanel = listSection(`Lessons · ${lessonData.mode || 'off'}`, lessons,
      'No evidence-backed lessons recorded for this task.', (lesson) => {
        const entry = panel('');
        entry.append(row(lesson.cue || `Lesson #${lesson.id}`,
          `${lesson.action || ''} · ${lesson.support || 0} supporting records`, lesson.kind));
        return entry;
      });
    const lessonActions = element('div', 'cyber-actions');
    lessonActions.append(button('Extract lessons', () => act(
      () => api(`/tasks/${taskId}/lessons/extract`, { method: 'POST', body: {} }),
      'Lesson extraction complete')));
    lessonPanel.append(lessonActions);

    const waves = Array.isArray(waveData.waves) ? waveData.waves : [];
    listSection('Follow-up waves', waves, 'No follow-up waves recorded for this task.', (wave) => {
      const item = panel('');
      item.append(row(`Wave #${wave.wave_number || wave.id}`,
        `${wave.reason || 'Follow-up'} · scheduled ${fmtDate(wave.scheduled_at)}`, wave.status));
      if (wave.detail) item.append(element('p', 'cyber-detail', wave.detail));
      if (wave.spawned_task_id) item.append(element('p', 'cyber-detail',
        `Follow-up task #${wave.spawned_task_id}`));
      return item;
    });

    const artifacts = Array.isArray(artifactData.artifacts) ? artifactData.artifacts : [];
    listSection('Evidence artifacts', artifacts, 'No evidence files indexed for this task.', (artifact) => {
      const item = panel('');
      item.append(row(artifact.path || `Artifact #${artifact.id}`,
        `${Math.ceil(Number(artifact.size_bytes || 0) / 1024)} KiB · SHA-256 ${artifact.sha256 || 'unavailable'}`,
        artifact.type));
      return item;
    });

    const businessMap = Array.isArray(mapData.business_map) ? mapData.business_map : [];
    listSection('Business map', businessMap, 'No business model entries recorded.', (entry) => {
      const item = panel('');
      item.append(row(entry.name || `Entry #${entry.id}`, `Updated ${fmtDate(entry.updated_at)}`, entry.kind));
      if (entry.detail && Object.keys(entry.detail).length) {
        item.append(element('pre', 'cyber-detail', JSON.stringify(entry.detail, null, 2)));
      }
      return item;
    });

    const branches = Array.isArray(campaignData.branches) ? campaignData.branches : [];
    listSection('Campaign branches', branches, 'No hypothesis branches planned yet.', (branch) => {
      const item = panel('');
      item.append(row(branch.hypothesis || branch.branch_id || `Branch #${branch.id}`,
        `${branch.branch_id || ''} · ${branch.goal || ''}`, branch.status));
      if (branch.probe_spec) item.append(element('p', 'cyber-detail', `Probe: ${branch.probe_spec}`));
      if (branch.expected_signal) item.append(element('p', 'cyber-detail', `Expected: ${branch.expected_signal}`));
      if (branch.reason) item.append(element('p', 'cyber-detail', `Result: ${branch.reason}`));
      return item;
    });

    const huntClasses = Array.isArray(huntData.hunt_classes) ? huntData.hunt_classes : [];
    listSection('Hunt coverage', huntClasses, 'No hunt-class board is enabled for this task.', (hunt) => {
      const item = panel('');
      item.append(row(hunt.class || `Class #${hunt.id}`,
        hunt.reason || hunt.evidence || 'No outcome recorded', hunt.status));
      return item;
    });

    const chains = Array.isArray(chainData.chains) ? chainData.chains : [];
    listSection('Attack chains', chains, 'No attack chains proposed yet.', (chain) => {
      const item = panel('');
      item.append(row(chain.name || `Chain #${chain.id}`,
        `${chain.impact || 'Impact unknown'} · ${chain.source_finding_ids?.length || 0} confirmed findings`, chain.status));
      if (Array.isArray(chain.steps)) {
        chain.steps.forEach((step, index) => item.append(element('p', 'cyber-detail',
          `${index + 1}. ${step.step || ''}: ${step.description || ''}`)));
      }
      if (chain.reason) item.append(element('p', 'cyber-detail', `Result: ${chain.reason}`));
      if (chain.spawned_task_id) item.append(element('p', 'cyber-detail',
        `Verification task #${chain.spawned_task_id}`));
      return item;
    });
    const handoffs = Array.isArray(handoffData.handoffs) ? handoffData.handoffs : [];
    listSection('C2 handoffs', handoffs, 'No C2 handoffs recorded for this task.', (handoff) => {
      const item = panel('');
      item.append(row(handoff.vector || `Handoff #${handoff.id}`,
        `${handoff.target_host || ''}${handoff.target_path || ''}`, handoff.status));
      if (handoff.binary_sha256) item.append(element('p', 'cyber-detail',
        `${handoff.binary_arch || 'Binary'} · SHA-256 ${handoff.binary_sha256}`));
      return item;
    });
    return wrap;
  }

  async function renderChat() {
    await loadTasks();
    if (!selectedTask) return empty('Create or select a task to open its chat.');
    const taskId = selectedTask;
    const [history, status] = await Promise.all([
      api(`/tasks/${taskId}/activity?limit=200`), api(`/tasks/${taskId}/chat/status`),
    ]);
    const wrap = element('div');
    wrap.append(taskPicker(() => { chatDraft = ''; refresh(); }));
    const chatPanel = panel('Main agent conversation');
    const controls = element('div', 'cyber-actions');
    controls.append(pill(status.running ? 'Responding' : 'Ready'),
      button('New conversation', () => act(
        () => api(`/tasks/${taskId}/chat/new-session`, { method: 'POST', body: {} }), 'New conversation started')),
      button('Refresh conversation', refresh));
    if (status.running) controls.append(button('Stop reply', () => act(
      () => api(`/tasks/${taskId}/chat/stop`, { method: 'POST', body: {} }), 'Stop requested')));
    chatPanel.append(controls);
    const transcript = element('div', 'cyber-transcript');
    const turns = (Array.isArray(history.items) ? history.items : []).filter((item) =>
      item.kind === 'user' || item.kind === 'text');
    if (!status.running && turns.at(-1)?.kind === 'text') chatPollUntil = 0;
    if (!turns.length) transcript.append(empty('No messages in this conversation.'));
    for (const item of turns) {
      const bubble = element('article', `cyber-message ${item.kind === 'user' ? 'user' : 'agent'}`);
      bubble.append(element('div', 'cyber-row-sub', `${item.kind === 'user' ? 'You' : 'APTEX'} · ${fmtDate(item.ts)}`),
        element('div', 'cyber-detail', item.summary || item.detail || ''));
      transcript.append(bubble);
    }
    chatPanel.append(transcript);
    const form = element('form', 'cyber-stack');
    const message = textarea('Message the task agent', true);
    message.maxLength = 10000; message.value = chatDraft;
    message.addEventListener('input', () => { chatDraft = message.value; });
    const send = element('button', 'primary', 'Send message'); send.type = 'submit';
    form.append(message, send);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      send.disabled = true;
      try {
        await api(`/tasks/${taskId}/chat`, { method: 'POST', body: { message: message.value.trim() } });
        chatDraft = '';
        chatPollUntil = Date.now() + 120000;
        await refresh();
        showNotice('Message accepted by APTEX');
      } catch (error) { showNotice(error.message, true); }
      finally { send.disabled = false; }
    });
    chatPanel.append(form);
    wrap.append(chatPanel);
    if (status.running || Date.now() < chatPollUntil) {
      const expectedGeneration = generation;
      setTimeout(() => {
        if (mode === 'cybersecurity' && tab === 'chat' && generation === expectedGeneration) refresh();
      }, 3000);
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
      const triage = element('form', 'cyber-form');
      const severity = selectOptions('Set severity', ['critical', 'high', 'medium', 'low']
        .map((value) => [value, value]));
      severity.value = finding.severity || 'medium';
      const status = selectOptions('Set status', [
        'pending', 'in_progress', 'confirmed', 'resolved', 'fixed',
        'false_positive', 'ignored', 'duplicate', 'risk_accepted',
      ].map((value) => [value, value.replaceAll('_', ' ')]));
      status.value = finding.status || 'pending';
      const save = element('button', 'primary', 'Save triage'); save.type = 'submit';
      triage.append(severity, status, save);
      triage.addEventListener('submit', async (event) => {
        event.preventDefault();
        save.disabled = true;
        try {
          await api(`/findings/${finding.id}`, { method: 'PATCH', body: {
            severity: severity.value, status: status.value,
          } });
          await refresh();
          await openFinding(finding.id);
          showNotice('Finding triage saved');
        } catch (error) { showNotice(error.message, true); }
        finally { save.disabled = false; }
      });
      detail.append(triage);
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
    if (!selectedTask) return empty('Select a task after one has been created in APTEX.');
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
    const addForm = element('form', 'cyber-form');
    const kind = selectOptions('Scope kind', [
      ['root_domain', 'Root domain'], ['subdomain', 'Subdomain'], ['ip', 'IP address'],
      ['cidr', 'CIDR network'], ['company', 'Company'], ['icp', 'ICP'], ['keyword', 'Keyword'],
    ]);
    const value = input('Scope value', true); value.maxLength = 500;
    const reason = input('Reason'); reason.maxLength = 1000;
    const add = element('button', 'primary', 'Add scope'); add.type = 'submit';
    addForm.append(kind, value, reason, add);
    addForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      add.disabled = true;
      try {
        await act(() => api(`/tasks/${selectedTask}/scope`, { method: 'POST', body: {
          kind: kind.value, value: value.value.trim(), reason: reason.value.trim(),
        } }), 'Scope added');
      } finally { add.disabled = false; }
    });
    scopePanel.append(addForm);
    const list = element('div', 'cyber-list');
    const entries = Array.isArray(scope.scope) ? scope.scope : [];
    if (!entries.length) list.append(empty('No scope rows recorded.'));
    for (const entry of entries) {
      const item = row(entry.company_name || entry.domain || entry.net || entry.value || `Scope #${entry.id}`,
        `${entry.kind} · ${entry.source}${entry.reason ? ` · ${entry.reason}` : ''}`, null);
      if (entry.source === 'manual' && entry.id) item.append(button('Remove', () => act(
        () => api(`/tasks/${selectedTask}/scope/${entry.id}`, { method: 'DELETE' }), 'Scope removed')));
      list.append(item);
    }
    scopePanel.append(list);
    wrap.append(scopePanel);
    return wrap;
  }

  async function renderAssets() {
    await loadTasks();
    const params = new URLSearchParams({
      task_id: selectedTask || '0', type: assetType, limit: '50', offset: String(assetOffset),
    });
    const data = await api(`/assets?${params}`);
    const wrap = element('div');
    if (selectedTask) wrap.append(taskPicker(() => { assetOffset = 0; refresh(); }));
    const toolbar = element('div', 'cyber-toolbar');
    const viewType = selectOptions('Asset type', [
      ['root_domain', 'Root domains'], ['subdomain', 'Subdomains'], ['ip', 'IP addresses'],
      ['service', 'Services'], ['endpoint', 'Endpoints'], ['app', 'Apps'],
    ]);
    viewType.value = assetType;
    viewType.addEventListener('change', () => { assetType = viewType.value; assetOffset = 0; refresh(); });
    toolbar.append(viewType);
    wrap.append(toolbar);
    if (selectedTask) {
      const addPanel = panel('Register task asset');
      const form = element('form', 'cyber-form');
      const kind = selectOptions('New asset type', [
        ['root_domain', 'Root domain'], ['subdomain', 'Subdomain'],
        ['ip', 'IP address'], ['service', 'HTTP service URL'],
      ]);
      const value = input('Domain, IP address, or URL', true); value.maxLength = 2000;
      const add = element('button', 'primary', 'Add asset'); add.type = 'submit';
      form.append(kind, value, add);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        add.disabled = true;
        try {
          const result = await api('/assets', { method: 'POST', body: {
            task_id: Number(selectedTask), kind: kind.value, value: value.value.trim(),
          } });
          if (Array.isArray(result.errors) && result.errors.length) {
            throw new Error(result.errors[0].error || 'Asset was rejected');
          }
          assetType = kind.value;
          assetOffset = 0;
          await refresh();
          showNotice('Asset registered');
        } catch (error) { showNotice(error.message, true); }
        finally { add.disabled = false; }
      });
      addPanel.append(form);
      wrap.append(addPanel);
    }
    const listPanel = panel(`Assets · ${data.total ?? 0}`);
    const list = element('div', 'cyber-list');
    const assets = Array.isArray(data.assets) ? data.assets : [];
    if (!assets.length) list.append(empty('No assets match this view.'));
    for (const asset of assets) {
      list.append(row(asset.domain || asset.ip || asset.url || asset.app_name || `Asset #${asset.id}`,
        `#${asset.id} · ${asset.type}${asset.last_seen ? ` · Last seen ${fmtDate(asset.last_seen)}` : ''}`,
        asset.status_code || null));
    }
    listPanel.append(list);
    const paging = element('div', 'cyber-actions');
    if (assetOffset > 0) paging.append(button('Previous', () => { assetOffset = Math.max(0, assetOffset - 50); refresh(); }));
    if (assetOffset + 50 < (data.total || 0)) paging.append(button('Next', () => { assetOffset += 50; refresh(); }));
    listPanel.append(paging);
    wrap.append(listPanel);
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
    await loadTasks();
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
    if (selectedTask) {
      wrap.append(taskPicker(refresh));
      const rulesPanel = panel('Task asset interception rules');
      rulesPanel.append(element('p', 'cyber-detail',
        'These rules govern task asset access. Declared scope alone does not block tool operations.'));
      const form = element('form', 'cyber-form');
      const action = selectOptions('Rule action', [['block', 'Block'], ['allow', 'Allow']]);
      const kind = selectOptions('Match kind', [
        ['exact_domain', 'Exact domain'], ['fuzzy_domain', 'Domain contains'],
        ['exact_ip', 'Exact IP'], ['fuzzy_ip', 'IP contains'], ['cidr', 'CIDR'],
        ['exact_url', 'Exact URL'], ['fuzzy_url', 'URL contains'],
      ]);
      const pattern = input('Match pattern', true); pattern.maxLength = 500;
      const note = input('Rule note'); note.maxLength = 1000;
      const add = element('button', 'primary', 'Add rule'); add.type = 'submit';
      form.append(action, kind, pattern, note, add);
      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        add.disabled = true;
        try {
          await act(() => api(`/tasks/${selectedTask}/rules`, { method: 'POST', body: {
            enabled: true, action: action.value, kind: kind.value,
            pattern: pattern.value.trim(), note: note.value.trim(),
          } }), 'Task rule added');
        } finally { add.disabled = false; }
      });
      rulesPanel.append(form);
      const rulesData = await api(`/tasks/${selectedTask}/rules`);
      const rulesList = element('div', 'cyber-list');
      const rules = Array.isArray(rulesData.rules) ? rulesData.rules : [];
      if (!rules.length) rulesList.append(empty('No task rules configured.'));
      for (const rule of rules) {
        const entry = row(`${rule.action || 'block'} · ${rule.kind || ''} · ${rule.pattern || ''}`,
          rule.note || `Rule #${rule.id}`, rule.enabled ? 'enabled' : 'disabled');
        const controls = element('div', 'cyber-actions');
        controls.append(button(rule.enabled ? 'Disable' : 'Enable', () => act(
          () => api(`/tasks/${selectedTask}/rules/${rule.id}/toggle`, {
            method: 'POST', body: { enabled: !rule.enabled },
          }), `Rule ${rule.enabled ? 'disabled' : 'enabled'}`)),
        button('Remove', () => act(
          () => api(`/tasks/${selectedTask}/rules/${rule.id}`, { method: 'DELETE' }), 'Rule removed')));
        entry.append(controls);
        rulesList.append(entry);
      }
      rulesPanel.append(rulesList);
      wrap.append(rulesPanel);
    }
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
        `${fmtDate(exchange.ts)} · ${exchange.host || ''}`, exchange.status,
        () => openTrafficExchange(exchange.id)));
    }
    trafficPanel.append(list);
    wrap.append(trafficPanel);
    return wrap;
  }

  async function openTrafficExchange(id) {
    try {
      const data = await api(`/traffic/exchange?id=${encodeURIComponent(id)}`);
      if (mode !== 'cybersecurity' || tab !== 'traffic') return;
      byId('cyber-traffic-detail')?.remove();
      const detail = panel(`Exchange ${id}`);
      detail.id = 'cyber-traffic-detail';
      detail.append(element('h4', '', 'Request'),
        element('pre', 'cyber-detail', JSON.stringify(data.req || {}, null, 2)),
        element('h4', '', 'Response'),
        element('pre', 'cyber-detail', JSON.stringify(data.resp || {}, null, 2)));
      content.prepend(detail);
    } catch (error) { showNotice(error.message, true); }
  }

  async function renderReports() {
    await loadTasks();
    if (!selectedTask) return empty('Create or select a task to view its report.');
    const taskId = selectedTask;
    const data = await api(`/tasks/${taskId}/report`);
    const wrap = element('div');
    wrap.append(taskPicker(refresh));
    const reportPanel = panel('Generated task report');
    const markdown = String(data.markdown || '');
    const actions = element('div', 'cyber-actions');
    actions.append(button('Download Markdown', () => {
      const url = URL.createObjectURL(new Blob([markdown], { type: 'text/markdown;charset=utf-8' }));
      const link = element('a');
      link.href = url;
      link.download = `afterglow-task-${String(taskId).replace(/\D/g, '')}-report.md`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }));
    reportPanel.append(actions);
    reportPanel.append(markdown ? element('pre', 'cyber-detail', markdown) : empty('No report content yet.'));
    wrap.append(reportPanel);
    return wrap;
  }

  async function renderModels() {
    const localBrowser = ['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname);
    const [engine, profileData, chatgptResult, poolResult] = await Promise.all([
      api('/models'), api('/models/profiles'),
      api('/models/chatgpt').then((value) => ({ value }), (error) => ({ error })),
      api('/models/pool').then((value) => ({ value }), (error) => ({ error })),
    ]);
    const wrap = element('div');
    const statusPanel = panel('Active engine');
    const metrics = element('div', 'cyber-grid');
    metrics.append(card('Configured', engine.configured ? 'Ready' : 'Not set'),
      card('Provider', engine.provider || '—'), card('Model', engine.model || '—'));
    statusPanel.append(metrics);
    if (engine.base_url) statusPanel.append(element('p', 'cyber-detail', `Endpoint: ${engine.base_url}`));
    wrap.append(statusPanel);
    const profilesPanel = panel('Model profiles');
    const profiles = Array.isArray(profileData.profiles) ? profileData.profiles : [];
    const profileList = element('div', 'cyber-list');
    if (!profiles.length) profileList.append(empty('No model profiles configured on APTEX.'));
    for (const profile of profiles) {
      const entry = row(profile.name || profile.model || `Profile #${profile.id}`,
        `${profile.format || 'provider'} · ${profile.model || 'No model'}${profile.base_url ? ` · ${profile.base_url}` : ''}`,
        profile.is_default ? 'active' : 'available');
      if (!profile.is_default) entry.append(button('Make active', () => act(
        () => api('/models/active', { method: 'POST', body: { id: profile.id } }), 'Model profile activated')));
      if (profile.format === 'ollama' && LOCAL_OLLAMA_ENDPOINTS.has(profile.base_url)) entry.append(button('Test local model', async () => {
        try {
          const result = await api(`/models/local/profiles/${profile.id}/test`, { method: 'POST' });
          showNotice(result.ok ? `${profile.model} ready · ${result.gpu_offload_percent ?? 'unknown'}% GPU offload` :
            (result.error || 'Local model test failed'), !result.ok);
        } catch (error) { showNotice(error.message, true); }
      }));
      if (localBrowser && profile.auth_mode === 'chatgpt_plan') entry.append(button('Recheck ChatGPT', async () => {
        try {
          const result = await api(`/models/chatgpt/profiles/${profile.id}/recheck`, { method: 'POST' });
          await refresh();
          showNotice(result.ok ? (result.reply || `${profile.model} ready`) : (result.error || 'ChatGPT recheck failed'), !result.ok);
        } catch (error) { showNotice(error.message, true); }
      }));
      profileList.append(entry);
    }
    profilesPanel.append(profileList);
    wrap.append(profilesPanel);
    const localPanel = panel('Add a local Ollama profile');
    localPanel.append(element('p', 'cyber-detail',
      'Choose an Ollama endpoint reachable from the APTEX server. Discovery lists installed models; it does not download one.'));
    const localForm = element('form', 'cyber-stack');
    const target = selectOptions('Ollama location', [
      ['http://127.0.0.1:11434', 'Ollama on the APTEX host'],
      ['http://host.docker.internal:11434', 'Ollama on the Docker host'],
      ['http://ollama:11434', 'Ollama container'],
    ]);
    const model = input('Installed model tag', true); model.maxLength = 200;
    const suggestions = element('datalist');
    suggestions.id = 'cyber-local-model-options';
    model.setAttribute('list', suggestions.id);
    const name = input('Profile name', true); name.maxLength = 160;
    const localActions = element('div', 'cyber-actions');
    const discover = button('Find installed models', async () => {
      discover.disabled = true;
      try {
        const result = await api('/models/local/discover', { method: 'POST', body: { base_url: target.value } });
        if (!result.ok) throw new Error(result.error || 'Model discovery failed');
        suggestions.replaceChildren();
        for (const tag of Array.isArray(result.models) ? result.models : []) {
          const option = element('option'); option.value = String(tag); suggestions.append(option);
        }
        if (result.models?.length) model.value = String(result.models[0]);
        showNotice(result.models?.length ? `${result.models.length} installed model(s) found` : 'No installed models found');
      } catch (error) { showNotice(error.message, true); }
      finally { discover.disabled = false; }
    });
    const create = element('button', 'primary', 'Create local profile'); create.type = 'submit';
    localActions.append(discover, create);
    localForm.append(target, model, suggestions, name, localActions);
    localForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      create.disabled = true;
      try {
        await api('/models/local/profiles', { method: 'POST', body: {
          base_url: target.value, name: name.value.trim(), model: model.value.trim(),
        } });
        await refresh();
        showNotice('Local model profile created');
      } catch (error) { showNotice(error.message, true); }
      finally { create.disabled = false; }
    });
    localPanel.append(localForm);
    wrap.append(localPanel);
    if (poolResult.value) {
      const poolPanel = panel('Model failover');
      poolPanel.append(element('pre', 'cyber-detail', JSON.stringify(poolResult.value, null, 2)));
      wrap.append(poolPanel);
    }
    const chatgptPanel = panel('ChatGPT connection');
    if (chatgptResult.error) chatgptPanel.append(empty(`Status unavailable: ${chatgptResult.error.message}`));
    else {
      const accounts = Array.isArray(chatgptResult.value?.accounts) ? chatgptResult.value.accounts : [];
      chatgptPanel.append(element('p', 'cyber-detail',
        'ChatGPT plan connection uses APTEX’s local OAuth callback. Open this page on the APTEX machine to connect an account.'));
      if (localBrowser) chatgptPanel.append(button('Connect ChatGPT', async () => {
        const popup = window.open('about:blank', '_blank');
        if (popup) popup.opener = null;
        try {
          const result = await api('/models/chatgpt/connect', { method: 'POST', body: { account_id: '' } });
          const auth = new URL(result.url);
          if (auth.origin !== 'https://auth.openai.com' || auth.pathname !== '/api/accounts/authorize')
            throw new Error('Invalid ChatGPT authorization URL');
          if (popup) popup.location.replace(result.url);
          else window.location.assign(result.url);
          await refresh();
          showNotice('Complete ChatGPT sign-in in the new window');
        } catch (error) { popup?.close(); showNotice(error.message, true); }
      }));
      if (!accounts.length) chatgptPanel.append(empty('No ChatGPT account connected on APTEX.'));
      for (const account of accounts) {
        const entry = row(account.email || account.id || 'ChatGPT account',
          account.plan_block_reason || (account.plan_enabled ? 'Plan enabled' : 'Plan unavailable'),
          account.connected ? 'connected' : 'disconnected');
        if (localBrowser) entry.append(button('Disconnect', () => act(
          () => api('/models/chatgpt/disconnect', { method: 'POST', body: { account_id: account.id } }),
          'ChatGPT account disconnected')));
        chatgptPanel.append(entry);
      }
      if (chatgptResult.value?.error) chatgptPanel.append(element('p', 'cyber-detail', chatgptResult.value.error));
      if (chatgptResult.value?.pending) {
        chatgptPanel.append(element('p', 'cyber-detail', 'Waiting for ChatGPT sign-in…'));
        const expectedGeneration = generation;
        setTimeout(() => {
          if (mode === 'cybersecurity' && tab === 'models' && generation === expectedGeneration) refresh();
        }, 3000);
      }
      const connected = accounts.filter((account) => account.connected && account.plan_enabled);
      if (connected.length) {
        const form = element('form', 'cyber-stack');
        const account = selectOptions('ChatGPT account', connected.map((item) => [item.id, item.email || item.id]));
        if (connected.some((item) => item.id === chatgptAccount)) account.value = chatgptAccount;
        else chatgptAccount = account.value;
        const model = selectOptions('ChatGPT model', [['', 'Choose a model']]);
        model.disabled = true;
        const name = input('ChatGPT profile name', true); name.maxLength = 160;
        name.value = chatgptName;
        name.addEventListener('input', () => { chatgptName = name.value; });
        const save = element('button', 'primary', 'Create ChatGPT profile'); save.type = 'submit';
        save.disabled = true;
        async function loadCatalog(force = false) {
          chatgptAccount = account.value;
          const accountID = account.value;
          model.disabled = true;
          save.disabled = true;
          model.replaceChildren(element('option', '', 'Loading models…'));
          try {
            if (force) chatgptCatalog.delete(accountID);
            let items = chatgptCatalog.get(accountID);
            if (!items) {
              const data = await api(`/models/chatgpt/catalog?account_id=${encodeURIComponent(accountID)}`);
              items = Array.isArray(data.models) ? data.models : [];
              chatgptCatalog.set(accountID, items);
            }
            if (account.value !== accountID) return;
            model.replaceChildren();
            for (const item of items) {
              const option = element('option', '', item.name || item.id);
              option.value = item.id; model.append(option);
            }
            if (items.some((item) => item.id === chatgptModel)) model.value = chatgptModel;
            else chatgptModel = model.value;
            if (!items.length) model.append(element('option', '', 'No models available'));
            model.disabled = !items.length;
            save.disabled = !items.length;
          } catch (error) {
            model.replaceChildren(element('option', '', 'Models unavailable'));
            if (account.value === accountID) showNotice(error.message, true);
          }
        }
        account.addEventListener('change', () => { chatgptModel = ''; loadCatalog(); });
        model.addEventListener('change', () => { chatgptModel = model.value; });
        form.append(account, model, name, button('Load ChatGPT models', () => loadCatalog(true)), save);
        form.addEventListener('submit', async (event) => {
          event.preventDefault();
          save.disabled = true;
          try {
            await api('/models/chatgpt/profiles', { method: 'POST', body: {
              account_id: account.value, model: model.value, name: name.value.trim(),
            } });
            await refresh();
            showNotice('ChatGPT profile created');
          } catch (error) { showNotice(error.message, true); }
          finally { save.disabled = false; }
        });
        chatgptPanel.append(form);
        if (chatgptCatalog.has(account.value)) loadCatalog();
      }
    }
    wrap.append(chatgptPanel);
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
    content.replaceChildren(empty('Loading APTEX…'));
    setConnection('connecting', 'Connecting');
    let connected = false;
    try {
      const status = await api('/status');
      if (run !== generation || mode !== 'cybersecurity') return;
      connected = true;
      setConnection('connected', status.health?.ok ? 'Connected' : 'Connected · degraded');
      if (consoleLink) {
        consoleLink.hidden = !status.console_url;
        if (status.console_url) consoleLink.href = status.console_url;
        else consoleLink.removeAttribute('href');
      }
      let view;
      if (tab === 'overview') view = await renderOverview(status);
      else if (tab === 'tasks') view = await renderTasks();
      else if (tab === 'learning') view = await renderLearning();
      else if (tab === 'chat') view = await renderChat();
      else if (tab === 'findings') view = await renderFindings();
      else if (tab === 'scope') view = await renderScope();
      else if (tab === 'assets') view = await renderAssets();
      else if (tab === 'activity') view = await renderActivity();
      else if (tab === 'intercept') view = await renderIntercept();
      else if (tab === 'traffic') view = await renderTraffic();
      else if (tab === 'reports') view = await renderReports();
      else view = await renderModels();
      if (run !== generation || mode !== 'cybersecurity') return;
      content.replaceChildren(view);
      showNotice(status.console_error || '', !!status.console_error);
    } catch (error) {
      if (run !== generation || mode !== 'cybersecurity') return;
      if (!connected) setConnection('disconnected', 'Unavailable');
      content.replaceChildren(empty('APTEX data is unavailable. Check the server connection and credential.'));
      showNotice(error.message, true);
    }
  }

  odyButton.addEventListener('click', () => setMode('odysseus'));
  cyberButton.addEventListener('click', () => setMode('cybersecurity'));
  byId('cyber-refresh').addEventListener('click', refresh);
  tabs.forEach((item) => item.addEventListener('click', () => selectTab(item.dataset.cyberTab)));
  setMode(mode);
}
