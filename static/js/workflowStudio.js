import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';
import { createWorkflowConversion } from './workflowConversion.js';

// Presentation only. Existing forms, upload objects and queue controllers remain
// the owners of execution state. Changing this view never submits a workflow.
const ORDER = ['inputs', 'prepare', 'generate', 'review', 'refine', 'export'];
const LABELS = { inputs: 'Inputs', prepare: 'Prepare', setup: 'Model setup', generate: 'Generate', review: 'Review', refine: 'Refine', export: 'Export' };
let nextId = 0;
const node = (tag, cls = '', text) => {
  const element = document.createElement(tag); element.className = cls;
  if (text !== undefined) element.textContent = text;
  return element;
};
const control = (label, callback, cls = '') => {
  const result = node('button', `workflow-studio-button ${cls}`, label);
  result.type = 'button'; result.addEventListener('click', callback); return result;
};
const icon = name => {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('aria-hidden', 'true'); svg.classList.add('workflow-studio-icon');
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', name === 'arrow' ? 'M4 12h15m-6-6 6 6-6 6' : name === 'nodes' ? 'M3 3h6v6H3zm12 12h6v6h-6zM6 9v9h9M15 3h6v6h-6zM9 6h6' : 'M4 5h16M4 12h16M4 19h16');
  svg.append(path); return svg;
};
const textValue = value => typeof value === 'object' && value ? String(value.name || value.label || value.value || '') : String(value ?? '');

export function workflowStudioSummary(source = {}) {
  const setup = source.setup || {};
  const loras = (source.loras || setup.loras || []).map(item => ({
    name: textValue(item.name || item.label || item.lora || 'LoRA'),
    strength: Number(item.strength ?? item.scale ?? 1),
    enabled: item.enabled !== false,
  }));
  return {
    model: textValue(source.model || setup.model || 'Choose a model'),
    encoder: textValue(source.encoder || setup.encoder || 'Text encoder'),
    videoVae: textValue(source.videoVae || source.video_vae || setup.videoVae || setup.video_vae || 'Video VAE'),
    audioVae: textValue(source.audioVae || source.audio_vae || setup.audioVae || setup.audio_vae || 'Audio VAE'),
    loras,
    activeLoras: loras.filter(item => item.enabled && Number.isFinite(item.strength) && item.strength !== 0).length,
    mode: textValue(source.mode || ''),
    inputs: Number(source.inputCount ?? source.inputs?.count ?? 0),
    jobs: Number(source.jobCount ?? source.jobs?.count ?? 0),
    generation: [source.width && source.height ? `${source.width} × ${source.height}` : '', source.steps ? `${source.steps} steps` : ''].filter(Boolean).join(' · '),
  };
}

export function workflowStudioStageForControl(element, panels) {
  return panels.find(panel => panel.node === element || panel.node.contains(element))?.id || null;
}

export function createWorkflowStudio({ container, dialog, form, sections = [], getSummary = () => ({}), getPreview = () => null,
  family = 'h3', recipe = '', actions = [], persistentNodes = [], onApplyConverted } = {}) {
  const id = `workflow-studio-${++nextId}`;
  const storageKey = `odysseus-workflow-studio-v1:${family}:${recipe || 'default'}`;
  let saved = {}; try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch {}
  let closed = false, view = saved.view === 'nodes' ? 'nodes' : 'pipeline', selected = saved.selected || 'generate';
  let expanded = saved.expanded === true, zoom = 1, zoomTouched = false, selectedNode = selected, summary = workflowStudioSummary(), queuedRefresh = false;
  let previewIdentity = null, previewUrl = null, previewPlayer = null;
  const stageNodes = new Map(), graphNodes = new Map(), panels = [];
  const remember = () => { try { localStorage.setItem(storageKey, JSON.stringify({ view, selected, expanded })); } catch {} };
  if (!document.getElementById('workflow-studio-styles')) {
    const link = node('link'); link.id = 'workflow-studio-styles'; link.rel = 'stylesheet'; link.href = '/static/workflow-studio.css'; document.head.append(link);
  }
  dialog?.classList.add('workflow-studio-dialog'); container.classList.add('workflow-studio-container');
  const root = node('section', 'workflow-studio'); root.dataset.workflowFamily = family; root.dataset.workflowRecipe = recipe;
  root.setAttribute('aria-label', 'Workflow studio');
  container.insertBefore(root, form?.parentElement === container ? form : null);
  const toolbar = node('div', 'workflow-studio-toolbar');
  const tabs = node('div', 'workflow-studio-views'); tabs.setAttribute('aria-label', 'Workflow view');
  const pipelineTab = control('Pipeline', () => setView('pipeline')), nodeTab = control('Node map', () => setView('nodes'));
  pipelineTab.prepend(icon('pipeline')); nodeTab.prepend(icon('nodes')); tabs.append(pipelineTab, nodeTab);
  const recipeLabel = node('span', 'workflow-studio-recipe', recipe === 'seed_hunter' ? 'H3 Seed Hunter' : family === 'bfs' ? 'BFS video workflow' : 'H3 video workflow');
  toolbar.append(tabs, recipeLabel); root.append(toolbar);
  const conversion = onApplyConverted ? createWorkflowConversion({ family, recipe, onApply:onApplyConverted }) : null;
  if (conversion) { toolbar.append(conversion.button); root.append(conversion.panel); }
  const notices = node('div', 'workflow-studio-notices'); root.append(notices);
  for (const item of persistentNodes.filter(Boolean)) notices.append(item);
  const workspace = node('div', 'workflow-studio-workspace'); root.append(workspace);
  const navigation = node('nav', 'workflow-studio-stages'); navigation.setAttribute('aria-label', 'Workflow stages');
  const canvas = node('div', 'workflow-studio-canvas');
  const inspector = node('section', 'workflow-studio-inspector'); inspector.setAttribute('aria-label', 'Workflow settings');
  const inspectorHeader = node('div', 'workflow-studio-inspector-header'), inspectorTitle = node('h3');
  inspectorHeader.append(inspectorTitle); inspector.append(inspectorHeader);
  workspace.append(navigation, canvas, inspector);
  // The original form itself moves; inputs never leave their owning form.
  // Separate refinement/queue forms remain siblings, never nested in it.
  if (form) {
    if (!form.id) form.id = `${id}-form`;
    form.classList.add('workflow-studio-form'); inspector.append(form);
  }
  for (const definition of sections) {
    const panel = node('section', 'workflow-studio-panel'); panel.dataset.studioSection = definition.id;
    panel.id = `${id}-${definition.id}`; panel.setAttribute('aria-label', definition.title || LABELS[definition.id] || definition.id);
    const owner = definition.parent || (definition.nodes?.some(item => form?.contains(item)) ? form : container);
    const destination = owner === form ? form : inspector;
    for (const item of definition.nodes || (definition.node ? [definition.node] : [])) {
      if (!item || item === form || item === root || item.contains?.(root)) continue;
      panel.append(item);
    }
    destination.append(panel);
    panels.push({ ...definition, node: panel });
  }
  const available = new Set(panels.map(panel => panel.id));
  if (!available.has(selected)) selected = available.has('generate') ? 'generate' : panels[0]?.id;
  for (const [index, key] of ORDER.filter(key => available.has(key)).entries()) {
    const stage = control('', () => select(key)); stage.dataset.studioStage = key;
    const count = node('span', 'workflow-studio-stage-number', String(index + 1));
    const copy = node('span'), title = node('strong', '', panels.find(panel => panel.id === key)?.title || LABELS[key]);
    const detail = node('small'); copy.append(title, detail); stage.append(count, copy);
    stage.setAttribute('aria-controls', `${id}-${key}`); navigation.append(stage); stageNodes.set(key, { button: stage, detail });
  }
  if (available.has('setup')) {
    const setup = control('Model setup', () => select('setup'), 'workflow-studio-setup-button');
    setup.dataset.studioStage = 'setup'; setup.setAttribute('aria-controls', `${id}-setup`); navigation.append(setup);
    stageNodes.set('setup', { button: setup, detail: null });
  }

  const pipeline = node('div', 'workflow-studio-pipeline'); canvas.append(pipeline);
  const route = node('section', 'workflow-studio-model-route'); route.setAttribute('aria-label', 'Model connection to generation');
  const routeHeading = node('div', 'workflow-studio-route-heading'); routeHeading.append(node('strong', '', 'Generation model path'));
  const routeScope = node('span', '', 'Shared draft'); routeHeading.append(routeScope); route.append(routeHeading);
  const routeFlow = node('div', 'workflow-studio-route-flow');
  const modelButton = control('', () => select(available.has('setup') ? 'setup' : 'generate', { focus: true }), 'workflow-studio-route-card');
  const modelName = node('small'); modelButton.append(node('strong', '', family === 'bfs' ? 'Base model' : 'Base H3 model'), modelName);
  const adapterBox = node('div', 'workflow-studio-route-stack');
  adapterBox.append(node('strong', '', 'Applied LoRAs')); const adapterList = node('div'); adapterBox.append(adapterList);
  const renderButton = control('', () => select('generate'), 'workflow-studio-route-card workflow-studio-route-target');
  renderButton.append(node('strong', '', 'Generate'), node('small', '', 'Uses this model setup'));
  routeFlow.append(modelButton, icon('arrow'), adapterBox, icon('arrow'), renderButton); route.append(routeFlow);
  const modelResult = node('p', 'workflow-studio-route-result'); route.append(modelResult); pipeline.append(route);
  const preview = node('section', 'workflow-studio-preview'); preview.setAttribute('aria-label', 'Media preview');
  const previewHeader = node('div', 'workflow-studio-preview-header'), previewTitle = node('span', '', 'Source preview');
  previewHeader.append(previewTitle, control('Choose inputs', () => select('inputs'))); preview.append(previewHeader);
  const previewBody = node('div', 'workflow-studio-preview-body'); preview.append(previewBody);
  const previewEmpty = node('div', 'workflow-studio-preview-empty');
  previewEmpty.append(node('strong', '', 'Your media, in view'), node('p', '', 'Choose media in Inputs to preview it here.'), control('Open inputs', () => select('inputs')));
  previewBody.append(previewEmpty); pipeline.append(preview);

  const graph = node('div', 'workflow-studio-graph'); canvas.append(graph);
  const graphToolbar = node('div', 'workflow-studio-graph-toolbar');
  const expand = control('', () => { expanded = !expanded; remember(); buildGraph(); refresh(); });
  const zoomControls = node('div', 'workflow-studio-zoom');
  const zoomLabel = node('span', '', '100%');
  const zoomOut = control('−', () => { zoomTouched = true; setZoom(zoom - 0.15); }); zoomOut.setAttribute('aria-label', 'Zoom out');
  const zoomIn = control('+', () => { zoomTouched = true; setZoom(zoom + 0.15); }); zoomIn.setAttribute('aria-label', 'Zoom in');
  zoomControls.append(zoomOut, zoomLabel, zoomIn, control('Fit', () => { zoomTouched = false; fitGraph(); }));
  graphToolbar.append(expand, zoomControls); graph.append(graphToolbar);
  const viewport = node('div', 'workflow-studio-graph-viewport'); viewport.tabIndex = 0; viewport.setAttribute('aria-label', 'Workflow map. Scroll to explore connections.');
  const graphSpace = node('div', 'workflow-studio-graph-space'), graphContent = node('div', 'workflow-studio-graph-content');
  const wires = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); wires.classList.add('workflow-studio-wires');
  wires.setAttribute('viewBox', '0 0 1000 490'); wires.setAttribute('aria-hidden', 'true'); graphContent.append(wires);
  graphSpace.append(graphContent); viewport.append(graphSpace); graph.append(viewport);
  const footer = node('div', 'workflow-studio-footer');
  for (const action of actions.filter(Boolean)) {
    // Native submit behavior is retained when the action bar moves outside form.
    for (const submit of action.querySelectorAll('button[type="submit"], input[type="submit"]')) if (form) submit.setAttribute('form', form.id);
    if (action.matches?.('button[type="submit"], input[type="submit"]') && form) action.setAttribute('form', form.id);
    footer.append(action);
  }
  if (footer.childElementCount) root.append(footer);

  function select(key, { focus = false, nodeKey = key } = {}) {
    if (closed || !available.has(key)) return;
    selected = key; selectedNode = nodeKey; remember();
    for (const panel of panels) panel.node.hidden = panel.id !== key;
    if (form) form.hidden = !panels.some(panel => panel.id === key && form.contains(panel.node));
    for (const [stage, entry] of stageNodes) entry.button.setAttribute('aria-current', stage === key ? 'step' : 'false');
    for (const [graphKey, entry] of graphNodes) entry.button.setAttribute('aria-pressed', String(graphKey === selectedNode));
    inspectorTitle.textContent = panels.find(panel => panel.id === key)?.title || LABELS[key] || key;
    root.dataset.stage = key;
    if (focus) {
      const target = panels.find(panel => panel.id === key)?.node.querySelector('input:not([type="hidden"]):not(:disabled),textarea:not(:disabled),select:not(:disabled),button:not(:disabled)');
      target?.focus({ preventScroll: true });
    }
    inspector.scrollTop = 0;
  }

  function setView(next) {
    if (closed) return; view = next; root.dataset.view = view;
    pipeline.hidden = view !== 'pipeline'; graph.hidden = view !== 'nodes'; navigation.hidden = view !== 'pipeline';
    pipelineTab.setAttribute('aria-pressed', String(view === 'pipeline')); nodeTab.setAttribute('aria-pressed', String(view === 'nodes'));
    remember(); requestAnimationFrame(() => { if (!zoomTouched) fitGraph(); drawWires(); });
  }

  function graphButton(key, title, stage, x, y, kind = 'media') {
    const button = control('', () => select(stage, { focus: true, nodeKey:key }), `workflow-studio-node workflow-studio-node-${kind}`);
    button.dataset.studioNode = key; button.style.left = `${x}px`; button.style.top = `${y}px`;
    const heading = node('strong', '', title), detail = node('small'); button.append(heading, detail);
    graphContent.append(button); graphNodes.set(key, { button, detail, stage });
  }

  function buildGraph() {
    graphNodes.clear(); graphContent.querySelectorAll('.workflow-studio-node,.workflow-studio-hub-label').forEach(item => item.remove());
    expand.textContent = expanded ? 'Collapse setup hub' : 'Expand setup hub'; expand.setAttribute('aria-expanded', String(expanded));
    const setupStage = available.has('setup') ? 'setup' : 'generate';
    if (expanded) {
      const label = node('span', 'workflow-studio-hub-label', 'Model setup hub'); graphContent.append(label);
      graphButton('model', 'Model', setupStage, 40, 45, 'setup');
      graphButton('loras', 'LoRA stack', setupStage, 285, 45, 'setup');
      graphButton('encoder', 'Text encoder', setupStage, 530, 45, 'setup');
      graphButton('vaes', 'Video / audio VAEs', setupStage, 775, 45, 'setup');
    } else graphButton('setup', 'Model setup hub', setupStage, 530, 45, 'setup');
    const steps = ['inputs', 'prepare', 'generate', 'review'].filter(key => available.has(key));
    steps.forEach((key, index) => graphButton(key, LABELS[key], key, 40 + index * 245, 225, key === 'generate' ? 'generate' : 'media'));
    if (available.has('refine')) graphButton('refine', 'Refine selected', 'refine', 775, 405, 'refine');
    if (available.has('export')) graphButton('export', 'Export', 'export', available.has('refine') ? 285 : 775, 405, 'media');
    setZoom(zoom); requestAnimationFrame(drawWires);
  }

  function setZoom(value) {
    zoom = Math.max(0.35, Math.min(1.6, value)); graphContent.style.transform = `scale(${zoom})`;
    graphSpace.style.width = `${1000 * zoom}px`; graphSpace.style.height = `${500 * zoom}px`;
    zoomLabel.textContent = `${Math.round(zoom * 100)}%`; requestAnimationFrame(drawWires);
  }
  function fitGraph() { if (view === 'nodes' && viewport.clientWidth) setZoom(Math.min(1, (viewport.clientWidth - 24) / 1000)); }

  function drawWires() {
    if (closed || view !== 'nodes' || !graphContent.isConnected) return;
    const svgNS = wires.namespaceURI; wires.replaceChildren();
    const defs = document.createElementNS(svgNS, 'defs'), marker = document.createElementNS(svgNS, 'marker');
    marker.id = `${id}-arrow`; marker.setAttribute('viewBox', '0 0 10 10'); marker.setAttribute('refX', '9'); marker.setAttribute('refY', '5');
    marker.setAttribute('markerWidth', '5'); marker.setAttribute('markerHeight', '5'); marker.setAttribute('orient', 'auto');
    const arrow = document.createElementNS(svgNS, 'path'); arrow.setAttribute('d', 'M0 0 10 5 0 10Z'); arrow.setAttribute('fill', 'currentColor'); marker.append(arrow); defs.append(marker); wires.append(defs);
    const bounds = graphContent.getBoundingClientRect();
    const point = (key, side) => {
      const box = graphNodes.get(key)?.button.getBoundingClientRect(); if (!box) return null;
      return { x: ((side === 'left' ? box.left : side === 'right' ? box.right : box.left + box.width / 2) - bounds.left) / zoom,
        y: ((side === 'top' ? box.top : side === 'bottom' ? box.bottom : box.top + box.height / 2) - bounds.top) / zoom };
    };
    const line = (d, cls = '', arrow = true) => { const path = document.createElementNS(svgNS, 'path'); path.setAttribute('d', d); path.setAttribute('class', cls); if (arrow) path.setAttribute('marker-end', `url(#${id}-arrow)`); wires.append(path); };
    const connect = (a, b, sideA = 'right', sideB = 'left', cls = '') => {
      const p = point(a, sideA), q = point(b, sideB); if (!p || !q) return;
      line(sideA === 'bottom' ? `M${p.x} ${p.y}V${(p.y + q.y) / 2}H${q.x}V${q.y}` : `M${p.x} ${p.y}H${(p.x + q.x) / 2}V${q.y}H${q.x}`, cls);
    };
    const flow = ['inputs', 'prepare', 'generate', 'review'].filter(key => graphNodes.has(key));
    for (let i = 0; i < flow.length - 1; i++) connect(flow[i], flow[i + 1]);
    if (expanded) {
      // Four short joins collect into a named setup bus; only one bundle enters
      // Generate. Collapsing the hub doesn't alter any component binding.
      const target = point('generate', 'top');
      connect('model', 'loras', 'right', 'left', 'workflow-studio-wire-setup');
      for (const key of ['loras', 'encoder', 'vaes']) {
        const p = point(key, 'bottom'); if (p && target) line(`M${p.x} ${p.y}V175H${target.x}`, 'workflow-studio-wire-setup', false);
      }
      if (target) line(`M${target.x} 175V${target.y}`, 'workflow-studio-wire-bundle');
    } else connect('setup', 'generate', 'bottom', 'top', 'workflow-studio-wire-bundle');
    if (available.has('refine')) { connect('review', 'refine', 'bottom', 'top'); connect('refine', 'export', 'left', 'right'); }
    else connect('review', 'export', 'bottom', 'top');
  }

  function updatePreview() {
    let item; try { item = getPreview(); } catch { item = null; }
    const identity = item?.file || item?.url || null;
    if (identity === previewIdentity) { if (item?.label) previewTitle.textContent = item.label; return; }
    previewIdentity = identity; releaseVideoJobMedia(previewBody); if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null; previewPlayer = null; previewBody.replaceChildren();
    if (!identity) { previewBody.append(previewEmpty); previewTitle.textContent = 'Source preview'; return; }
    let url;
    if (item.file instanceof Blob) { previewUrl = URL.createObjectURL(item.file); url = previewUrl; }
    else {
      try { const parsed = new URL(item.url, window.location.origin); if (parsed.origin !== window.location.origin) throw new Error('External preview'); url = parsed.href; }
      catch { previewBody.append(previewEmpty); return; }
    }
    previewTitle.textContent = item.label || item.file?.name || 'Source preview';
    const kind = item.type || (item.file?.type?.startsWith('image/') ? 'image' : item.file?.type?.startsWith('audio/') ? 'audio' : 'video');
    if (kind === 'image') {
      const image = node('img'); image.alt = previewTitle.textContent; image.src = url; previewBody.append(image);
    } else {
      previewPlayer = createVideoJobMedia(url, { type: kind === 'audio' ? 'audio' : 'video', label: 'Play preview' }); previewBody.append(previewPlayer.node);
    }
  }

  function refresh() {
    if (closed) return;
    try { summary = workflowStudioSummary(getSummary() || {}); } catch { summary = workflowStudioSummary(); }
    modelName.textContent = summary.model; modelName.title = summary.model;
    // Stable buttons prevent a change/blur event from consuming the next click.
    const existing = [...adapterList.children];
    summary.loras.forEach((lora, index) => {
      let row = existing[index];
      if (!row || row.tagName !== 'BUTTON') { row = control('', () => select(available.has('setup') ? 'setup' : 'generate', { focus: true }), 'workflow-studio-adapter'); existing[index]?.replaceWith(row); if (!row.parentNode) adapterList.append(row); }
      row.textContent = `${lora.enabled && lora.strength !== 0 ? '●' : '○'} ${lora.name} · ${lora.enabled ? lora.strength : 'Off'}`;
      row.title = lora.name; row.classList.toggle('inactive', !lora.enabled || lora.strength === 0);
    });
    if (summary.loras.length) {
      for (const extra of [...adapterList.children].slice(summary.loras.length)) extra.remove();
    } else {
      // Keep the empty-stack action stable across polling and form input, too.
      // Replacing a button between pointerdown and click can swallow that click.
      let add = adapterList.firstElementChild;
      if (!add) { add = control('', () => select(available.has('setup') ? 'setup' : 'generate', { focus: true }), 'workflow-studio-adapter'); adapterList.append(add); }
      add.textContent = 'Add LoRA'; add.removeAttribute('title'); add.classList.remove('inactive');
      for (const extra of [...adapterList.children].slice(1)) extra.remove();
    }
    modelResult.textContent = summary.activeLoras ? `One model + ${summary.activeLoras} active LoRA${summary.activeLoras === 1 ? '' : 's'} → Generate` : 'Base model → Generate';
    stageNodes.get('inputs')?.detail && (stageNodes.get('inputs').detail.textContent = summary.inputs ? `${summary.inputs} inputs` : 'Media & batch');
    stageNodes.get('prepare')?.detail && (stageNodes.get('prepare').detail.textContent = 'Prompt & preparation');
    stageNodes.get('generate')?.detail && (stageNodes.get('generate').detail.textContent = summary.generation || 'Render settings');
    stageNodes.get('review')?.detail && (stageNodes.get('review').detail.textContent = summary.jobs ? `${summary.jobs} jobs total` : 'Jobs & results');
    stageNodes.get('refine')?.detail && (stageNodes.get('refine').detail.textContent = 'Selected preview');
    stageNodes.get('export')?.detail && (stageNodes.get('export').detail.textContent = 'Save & import');
    const graphDetails = { setup: `${summary.model} · ${summary.activeLoras} LoRAs`, model: summary.model,
      loras: summary.loras.length ? summary.loras.map(item => `${item.name} ${item.enabled ? item.strength : 'Off'}`).join(' · ') : 'No adapters applied',
      encoder: summary.encoder, vaes: `${summary.videoVae} / ${summary.audioVae}`, inputs: summary.mode || 'Media & batch sources',
      prepare: 'Prompt & source settings', generate: summary.generation || 'Render settings', review: 'Jobs · preview · retry', refine: 'Selected result settings', export: 'Odysseus / ComfyUI JSON' };
    for (const [key, entry] of graphNodes) { entry.detail.textContent = graphDetails[key] || ''; entry.detail.title = entry.detail.textContent; entry.button.setAttribute('aria-pressed', String(key === selectedNode)); }
    updatePreview(); requestAnimationFrame(drawWires);
  }
  const scheduleRefresh = () => { if (queuedRefresh || closed) return; queuedRefresh = true; queueMicrotask(() => { queuedRefresh = false; refresh(); }); };
  const revealInvalid = event => {
    // A validity sweep emits an event for every invalid field. Always reveal
    // the first invalid control in the owning form, which is where the browser
    // will move focus, instead of ending on the last invalid field's stage.
    const first = [...(event.target.form?.elements || [])].find(item => item.willValidate && !item.validity.valid) || event.target;
    const stage = workflowStudioStageForControl(first, panels);
    if (stage) select(stage);
    for (let ancestor = first.parentElement; ancestor && root.contains(ancestor); ancestor = ancestor.parentElement)
      if (ancestor.tagName === 'DETAILS') ancestor.open = true;
  };
  root.addEventListener('input', scheduleRefresh); root.addEventListener('change', scheduleRefresh); root.addEventListener('invalid', revealInvalid, true);
  const resize = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => requestAnimationFrame(() => { if (!zoomTouched) fitGraph(); drawWires(); })) : null; resize?.observe(canvas);
  buildGraph(); setView(view); select(selected); refresh();
  return {
    root, select, refresh,
    destroy() {
      if (closed) return; closed = true; resize?.disconnect(); conversion?.destroy();
      root.removeEventListener('input', scheduleRefresh); root.removeEventListener('change', scheduleRefresh); root.removeEventListener('invalid', revealInvalid, true);
      releaseVideoJobMedia(previewBody); if (previewUrl) URL.revokeObjectURL(previewUrl);
    },
  };
}
