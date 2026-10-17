import { bindWorkflowPromptHistory } from './workflowPromptHistory.js';
import { blankWorkflowGraph, createWorkflowGraphStore, graphFields, graphPorts, graphOrder, graphId, workflowRequestId, graphTypes, connectionIssue } from './workflowGraphStore.js';
import { showVideoServerFiles } from './videoServerFiles.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { graphFieldMultiline, graphFieldUsesPromptHistory, graphOutputKind, graphPortHelp } from './workflowGraphPresentation.js';

const API = '/api/workflow-graphs', DRAFT = 'odysseus-workflow-graph-draft-v1';
let retainedDraft = null;
const copyBindings = value => Object.fromEntries(Object.entries(value || {}).map(([key,items]) => [key,[...items]]));
const make = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const button = (text, run, cls = '') => { const b = make('button', `wg-button ${cls}`, text); b.type = 'button'; b.onclick = run; return b; };
const friendly = value => String(value || '').replaceAll('_',' ').replace(/\b\w/g,l => l.toUpperCase());
const nameOf = (node, types) => node?.title || types.get(node?.type)?.label || types.get(node?.type)?.title || node?.type || 'Operation';
const clone = value => JSON.parse(JSON.stringify(value));
const svgEl = (tag, attrs = {}) => { const el = document.createElementNS('http://www.w3.org/2000/svg',tag); for (const [k,v] of Object.entries(attrs)) el.setAttribute(k,String(v)); return el; };
async function request(path, options = {}) {
  const response = await fetch(API + path,{credentials:'same-origin',...options}); let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) { const detail = data.detail || data.error; const error = new Error(typeof detail === 'string' ? detail : detail?.message || `Request failed (${response.status})`); error.details = detail; throw error; } return data;
}
const post = (path,value,signal) => request(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(value),signal});
function stylesheet(id,href) { if (document.getElementById(id)) return; const s = make('link'); s.id = id; s.rel = 'stylesheet'; s.href = href; document.head.append(s); }

// This workspace owns a graph, not a mirror of the old preset forms. Every
// connection in either view mutates the same edges submitted to the compiler.
export function showWorkflowGraphEditor(initial = {}, anchor = document.activeElement) {
  if (document.querySelector('.workflow-graph-overlay')) { document.querySelector('.workflow-graph-overlay input')?.focus(); return; }
  stylesheet('workflow-graph-styles','/static/workflow-graph.css'); stylesheet('h3-video-styles','/static/h3-video.css');
  const overlay = make('div','workflow-graph-overlay'); overlay.style.zIndex = String(topPortalZ());
  const root = make('section','workflow-graph-editor'); root.setAttribute('role','dialog'); root.setAttribute('aria-modal','true'); root.setAttribute('aria-label','Workflow builder');
  overlay.append(root); document.body.append(overlay);
  const promptHistory = bindWorkflowPromptHistory(root, { workflow: 'workflow_graph' });
  const header = make('header','wg-header'), identity = make('div','wg-identity');
  const title = make('input','wg-title'); title.setAttribute('aria-label','Workflow title'); title.maxLength = 180;
  const saved = make('span','wg-muted','Loading operations…'); identity.append(make('span','wg-kicker','ODYSSEUS / WORKFLOWS'),title,saved);
  const headerActions = make('div','wg-toolbar'), newBtn = button('New workflow',() => openLibrary(true)), libraryBtn = button('Library',() => openLibrary(false));
  const saveBtn = button('Save',save), importBtn = button('Import JSON',() => importPicker.click()), exportBtn = button('Export JSON',exportDocument), closeBtn = button('Close',close);
  headerActions.append(newBtn,libraryBtn,saveBtn,importBtn,exportBtn,closeBtn); header.append(identity,headerActions);
  const toolbar = make('div','wg-main-toolbar'), tabs = make('div','wg-tabs'), pipelineTab = button('Pipeline',() => setView('pipeline')), nodesTab = button('Nodes',() => setView('nodes'));
  pipelineTab.dataset.graphView = 'pipeline'; nodesTab.dataset.graphView = 'nodes'; tabs.setAttribute('aria-label','Workflow view'); tabs.append(pipelineTab,nodesTab);
  const addBtn = button('+ Add operation',() => openCatalog()), undoBtn = button('Undo',() => store?.undo()), redoBtn = button('Redo',() => store?.redo()), validateBtn = button('Check workflow',() => validate(true)), runBtn = button('Queue workflow',queue,'wg-primary');
  const packagesBtn = button('Node packages',openPackages);
  toolbar.append(tabs,addBtn,packagesBtn,undoBtn,redoBtn,validateBtn,runBtn);
  const notice = make('p','wg-notice'); notice.setAttribute('role','status'); notice.setAttribute('aria-live','polite'); notice.hidden = true;
  const setupNotes = make('details','wg-setup-notes'); setupNotes.hidden = true;
  const workspace = make('div','wg-workspace'), main = make('div','wg-main'), inspector = make('aside','wg-inspector'); inspector.setAttribute('aria-label','Operation settings');
  const pipeline = make('div','wg-pipeline'); pipeline.setAttribute('aria-label','Workflow operations');
  const canvas = make('div','wg-canvas'), canvasTools = make('div','wg-canvas-tools');
  const zoomLabel = make('span','wg-muted','100%'); const zoomOut = button('−',() => setZoom(zoom-.1)), zoomIn = button('+',() => setZoom(zoom+.1));
  zoomOut.setAttribute('aria-label','Zoom out'); zoomIn.setAttribute('aria-label','Zoom in');
  const foldBtn = button('Collapse setup hubs',() => { collapsed = !collapsed; foldBtn.textContent = collapsed ? 'Expand setup hubs' : 'Collapse setup hubs'; renderCanvas(); });
  canvasTools.append(foldBtn,button('Arrange',() => store.layout()),zoomOut,zoomLabel,zoomIn,button('Fit',fit));
  const wireHint = make('span','wg-wire-hint','Drag the background to pan. Drag a block to move it. Connect an output to an input.');
  canvasTools.append(wireHint);
  const viewport = make('div','wg-viewport'); viewport.tabIndex = 0; viewport.setAttribute('aria-label','Editable workflow canvas');
  const space = make('div','wg-space'), scene = make('div','wg-scene'); space.append(scene); viewport.append(space); canvas.append(canvasTools,viewport); main.append(pipeline,canvas); workspace.append(main,inspector);
  const footer = make('section','wg-footer'), validation = make('details','wg-validation'), validationTitle = make('summary','','Execution plan'); validation.append(validationTitle);
  const validationBody = make('div','wg-validation-body'); validation.append(validationBody);
  const runs = make('details','wg-runs'), runsTitle = make('summary','','Recent workflow runs'), runsBody = make('div','wg-runs-body'); runs.append(runsTitle,runsBody); runs.ontoggle = () => { if (runs.open) refreshRuns(); };
  footer.append(validation,runs); root.append(header,toolbar,notice,setupNotes,workspace,footer);
  const importPicker = make('input'); importPicker.type = 'file'; importPicker.accept = '.json,application/json'; importPicker.hidden = true; root.append(importPicker);
  let catalog = {}, inventory = {}, store, selected = null, view = 'pipeline', dirty = false, savedId = null, revision = null, lastSaved = '', bindings = {}, zoom = 1, collapsed = true, wireStart = null, edgeSelected = null, closed = false, validationController = null, generation = 0, validationTimer, runsTimer, libraryPanel = null, validated = null, queueing = false, importing = false, saving = false, loadToken = 0, frame, drag = null, requestId = null, pan = {x:0,y:0}, panGesture = null;
  const mediaUrls = new Set(), runCards = new Map();
  const tell = (text,error = false) => { notice.hidden = !text; notice.textContent = text; notice.classList.toggle('wg-error',error); };
  function renderSetupNotes(notes) {
    const values = Array.isArray(notes) ? notes.filter(note => typeof note === 'string' || typeof note?.message === 'string') : [];
    setupNotes.hidden = !values.length; setupNotes.replaceChildren(make('summary','',`Workflow setup · ${values.length} note${values.length === 1 ? '' : 's'}`));
    const body = make('div','wg-setup-notes-body');
    for (const note of values) { const row = make('p',note.severity === 'error' ? 'wg-error' : 'wg-muted',typeof note === 'string' ? note : note.message); if (note.node_id && store?.document.nodes.some(n => n.id === note.node_id)) row.append(button('Inspect',() => select(note.node_id))); body.append(row); }
    setupNotes.append(body);
  }
  const currentDocument = () => clone(store?.document || blankWorkflowGraph());
  const persist = () => {
    if (!store) return;
    const serializable = Object.fromEntries(Object.entries(bindings).map(([id,items]) => [id,items.filter(f => f.serverFile).map(f => ({name:f.name,serverFile:f.serverFile}))]).filter(([,v]) => v.length));
    try { const serialized = JSON.stringify({document:store.document,savedId,revision,serverBindings:serializable,view,selected}); localStorage.setItem(DRAFT,serialized); retainedDraft = {serialized,bindings:copyBindings(bindings)}; } catch {}
  };
  const updateDirty = () => { dirty = JSON.stringify(store?.document) !== lastSaved; saved.textContent = `${savedId ? `Revision ${revision}` : 'Local draft'}${dirty ? ' · Unsaved changes' : ' · Saved'} · ${store?.document.nodes.length || 0} operations`; saveBtn.disabled = saving || !store; undoBtn.disabled = !store?.canUndo; redoBtn.disabled = !store?.canRedo; };
  function select(id) {
    selected = id; edgeSelected = null; persist(); renderPipeline(); renderCanvas(); renderInspector();
    if (window.matchMedia('(max-width:720px)').matches) {
      validation.open = false; runs.open = false; setupNotes.open = false;
      requestAnimationFrame(() => { workspace.scrollTop = inspector.offsetTop - workspace.offsetTop; });
    }
  }
  function setView(next) { workspace.scrollTop = 0; view = next; root.dataset.view = next; pipeline.hidden = next !== 'pipeline'; canvas.hidden = next !== 'nodes'; pipelineTab.setAttribute('aria-pressed',String(next === 'pipeline')); nodesTab.setAttribute('aria-pressed',String(next === 'nodes')); persist(); if (next === 'nodes') renderCanvas(); }
  function setZoom(next) { zoom = Math.max(.35,Math.min(1.75,next)); scene.style.transform = `translate(${pan.x}px,${pan.y}px) scale(${zoom})`; space.style.width = `${Math.max(viewport.clientWidth,Number(scene.dataset.width || 1000)*zoom+Math.max(0,pan.x))}px`; space.style.height = `${Math.max(viewport.clientHeight,Number(scene.dataset.height || 500)*zoom+Math.max(0,pan.y))}px`; zoomLabel.textContent = `${Math.round(zoom*100)}%`; }
  function fit() { if (!store?.document.nodes.length) return; pan = {x:0,y:0}; setZoom(Math.min(1,(viewport.clientWidth-24)/Number(scene.dataset.width || 1000),(viewport.clientHeight-24)/Number(scene.dataset.height || 500))); viewport.scrollLeft = 0; viewport.scrollTop = 0; }
  viewport.onpointerdown = event => {
    if (event.button !== 0 || event.target.closest('.wg-node,.wg-wire-hit,button,input,select,textarea,a') || panGesture) return;
    event.preventDefault(); viewport.focus({preventScroll:true});
    panGesture = {id:event.pointerId,x:event.clientX,y:event.clientY,origin:{...pan}}; viewport.setPointerCapture(event.pointerId); viewport.classList.add('wg-panning');
  };
  viewport.onpointermove = event => { if (!panGesture || event.pointerId !== panGesture.id) return; event.preventDefault(); pan = {x:panGesture.origin.x+event.clientX-panGesture.x,y:panGesture.origin.y+event.clientY-panGesture.y}; setZoom(zoom); };
  const stopPan = event => { if (!panGesture || event.pointerId !== panGesture.id) return; if (viewport.hasPointerCapture(event.pointerId)) viewport.releasePointerCapture(event.pointerId); panGesture = null; viewport.classList.remove('wg-panning'); };
  viewport.onpointerup = stopPan; viewport.onpointercancel = stopPan; viewport.onlostpointercapture = () => { panGesture = null; viewport.classList.remove('wg-panning'); };
  function handleChange(document,reason) {
    if (selected && !document.nodes.some(n => n.id === selected)) selected = document.nodes[0]?.id || null;
    requestId = null; validated = null; runBtn.disabled = queueing || !document.nodes.length; generation++; validationController?.abort(); clearTimeout(validationTimer);
    title.value = document.title || 'Untitled workflow'; updateDirty(); persist(); renderPipeline(); renderCanvas();
    if (!['parameter','title','move'].includes(reason)) renderInspector();
    validationTitle.textContent = 'Execution plan · Checking changes…'; validationTimer = setTimeout(() => validate(false),450);
  }
  function replace(document,{id = null,rev = null,media = {}} = {}) {
    loadToken++; savedId = id; revision = rev; bindings = media; selected = document.nodes?.[0]?.id || null; wireStart = null; edgeSelected = null;
    lastSaved = id ? JSON.stringify(document) : ''; store.replace(document); closePanel();
    renderSetupNotes(document.metadata?.setup_notes);
  }
  title.onchange = () => store?.title(title.value.trim() || 'Untitled workflow');
  function openPanel(label) {
    closePanel(); const panel = make('section','wg-overlay-panel'); panel.setAttribute('aria-label',label);
    const heading = make('div','wg-panel-heading'); heading.append(make('h2','',label),button('Back to workflow',closePanel)); panel.append(heading); root.append(panel); libraryPanel = panel; return panel;
  }
  function closePanel() { libraryPanel?.remove(); libraryPanel = null; }
  async function refreshCatalog() {
    const value = await request('/catalog'); if (closed) return;
    catalog = value; inventory = catalog.inventory?.h3 || {};
    store.types.clear(); for (const [id,type] of graphTypes(catalog)) store.types.set(id,type);
    handleChange(store.document,'catalog');
  }
  async function openPackages() {
    const panel = openPanel('Node packages');
    panel.append(make('p','wg-muted','Add operations from installed ComfyUI packages using their own declared inputs, outputs and settings. Discovery reads metadata in a private CPU process; missing dependencies are reported here.'));
    const status = make('p','wg-muted','Loading installed packages…'); status.setAttribute('role','status'); panel.append(status);
    const actions = make('div','wg-toolbar'), candidates = make('select','wg-input'); candidates.setAttribute('aria-label','Installed node package');
    const discover = button('Discover package',async() => {
      if (!candidates.value) return; discover.disabled = true; status.textContent = 'Reading package declarations…';
      try { const result = await post('/packages/discover',{id:candidates.value}); if (closed) return; await refreshCatalog(); if (libraryPanel !== panel) return; await load();
        status.textContent = result.status === 'failed' ? 'Discovery failed. Review the package report below.' : `Package declarations updated · ${result.nodes?.length || 0} operations.`;
      } catch(e) { if (libraryPanel === panel) status.textContent = e.message; }
      finally { if (libraryPanel === panel) discover.disabled = !candidates.value; }
    });
    const objectPicker = make('input'); objectPicker.type = 'file'; objectPicker.accept = '.json,application/json'; objectPicker.hidden = true;
    const objectButton = button('Import /object_info JSON',() => objectPicker.click());
    objectPicker.onchange = async() => {
      const file = objectPicker.files?.[0]; objectPicker.value = ''; if (!file) return; objectButton.disabled = true;
      try { if (file.size > 12*1024*1024) throw new Error('Node metadata JSON exceeds 12 MiB.'); const object_info = JSON.parse(await file.text());
        const result = await post('/packages/object-info',{object_info}); if (closed) return; await refreshCatalog(); if (libraryPanel !== panel) return; await load();
        status.textContent = `Imported ${result.nodes?.length || 0} node declarations. Execution needs the matching installed package.`;
      } catch(e) { if (libraryPanel === panel) status.textContent = e.message; }
      finally { objectButton.disabled = false; }
    };
    candidates.onchange = () => { discover.disabled = !candidates.value; }; actions.append(candidates,discover,objectButton,objectPicker); panel.append(actions);
    const list = make('div','wg-package-list'); panel.append(list);
    function packageCard(manifest) {
      const card = make('article','wg-package-card'), heading = make('div','wg-run-heading');
      heading.append(make('strong','',manifest.package?.name || manifest.source?.name || manifest.package_id || 'Imported object info'),make('span','wg-state',manifest.status || 'metadata'));
      card.append(heading,make('p','wg-muted',`${manifest.nodes?.length || 0} declared operations · ${manifest.execution_status === 'unbound' ? 'Needs matching installed source' : 'Metadata discovered; execution validated when queued'}`));
      for (const issue of manifest.issues || []) card.append(make('p','wg-error',`${issue.class_type ? issue.class_type+': ' : ''}${issue.message || issue.code}${issue.missing_module ? ` · Missing dependency: ${issue.missing_module}` : ''}`));
      const exportSchema = button('Export schema',() => {
        const url = URL.createObjectURL(new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'})), link = make('a'); link.href = url; link.download = `${manifest.package_id || 'comfy-node'}-schema.json`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
      }); card.append(exportSchema);
      const schema = make('details','wg-package-schema'); schema.append(make('summary','','Inspect declared inputs, outputs and hooks'));
      schema.ontoggle = () => { if (schema.open && !schema.querySelector('pre')) schema.append(make('pre','',JSON.stringify(manifest.nodes || [],null,2))); }; card.append(schema); return card;
    }
    async function load() {
      const result = await request('/packages'); if (closed || libraryPanel !== panel) return;
      const previous = candidates.value; candidates.replaceChildren(new Option('Choose installed package…',''));
      for (const item of result.candidates || []) candidates.add(new Option(item.name || item.id,item.id));
      if ([...candidates.options].some(o => o.value === previous)) candidates.value = previous;
      discover.disabled = !candidates.value; list.replaceChildren(...(result.packages || []).map(packageCard));
      if (!(result.packages || []).length) list.append(make('p','wg-muted','No node packages discovered yet. Choose an installed package above to read its schema.'));
      status.textContent = `${result.packages?.length || 0} package reports · ${result.candidates?.length || 0} installed candidates`;
    }
    try { await load(); } catch(e) { if (libraryPanel === panel) status.textContent = e.message; }
  }
  async function openLibrary(fresh) {
    const panel = openPanel(fresh ? 'New workflow' : 'Workflow library');
    panel.append(make('p','wg-muted',fresh ? 'Start with a blank graph or an editable template. Every operation and connection can be changed.' : 'Saved on this Odysseus server. Library workflows restore settings and connections; attach media again before queuing.'));
    const cards = make('div','wg-library-grid'); panel.append(cards);
    if (fresh) {
      cards.append(libraryCard('Blank workflow','Build your own connected operations.',() => replace(blankWorkflowGraph())));
      for (const template of catalog.templates || []) cards.append(libraryCard(template.title || template.name || template.id,template.description || 'Editable workflow template',() => { replace(clone(template.document || template.graph)); renderSetupNotes(template.setup_notes || template.document?.metadata?.setup_notes); }));
      return;
    }
    try {
      const response = await request('/documents'); if (libraryPanel !== panel) return;
      if (!(response.documents || []).length) cards.append(make('p','wg-muted','No saved workflows yet. Use Save to add your draft.'));
      for (const item of response.documents || []) cards.append(libraryCard(item.title,`Revision ${item.revision}`,async() => {
        const token = ++loadToken; try { const doc = await request(`/documents/${encodeURIComponent(item.id)}`); if (!closed && token === loadToken && libraryPanel === panel) replace(doc.document,{id:doc.id,rev:doc.revision}); } catch(e) { tell(e.message,true); }
      }));
    } catch(e) { if (libraryPanel === panel) cards.append(make('p','wg-error',e.message)); }
  }
  function libraryCard(title,detail,action) { const card = button('',action,'wg-library-card'); card.append(make('strong','',title),make('span','wg-muted',detail)); return card; }
  function openCatalog() {
    const panel = openPanel('Add operation'), search = make('input','wg-input'); search.type = 'search'; search.placeholder = 'Find an operation…'; search.setAttribute('aria-label','Search operations');
    const list = make('div','wg-operation-list'); panel.append(search,list);
    const render = () => { list.replaceChildren(); for (const [type,spec] of store.types) {
      if (`${spec.label || spec.title || ''} ${type} ${spec.description || ''}`.toLowerCase().includes(search.value.toLowerCase())) {
        const item = libraryCard(spec.label || spec.title || type,spec.description || type,() => { selected = store.add(type); closePanel(); renderInspector(); renderPipeline(); renderCanvas(); }); item.dataset.operationType = type; list.append(item);
      }
    } }; search.oninput = render; render(); search.focus();
  }
  function renderPipeline() {
    if (!store) return; pipeline.replaceChildren();
    const heading = make('div','wg-section-heading'); heading.append(make('h2','','Your workflow'),make('p','wg-muted','Select an operation to change its settings and connections.')); pipeline.append(heading);
    if (!store.document.nodes.length) { const empty = make('div','wg-empty'); empty.append(make('h3','','Start with an operation'),make('p','wg-muted','Connect models, inputs, generation and outputs into your own workflow.'),button('Add first operation',openCatalog,'wg-primary')); pipeline.append(empty); return; }
    const order = graphOrder(store.document);
    for (const [index,n] of order.entries()) {
      const spec = store.types.get(n.type), card = button('',() => select(n.id),'wg-stage'); card.dataset.graphNode = n.id; card.setAttribute('aria-pressed',String(n.id === selected));
      const copy = make('span','wg-stage-copy'), sources = store.document.edges.filter(e => e.target.node === n.id).map(e => nameOf(store.document.nodes.find(x => x.id === e.source.node),store.types));
      copy.append(make('strong','',nameOf(n,store.types)),make('small','wg-muted',sources.length ? `From ${[...new Set(sources)].join(' + ')}` : spec?.description || 'No connected inputs'));
      if (n.state && n.state !== 'active') copy.append(make('span','wg-state',n.state));
      if (!spec) copy.append(make('span','wg-error','Unsupported operation · preserved'));
      if (n.type === 'input.prompt' && n.params?.text) copy.append(make('small','wg-stage-preview',n.params.text));
      if (n.type === 'model.lora') copy.append(make('small','wg-stage-preview',`${componentName(n.params?.component,n.params?.name)} · Strength ${n.params?.strength ?? 1}`));
      card.append(make('span','wg-stage-index',String(index+1).padStart(2,'0')),copy,make('span','wg-stage-arrow','→')); pipeline.append(card);
    }
    pipeline.append(button('+ Add operation',openCatalog,'wg-add-end'));
  }
  function portList(n,direction) {
    const defined = graphPorts(store.types.get(n.type),direction); if (defined.length) return defined;
    const imported = n[direction] || n.ports?.[direction]; if (imported) return graphPorts({[direction]:imported},direction);
    // Draw preserved unknown-node links, but do not invent a runnable schema.
    const key = direction === 'inputs' ? 'target' : 'source';
    return [...new Set(store.document.edges.filter(e => e[key].node === n.id).map(e => e[key].port))].map(id => ({id,label:id,type:'UNKNOWN'}));
  }
  function foldedNodes() {
    const hidden = new Set(); if (!collapsed) return hidden;
    for (const hub of store.document.nodes.filter(n => n.type === 'hub.setup')) {
      let changed = true; while (changed) { changed = false; for (const n of store.document.nodes) {
        if (hidden.has(n.id) || !(n.type.startsWith('component.') || n.type === 'model.lora')) continue;
        const outgoing = store.document.edges.filter(e => e.source.node === n.id);
        if (outgoing.length && outgoing.every(e => e.target.node === hub.id || hidden.has(e.target.node))) { hidden.add(n.id); changed = true; }
      } }
    } return hidden;
  }
  function renderCanvas() {
    if (!store) return; scene.replaceChildren(); const hidden = foldedNodes(), nodes = store.document.nodes.filter(n => !hidden.has(n.id));
    foldBtn.textContent = collapsed ? 'Expand setup hubs' : 'Collapse setup hubs';
    const box = new Map(nodes.map((n,i) => [n.id,{x:Math.max(16,Number(n.position?.x) || 48+i%3*288),y:Math.max(16,Number(n.position?.y) || 64+Math.floor(i/3)*256),h:Math.max(112,73+Math.max(portList(n,'inputs').length,portList(n,'outputs').length)*23)}]));
    const width = Math.max(680,...[...box.values()].map(b => b.x+260)), height = Math.max(480,...[...box.values()].map(b => b.y+b.h+48)); scene.dataset.width = width; scene.dataset.height = height; scene.style.width = `${width}px`; scene.style.height = `${height}px`;
    const wires = svgEl('svg',{width,height,'class':'wg-wires','aria-label':'Workflow connections'}); scene.append(wires);
    for (const e of store.document.edges) {
      if (hidden.has(e.source.node) || hidden.has(e.target.node)) continue;
      const from = nodes.find(n => n.id === e.source.node), to = nodes.find(n => n.id === e.target.node); if (!from || !to) continue;
      const a = box.get(from.id), b = box.get(to.id), out = portList(from,'outputs'), input = portList(to,'inputs'), oi = out.findIndex(p => p.id === e.source.port), ti = input.findIndex(p => p.id === e.target.port);
      const x1 = a.x+220,y1 = a.y+73+Math.max(0,oi)*23,x2 = b.x,y2 = b.y+73+Math.max(0,ti)*23;
      // Elbow routing: backward links travel around the top of both blocks.
      const mx = x2 >= x1+40 ? Math.round((x1+x2)/32)*16 : x1+24, top = Math.min(a.y,b.y)-20;
      const path = x2 >= x1+40 ? `M${x1},${y1} H${mx} V${y2} H${x2}` : `M${x1},${y1} H${mx} V${top} H${x2-24} V${y2} H${x2}`;
      const line = svgEl('path',{d:path,'class':`wg-wire${e.id === edgeSelected ? ' selected' : ''}${out[oi]?.type === 'SETUP' ? ' bundle' : ''}`,'data-edge-id':e.id});
      const hit = svgEl('path',{d:path,'class':'wg-wire-hit',tabindex:0,role:'button','aria-label':`Connection ${nameOf(from,store.types)} to ${nameOf(to,store.types)}. Select to reconnect or remove.`});
      const choose = () => { edgeSelected = e.id; wireStart = null; renderCanvas(); renderEdgeInspector(e); }; hit.onclick = choose; hit.onkeydown = event => { if (['Enter',' '].includes(event.key)) { event.preventDefault(); choose(); } };
      wires.append(line,hit);
    }
    for (const n of nodes) {
      const b = box.get(n.id), card = make('article',`wg-node${n.id === selected && !edgeSelected ? ' selected' : ''}${!store.types.has(n.type) ? ' unsupported' : ''}`); card.dataset.graphNode = n.id; card.style.left = `${b.x}px`; card.style.top = `${b.y}px`; card.style.height = `${b.h}px`; card.style.width = '220px';
      card.onpointerdown = event => { if (!event.target.closest('button,input,select,textarea,a')) startDrag(event,n,card); };
      const head = button(nameOf(n,store.types),() => select(n.id),'wg-node-head'); head.title = 'Select to inspect. Drag to move.'; head.onpointerdown = event => startDrag(event,n,card); card.append(head);
      const category = store.types.get(n.type)?.category || n.type;
      card.append(make('small','wg-node-type',n.state && n.state !== 'active' ? `${category} · ${n.state}` : n.type === 'hub.setup' ? 'Model · applied LoRAs · encoder · VAEs' : category));
      if (n.type === 'hub.setup' && collapsed) card.classList.add('wg-hub');
      for (const direction of ['inputs','outputs']) {
        const list = make('div',`wg-ports ${direction}`);
        for (const p of portList(n,direction)) {
          const port = button('',event => choosePort(event,n,p,direction),'wg-port'); port.dataset.portNode = n.id; port.dataset.port = p.id; port.dataset.direction = direction; port.title = `${p.label || p.id} · ${p.type}${p.required ? ' · required' : ''}`;
          port.setAttribute('aria-label',`${nameOf(n,store.types)} ${direction === 'inputs' ? 'input' : 'output'} ${p.label || p.id} (${p.type})`);
          if (direction === 'outputs') port.onpointerdown = event => startWireDrag(event,n,p);
          port.append(make('i','wg-port-dot'),make('span','',p.label || p.id));
          if (wireStart?.node === n.id && wireStart.port === p.id && direction === 'outputs') port.classList.add('selected');
          if (wireStart && direction === 'inputs' && !connectionIssue(store.document,store.types,wireStart,{node:n.id,port:p.id})) port.classList.add('compatible');
          list.append(port);
        } card.append(list);
      }
      scene.append(card);
    }
    setZoom(zoom);
    if (!nodes.length) { const empty = make('div','wg-canvas-empty'); empty.append(make('h3','','A blank canvas, ready to connect'),button('Add an operation',openCatalog,'wg-primary')); scene.append(empty); }
  }
  function startDrag(event,n,card) {
    if (event.button !== 0) return; event.stopPropagation(); const x = event.clientX,y = event.clientY, old = {x:parseFloat(card.style.left),y:parseFloat(card.style.top)}; let moved = false;
    const move = e => { if (Math.abs(e.clientX-x)+Math.abs(e.clientY-y)<5 && !moved) return; moved = true; card.style.left = `${Math.max(16,Math.round((old.x+(e.clientX-x)/zoom)/16)*16)}px`; card.style.top = `${Math.max(16,Math.round((old.y+(e.clientY-y)/zoom)/16)*16)}px`; };
    const up = () => { window.removeEventListener('pointermove',move); window.removeEventListener('pointerup',up); window.removeEventListener('pointercancel',up); if (moved) { const pos = {x:parseFloat(card.style.left),y:parseFloat(card.style.top)}; store.patch(n.id,{position:pos},'move'); } drag = null; };
    drag = {move,up}; window.addEventListener('pointermove',move); window.addEventListener('pointerup',up,{once:true}); window.addEventListener('pointercancel',up,{once:true});
  }
  function choosePort(event,n,p,direction) {
    event.stopPropagation();
    if (direction === 'outputs') { wireStart = {node:n.id,port:p.id}; edgeSelected = null; tell(`Connect ${nameOf(n,store.types)} · ${p.label || p.id}: choose a compatible input.`); renderCanvas(); return; }
    if (!wireStart) { select(n.id); tell('Choose an output first, or select an upstream operation in Connections.'); inspector.querySelector('select[data-input-port]')?.focus(); return; }
    try { const source = wireStart; store.connect(source,{node:n.id,port:p.id}); wireStart = null; selected = n.id; tell('Connection updated. Execution now follows this link.'); renderCanvas(); renderInspector(); } catch(e) { tell(e.message,true); }
  }
  function startWireDrag(event,n,p) {
    if (event.button !== 0) return;
    const startX = event.clientX,startY = event.clientY; let moved = false, ghost;
    const point = e => { const rect = scene.getBoundingClientRect(); return {x:(e.clientX-rect.left)/zoom,y:(e.clientY-rect.top)/zoom}; };
    const origin = point(event);
    const move = e => {
      if (!moved && Math.abs(e.clientX-startX)+Math.abs(e.clientY-startY)<5) return;
      if (!moved) { moved = true; ghost = svgEl('path',{'class':'wg-wire selected'}); scene.querySelector('svg')?.append(ghost); }
      const end = point(e), middle = Math.round((origin.x+end.x)/32)*16;
      ghost.setAttribute('d',`M${origin.x},${origin.y} H${middle} V${end.y} H${end.x}`);
    };
    const up = e => {
      window.removeEventListener('pointermove',move); window.removeEventListener('pointerup',up); ghost?.remove();
      if (!moved || closed) return;
      const input = document.elementFromPoint(e.clientX,e.clientY)?.closest('[data-direction="inputs"][data-port-node]');
      if (!input) { tell('Drop onto an input port, or click an output then an input.'); return; }
      try { store.connect({node:n.id,port:p.id},{node:input.dataset.portNode,port:input.dataset.port}); wireStart = null; tell('Connection updated.'); renderCanvas(); }
      catch(error) { tell(error.message,true); }
    };
    window.addEventListener('pointermove',move); window.addEventListener('pointerup',up,{once:true});
  }
  function componentName(id,fallback) { return (inventory.components || []).find(c => String(c.id) === String(id))?.name || fallback || id || 'Choose component'; }
  function renderInspector() {
    inspector.replaceChildren(); const n = store?.document.nodes.find(n => n.id === selected);
    if (!n) { inspector.append(make('h2','','Operation settings'),make('p','wg-muted','Select an operation in either view to edit its parameters and connections.')); return; }
    const spec = store.types.get(n.type); inspector.append(button(`← Back to ${view === 'nodes' ? 'canvas' : 'operations'}`,() => { workspace.scrollTop = 0; },'wg-back-to-map')); inspector.append(make('span','wg-kicker',n.type),make('h2','',nameOf(n,store.types)));
    if (spec?.description) inspector.append(make('p','wg-muted',spec.description));
    const controls = make('div','wg-inspector-actions'); controls.append(button('Duplicate',() => {
      const id = store.duplicate(n.id); if (!id) return;
      for (const key of Object.keys(bindings)) if (key === n.id || key.startsWith(`${n.id}::`)) bindings[id+key.slice(n.id.length)] = [...bindings[key]];
      selected = id; persist(); renderInspector(); renderPipeline(); renderCanvas();
    }),button('Delete operation',() => { store.remove(n.id); tell('Operation deleted. Undo restores its connections.'); })); inspector.append(controls);
    const label = make('input','wg-input'); label.value = n.title || ''; label.placeholder = spec?.label || n.type; label.onchange = () => store.patch(n.id,{title:label.value}); inspector.append(fieldLabel('Operation name',label));
    const state = make('select','wg-input'); for (const [value,label] of [['active','Enabled'],['disabled','Disabled'],['bypassed','Bypassed']]) { if (value === 'bypassed' && !spec?.bypass && n.state !== 'bypassed') continue; state.add(new Option(label,value)); }
    state.value = n.state || 'active'; state.onchange = () => store.patch(n.id,{state:state.value}); inspector.append(fieldLabel('Operation state',state));
    if (!spec) {
      inspector.append(make('p','wg-error','This imported operation is preserved, including its settings and connections. It needs an execution adapter before an enabled output can use it.'));
      const raw = make('textarea','wg-input'); raw.rows = 10; raw.value = JSON.stringify(n.params || {},null,2); raw.onchange = () => { try { const params = JSON.parse(raw.value); if (!params || Array.isArray(params) || typeof params !== 'object') throw new Error('Use a JSON object.'); store.patch(n.id,{params}); } catch(e) { tell(e.message,true); } }; inspector.append(fieldLabel('Preserved parameters',raw));
      renderConnections(n); return;
    }
    if (n.type.startsWith('input.') && ['image','video','audio','refmod'].includes(n.type.slice(6))) inspector.append(renderMediaInput(n));
    if (spec.execution !== 'comfy' && portList(n,'inputs').some(p => ['IMAGE','VIDEO','AUDIO','REFMOD'].includes(p.type))) inspector.append(renderReferenceAttachments(n));
    for (const f of graphFields(spec)) {
      if (['asset','name'].includes(f.id) && ['input.image','input.video','input.audio','input.refmod'].includes(n.type)) continue;
      inspector.append(parameterControl(n,f));
    }
    if (n.type === 'hub.setup') {
      const explanation = make('p','wg-muted','One setup connection carries the model after its LoRAs, encoder and VAEs. The bundle retains each component’s identity.'); inspector.append(explanation);
      for (const edge of store.document.edges.filter(e => e.target.node === n.id)) { const source = store.document.nodes.find(x => x.id === edge.source.node); if (source) inspector.append(button(`${friendly(edge.target.port)}: ${nameOf(source,store.types)}`,() => select(source.id),'wg-upstream-link')); }
    }
    renderConnections(n);
    if (spec.execution === 'comfy') {
      const schema = make('details','wg-package-schema'); schema.append(make('summary','','Package input rules and schema'));
      schema.append(make('p','wg-muted',`Execution: ${spec.execution_status || 'Requires validation'}. ${spec.input_is_list ? 'This operation receives complete input lists.' : 'The execution core maps over input lists as declared by the package.'}`));
      const hidden = Object.keys(spec.raw_schema?.input?.hidden || {});
      if (hidden.length) schema.append(make('p','wg-muted',`Provided by the execution core: ${hidden.join(', ')}.`));
      schema.ontoggle = () => { if (schema.open && !schema.querySelector('pre')) schema.append(make('pre','',JSON.stringify({schema:spec.raw_schema || {},hooks:spec.hooks || {},function:spec.function},null,2))); };
      inspector.append(schema);
    }
    if (n.type === 'hub.setup') inspector.append(button(collapsed ? 'Expand setup in Nodes' : 'Show setup in Nodes',() => { collapsed = false; setView('nodes'); renderCanvas(); }));
  }
  function fieldLabel(text,input,help) { const label = make('label','wg-field'); label.append(make('span','',text),input); if (help) label.append(make('small','wg-muted',help)); return label; }
  function parameterControl(n,f) {
    const value = n.params?.[f.id] ?? f.default, label = f.label || friendly(f.id); let input, transform = v => v;
    const options = f.choices || f.options || f.enum;
    if (f.editor === 'media') { const wrap = make('div','wg-field'); wrap.append(make('span','',label),renderMediaInput(n,{kind:f.media_kind || 'image',parameter:f.id})); return wrap; }
    if (f.editor === 'component' || f.id === 'component') {
      input = make('select','wg-input'); input.add(new Option('Choose installed component',''));
      const role = f.component_kind || (n.type === 'model.lora' ? 'lora' : n.type.split('.').at(-1));
      const components = f.inventory_source === 'bfs' ? catalog.inventory?.bfs?.components || [] : (inventory.components || []).filter(c => c.role === role || c.kind === role);
      for (const component of components) input.add(new Option(component.name || component.id,component.id));
      if (value && ![...input.options].some(o => o.value === String(value))) input.add(new Option(`Unavailable: ${n.params?.name || value}`,value));
      input.value = value ?? '';
    } else if (f.id === 'workflow_id' && n.type.startsWith('bfs.')) {
      input = make('select','wg-input'); input.add(new Option('Choose BFS workflow',''));
      for (const workflow of catalog.inventory?.bfs?.workflows || []) input.add(new Option(workflow.label || workflow.name || workflow.id,workflow.id));
      if (value && ![...input.options].some(o => o.value === String(value))) input.add(new Option(`Unavailable: ${value}`,value));
      input.value = value ?? '';
    } else if (f.id === 'gpu' || f.id === 'vae_gpu' || f.editor === 'gpu') {
      input = make('select','wg-input');
      if (f.id === '__odysseus_device') input.add(new Option('CPU','cpu'));
      else input.add(new Option(f.id === 'gpu' ? 'Server default' : 'Same as transformer',''));
      for (const gpu of inventory.gpus || []) input.add(new Option(gpu.name || `GPU ${gpu.id}`,String(gpu.id)));
      if (value !== undefined && value !== null && ![...input.options].some(o => o.value === String(value))) input.add(new Option(String(value),String(value)));
      input.value = value ?? '';
    } else if (options) {
      input = make('select','wg-input');
      const optionValues = new Map();
      for (const item of options) { const val = item !== null && typeof item === 'object' ? item.value ?? item.id : item; optionValues.set(String(val),val); input.add(new Option(item !== null && typeof item === 'object' ? item.label || item.name || String(val) : String(item),String(val))); }
      if (value != null && !optionValues.has(String(value))) { optionValues.set(String(value),value); input.add(new Option(`Unavailable: ${value}`,String(value))); }
      input.value = value == null ? '' : String(value); transform = v => optionValues.has(v) ? optionValues.get(v) : v;
    } else if (['boolean','bool'].includes(f.type)) { input = make('input'); input.type = 'checkbox'; input.checked = !!value; }
    else if (['integer','number','float'].includes(f.type)) { input = make('input','wg-input'); input.type = 'number'; input.step = f.step ?? (f.type === 'integer' ? '1' : 'any'); if (f.min != null) input.min = f.min; if (f.max != null) input.max = f.max; input.value = value ?? ''; transform = v => v === '' && (f.nullable || f.default === null) ? null : Number(v); }
    else if (f.type === 'object' || f.type === 'array' || typeof value === 'object' && value !== null) { input = make('textarea','wg-input'); input.rows = 5; input.value = JSON.stringify(value ?? {},null,2); transform = JSON.parse; }
    else if (graphFieldMultiline(f,store.types.get(n.type))) { input = make('textarea','wg-input'); input.rows = 7; input.value = value ?? ''; }
    else { input = make('input','wg-input'); input.type = 'text'; input.value = value ?? ''; }
    if ((input.tagName === 'TEXTAREA' || input.type === 'text') && graphFieldUsesPromptHistory(store.document,n,f)) input.dataset.workflowPrompt = '';
    input.dataset.graphParam = f.id; input.setAttribute('aria-label',label);
    const update = () => { try { if (!input.checkValidity()) { input.reportValidity(); return; } const next = input.type === 'checkbox' ? input.checked : transform(input.value); if (typeof next === 'number' && !Number.isFinite(next)) throw new Error('Enter a finite number.'); store.parameter(n.id,f.id,next); } catch(e) { tell(`${label}: ${e.message}`,true); } };
    input.onchange = update;
    const optional = Object.hasOwn(store.types.get(n.type)?.raw_schema?.input?.optional || {},f.id);
    return fieldLabel(`${label}${optional ? ' · optional' : ''}`,input,f.description || f.help || f.metadata?.tooltip);
  }
  function renderMediaInput(n,options = {}) {
    const kind = options.kind || n.type.slice(6), box = make('div','wg-media-input'), list = make('div','wg-media-list'), picker = make('input'); picker.type = 'file'; picker.accept = kind === 'refmod' ? '.safetensors' : `${kind}/*`; picker.hidden = true;
    const bindingKey = options.parameter && store.types.get(n.type)?.execution === 'comfy' ? `${n.id}::${options.parameter}` : n.id;
    const apply = items => { requestId = null; bindings[bindingKey] = items; store.parameter(n.id,options.parameter || 'name',items.map(f => f.name).join(', ')); persist(); renderInspector(); };
    picker.onchange = () => { if (picker.files?.length) apply([...picker.files]); };
    const missing = n.params?.[options.parameter || 'name'] && !bindings[bindingKey]?.length;
    box.append(button(missing ? 'Attach again' : `Choose ${kind}`,() => picker.click()),picker);
    bindMediaDrop(box,kind,items => apply(items.slice(0,1)));
    if (kind === 'video') box.append(button('Server files · E: / F:',event => showVideoServerFiles({family:'h3',multiple:false,onSelect:apply},event.currentTarget)));
    const items = bindings[bindingKey] || [];
    if (!items.length) list.append(make('p','wg-muted',n.params?.[options.parameter || 'name'] ? `${n.params[options.parameter || 'name']} · Select the file to attach it to this draft.` : `No ${kind} attached.`));
    for (const item of items) {
      const row = make('div','wg-media-row'); row.append(make('span','',item.name),button('Remove',() => apply(items.filter(f => f !== item))));
      if (kind !== 'refmod') row.append(button('Preview',() => {
        let url; if (item.serverFile) { const params = new URLSearchParams(item.serverFile); url = `/api/video/h3/server-files/video?${params}`; }
        else { url = URL.createObjectURL(item); mediaUrls.add(url); }
        const media = make(kind === 'image' ? 'img' : kind,'wg-media-preview'); media.src = url; if (kind !== 'image') { media.controls = true; media.preload = 'metadata'; } media.alt = item.name; row.append(media);
      })); list.append(row);
    }
    box.append(list); return box;
  }
  function bindMediaDrop(box,kind,apply) {
    const extensions = {video:/\.(mp4|m4v|mov|webm|mkv|avi|mpeg|mpg|mts|m2ts)$/i,image:/\.(png|jpe?g|webp|gif|avif|heic|heif|bmp|tiff?)$/i,audio:/\.(wav|mp3|m4a|aac|ogg|flac|opus|aiff?)$/i,refmod:/\.safetensors$/i};
    box.ondragover = event => { if ([...event.dataTransfer?.types || []].includes('Files')) { event.preventDefault(); event.stopPropagation(); event.dataTransfer.dropEffect = 'copy'; box.classList.add('wg-drop-active'); } };
    box.ondragleave = event => { if (!box.contains(event.relatedTarget)) box.classList.remove('wg-drop-active'); };
    box.ondrop = event => {
      event.preventDefault(); event.stopPropagation(); box.classList.remove('wg-drop-active');
      const files = [...event.dataTransfer?.files || []], accepted = files.filter(file => file.type.startsWith(`${kind}/`) || extensions[kind]?.test(file.name));
      if (accepted.length) apply(accepted);
      if (accepted.length !== files.length) tell(`Only ${kind === 'refmod' ? 'safetensors' : kind} files can be attached here.`,true);
    };
  }
  function renderReferenceAttachments(n) {
    const bundle = n.type === 'input.references';
    const group = make('section','wg-reference-attachments'); group.append(make('h3','wg-subheading',bundle ? 'Attach references' : 'Attach media'),make('p','wg-muted',bundle ? 'Multiple references stay together in this job. Add files here or connect existing inputs below.' : 'Choose files for these inputs, or connect existing media operations below.'));
    const labels = {reference_videos:'Reference videos',reference_images:'Reference images',reference_audio:'Reference audio',source_video:'Source video',first_frame:'First frame',last_frame:'Last frame',reference_mods:'Reference mods',identity_image:'Face / head image',mask_video:'Mask video'};
    const help = {reference_videos:'Video references for the generated scene. Add several files to use them together.',reference_images:'Image references for the generated scene.',reference_audio:'Audio references used together in this job.',source_video:'One aligned source video to edit or follow. This is separate from numbered references.',first_frame:'Use this image as the first frame.',last_frame:'Use this image as the last frame.',reference_mods:'Attach precomputed reference modifier files.'};
    const available = portList(n,'inputs').filter(p => ['IMAGE','VIDEO','AUDIO','REFMOD'].includes(p.type));
    const keys = [...Object.keys(labels).filter(key => available.some(p => p.id === key)),...available.map(p => p.id).filter(key => !Object.hasOwn(labels,key))];
    for (const key of keys) {
      const port = portList(n,'inputs').find(p => p.id === key); if (!port) continue;
      const kind = {VIDEO:'video',IMAGE:'image',AUDIO:'audio',REFMOD:'refmod'}[port.type]; if (!kind) continue;
      const label = labels[key] || port.label || friendly(key), edges = store.document.edges.filter(e => e.target.node === n.id && e.target.port === key);
      const section = make('details','wg-reference-slot'); section.dataset.referencePort = key; section.open = key === (bundle ? 'reference_videos' : keys[0]) || !!edges.length;
      section.append(make('summary','',`${label}${edges.length ? ` · ${edges.length}` : ''}`));
      const box = make('div','wg-media-input'); box.append(make('p','wg-muted',help[key] || `Choose ${port.multiple ? kind+' files' : 'one '+kind+' file'} for ${label.toLowerCase()}.`));
      const picker = make('input'); picker.type = 'file'; picker.hidden = true; picker.multiple = !!port.multiple; picker.accept = kind === 'refmod' ? '.safetensors' : `${kind}/*`; picker.setAttribute('aria-label',`Attach ${label.toLowerCase()}`);
      const attach = items => {
        if (!items.length) return;
        try {
          const chosen = port.multiple ? items : items.slice(0,1), ids = store.attachMedia({node:n.id,port:key},kind,chosen.map(item => item.name));
          ids.forEach((id,index) => { bindings[id] = [chosen[index]]; }); requestId = null; persist(); renderInspector();
          tell(`${chosen.length} ${kind}${chosen.length === 1 ? '' : 's'} attached to ${label.toLowerCase()} in this job.`);
        } catch(error) { tell(error.message,true); }
      };
      picker.onchange = () => { if (picker.files?.length) attach([...picker.files]); picker.value = ''; };
      const choose = button(port.multiple ? `Add ${kind === 'refmod' ? 'reference mods' : kind+'s'}` : `Choose ${kind}`,() => picker.click()); choose.setAttribute('aria-label',`${port.multiple ? 'Add' : 'Choose'} ${label.toLowerCase()}`); box.append(choose,picker);
      if (kind === 'video') box.append(button('Server files · E: / F:',event => showVideoServerFiles({family:n.type.startsWith('bfs.') ? 'bfs' : 'h3',multiple:!!port.multiple,onSelect:attach},event.currentTarget)));
      box.append(make('small','wg-muted',`Or drop ${kind === 'refmod' ? '.safetensors files' : kind+' files'} here.`)); bindMediaDrop(box,kind,attach);
      for (const edge of edges) {
        const source = store.document.nodes.find(node => node.id === edge.source.node), row = make('div','wg-reference-file');
        row.append(make('strong','',source?.params?.name || nameOf(source,store.types)));
        const attached = bindings[source?.id]?.[0];
        const actions = make('div','wg-toolbar');
        if (source?.type === `input.${kind}` && !attached) {
          row.append(make('small','wg-error','Choose a file for this input before queuing.'));
          const again = make('input'); again.type = 'file'; again.hidden = true; again.accept = picker.accept;
          again.onchange = () => { const file = again.files?.[0]; if (file) { bindings[source.id] = [file]; store.patch(source.id,{title:file.name,params:{...source.params,name:file.name}}); persist(); renderInspector(); } };
          actions.append(button('Attach again',() => again.click()),again);
          if (kind === 'video') actions.append(button('Server files · E: / F:',event => showVideoServerFiles({family:n.type.startsWith('bfs.') ? 'bfs' : 'h3',multiple:false,onSelect:items => { if (items[0]) { bindings[source.id] = [items[0]]; store.patch(source.id,{title:items[0].name,params:{...source.params,name:items[0].name}}); persist(); renderInspector(); } }},event.currentTarget)));
        }
        actions.append(button('Open input',() => select(source.id)),button(bundle ? 'Remove reference' : 'Remove input',() => store.detachMedia(edge.id)));
        if (attached && kind !== 'refmod') actions.append(button('Preview',() => {
          if (row.querySelector('.wg-media-preview')) return;
          let url; if (attached.serverFile) url = `/api/video/h3/server-files/video?${new URLSearchParams(attached.serverFile)}`; else { url = URL.createObjectURL(attached); mediaUrls.add(url); }
          const media = make(kind === 'image' ? 'img' : kind,'wg-media-preview'); media.src = url; if (kind === 'image') media.alt = attached.name; else { media.controls = true; media.preload = 'metadata'; } row.append(media);
        }));
        row.append(actions); box.append(row);
      }
      section.append(box); group.append(section);
    }
    return group;
  }
  function compatibleSources(n,p) {
    const options = [];
    for (const other of store.document.nodes) for (const port of graphPorts(store.types.get(other.type),'outputs')) {
      const source = {node:other.id,port:port.id}; if (!connectionIssue(store.document,store.types,source,{node:n.id,port:p.id})) options.push({source,label:`${nameOf(other,store.types)} · ${port.label || port.id}`});
    } return options;
  }
  function renderConnections(n) {
    const inputs = portList(n,'inputs'); if (!inputs.length) return;
    inspector.append(make('h3','wg-subheading','Connections'));
    for (const p of inputs) {
      const connected = store.document.edges.filter(e => e.target.node === n.id && e.target.port === p.id), options = compatibleSources(n,p);
      const hasWidget = graphFields(store.types.get(n.type)).some(f => f.id === p.id);
      const picker = make('select','wg-input'); picker.dataset.inputPort = p.id; picker.setAttribute('aria-label',`${p.label || p.id} connection`); picker.add(new Option(p.multiple ? 'Add upstream connection…' : p.required ? 'Choose required input…' : hasWidget ? 'Use setting above' : 'Not connected',''));
      for (const option of options) picker.add(new Option(option.label,JSON.stringify(option.source)));
      if (!p.multiple && connected[0]) { const value = JSON.stringify(connected[0].source); if (![...picker.options].some(o => o.value === value)) picker.add(new Option(`Preserved: ${connected[0].source.node} · ${connected[0].source.port}`,value)); picker.value = value; }
      picker.onchange = () => { try { if (picker.value) store.connect(JSON.parse(picker.value),{node:n.id,port:p.id}); else if (!p.multiple) for (const e of connected) store.disconnect(e.id); } catch(e) { tell(e.message,true); } };
      inspector.append(fieldLabel(`${p.label || friendly(p.id)} · ${p.type}${p.is_list ? ' list' : ''}${p.required ? ' · required' : hasWidget ? '' : ' · optional'}`,picker,graphPortHelp(p,hasWidget)));
      for (const e of connected) { const line = make('div','wg-connection-row'); line.append(make('small','wg-muted',`${nameOf(store.document.nodes.find(x => x.id === e.source.node),store.types)} → ${p.label || p.id}`),button('Disconnect',() => store.disconnect(e.id))); inspector.append(line); }
    }
  }
  function renderEdgeInspector(edge) {
    inspector.replaceChildren(); inspector.append(make('h2','','Connection'),make('p','wg-muted','This link supplies a real execution input. Reconnecting changes the compiled plan.'));
    const target = store.document.nodes.find(n => n.id === edge.target.node), port = portList(target,'inputs').find(p => p.id === edge.target.port); if (!target || !port) return;
    const select = make('select','wg-input'); select.setAttribute('aria-label','Connection source');
    for (const item of compatibleSources(target,port)) select.add(new Option(item.label,JSON.stringify(item.source))); select.value = JSON.stringify(edge.source);
    select.onchange = () => { try { store.connect(JSON.parse(select.value),edge.target,edge.id); edgeSelected = null; selected = target.id; renderInspector(); } catch(e) { tell(e.message,true); } };
    inspector.append(fieldLabel(`Feeds ${nameOf(target,store.types)} · ${port.label || port.id}`,select),button('Delete connection',() => { store.disconnect(edge.id); edgeSelected = null; renderInspector(); }));
  }
  async function validate(expand = false) {
    if (!store || closed) return; clearTimeout(validationTimer); validationController?.abort(); const controller = new AbortController(); validationController = controller; const token = generation;
    if (expand) validation.open = true;
    try {
      const result = await post('/validate',{document:currentDocument()},controller.signal);
      if (closed || token !== generation || controller !== validationController) return; validated = result; renderValidation(result); runBtn.disabled = queueing || !store.document.nodes.length;
    } catch(e) { if (e.name !== 'AbortError' && !closed && token === generation) { validated = null; validationTitle.textContent = 'Execution plan · Could not check'; validationBody.textContent = e.message; runBtn.disabled = queueing || !store.document.nodes.length; } }
  }
  function renderValidation(result) {
    const issues = result.issues || [], steps = Array.isArray(result.plan) ? result.plan : result.plan?.steps || [];
    validationTitle.textContent = result.valid ? `Execution plan · Ready · ${steps.length} step${steps.length === 1 ? '' : 's'}` : `Execution plan · ${issues.filter(i => i.severity === 'error').length || 'Some'} issue${issues.length === 1 ? '' : 's'} to resolve`;
    validationBody.replaceChildren();
    for (const issue of issues) { const row = make('p',issue.severity === 'error' ? 'wg-error' : 'wg-muted',issue.message || String(issue)); if (issue.node_id || issue.node) row.append(button('Inspect',() => select(issue.node_id || issue.node))); validationBody.append(row); }
    const list = make('ol','wg-plan'); for (const step of steps) { const nodeId = step.node_id || step.node || step.id; const n = store.document.nodes.find(n => n.id === nodeId); list.append(make('li','',`${n ? nameOf(n,store.types) : step.label || step.type || step.id}${step.dependencies?.length ? ` · after ${step.dependencies.length} upstream operation(s)` : ''}`)); }
    if (!steps.length && result.valid) list.append(make('li','','Validated outputs are ready to queue.')); validationBody.append(list);
  }
  async function save() {
    if (!store || saving) return; saving = true; updateDirty(); const snapshot = currentDocument(), token = loadToken, id = savedId;
    try { const value = await post('/documents',{document:snapshot,...(id ? {id,revision} : {})}); if (closed || token !== loadToken || id !== savedId) return; savedId = value.id; revision = value.revision; lastSaved = JSON.stringify(value.document || snapshot); persist(); tell('Workflow saved on this server.'); }
    catch(e) { tell(e.message,true); } finally { saving = false; if (!closed) updateDirty(); }
  }
  function exportDocument() { if (!store) return; const url = URL.createObjectURL(new Blob([JSON.stringify(store.document,null,2)],{type:'application/json'})), a = make('a'); a.href = url; a.download = `${(store.document.title || 'workflow').replace(/[^\w\-. ]/g,'_')}.odysseus.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); }
  importPicker.onchange = async() => {
    const file = importPicker.files?.[0]; importPicker.value = ''; if (!file || importing) return; importing = true; importBtn.disabled = true; const token = ++loadToken;
    try {
      if (file.size > 8*1024*1024) throw new Error('Workflow JSON exceeds 8 MiB.'); const workflow = JSON.parse(await file.text());
      const result = await post('/import',{workflow,title:file.name.replace(/\.json$/i,'')}); if (closed || token !== loadToken) return;
      if (!result.document?.nodes || !result.document?.edges) throw new Error('Import returned an invalid workflow graph.');
      replace(result.document); tell(`${file.name} imported. All operations and connections are retained; review any unsupported operations before queuing.`);
      renderSetupNotes(result.setup_notes || result.document.metadata?.setup_notes);
      if (result.issues?.length) { validation.open = true; renderValidation({valid:false,issues:result.issues,plan:[]}); }
    } catch(e) { if (!closed && token === loadToken) tell(`Import failed: ${e.message}. Your draft was kept.`,true); } finally { importing = false; importBtn.disabled = false; }
  };
  async function queue() {
    if (queueing || !store) return;
    queueing = true; runBtn.disabled = true; runBtn.textContent = 'Checking…';
    if (!validated?.valid) await validate(true);
    if (closed || !validated?.valid) { queueing = false; runBtn.disabled = !store.document.nodes.length; runBtn.textContent = 'Queue workflow'; if (!closed) { validation.open = true; tell('Resolve the execution plan issues before queuing.',true); } return; }
    runBtn.textContent = 'Queuing…';
    const snapshot = currentDocument(), body = new FormData(), runtimeBindings = {}; body.append('document',JSON.stringify(snapshot)); requestId ||= workflowRequestId(); body.append('request_id',requestId);
    const plan = Array.isArray(validated.plan) ? validated.plan : validated.plan?.steps || [];
    const bindingKeys = [...new Set(plan.flatMap(step => Object.values(step.inputs || {}).flat()).filter(key => typeof key === 'string'))];
    for (const key of bindingKeys) if (bindings[key]?.length) { runtimeBindings[key] = bindings[key].map(file => {
      if (file.serverFile) return {server:clone(file.serverFile)}; const key = graphId('input'); body.append(key,file,file.name); return {upload:key};
    }); }
    body.append('bindings',JSON.stringify(runtimeBindings));
    try { const result = await request('/runs',{method:'POST',headers:{'X-Odysseus-Submission-Id':requestId},body}); if (closed) return; requestId = null; tell(`Workflow queued · ${result.id}. You can keep editing and queue another run.`); runs.open = true; await refreshRuns(); }
    catch(e) { tell(e.message,true); if (e.details?.issues) { validation.open = true; renderValidation({valid:false,issues:e.details.issues}); } }
    finally { queueing = false; if (!closed) { runBtn.textContent = 'Queue workflow'; runBtn.disabled = !store.document.nodes.length; } }
  }
  async function refreshRuns() {
    if (closed) return; clearTimeout(runsTimer);
    try {
      const result = await request('/runs'); if (closed) return; const rows = result.runs || []; runsTitle.textContent = `Recent workflow runs · ${rows.length}`;
      if (!runsBody.querySelector('[data-refresh-runs]')) { runsBody.replaceChildren(); const refresh = button('Refresh runs',refreshRuns); refresh.dataset.refreshRuns = 'true'; runsBody.append(refresh); }
      runsBody.querySelector('.wg-runs-empty')?.remove();
      if (!rows.length) runsBody.append(make('p','wg-muted wg-runs-empty','No workflow runs yet.'));
      const visible = new Set(rows.slice(0,15).map(r => r.id));
      for (const [id,ui] of runCards) if (!visible.has(id)) { ui.card.remove(); runCards.delete(id); }
      for (const r of rows.slice(0,15)) {
        let ui = runCards.get(r.id);
        if (!ui) {
          const card = make('article','wg-run'), line = make('div','wg-run-heading'), title = make('strong'), status = make('span','wg-state'); line.append(title,status); card.append(line);
          const actions = make('div','wg-toolbar'), detail = button('Details',async() => {
            try { const value = await request(`/runs/${encodeURIComponent(r.id)}`); if (!closed) { ui.expanded = true; ui.latest = value; renderRunDetails(card,value); } } catch(e) { tell(e.message,true); }
          });
          const cancel = button('Cancel run',async() => { try { await post(`/runs/${encodeURIComponent(r.id)}/cancel`,{}); await refreshRuns(); } catch(e) { tell(e.message,true); } });
          actions.append(detail,cancel,button('Edit as new draft',async() => { try { const value = ui.latest.document ? ui.latest : await request(`/runs/${encodeURIComponent(r.id)}`); replace(value.document); tell('Loaded this run’s settings as a draft. Review source attachments, edit, then queue when ready.'); } catch(e) { tell(e.message,true); } }));
          card.append(actions); ui = {card,title,status,cancel,latest:r,expanded:false,signature:''}; runCards.set(r.id,ui); runsBody.append(card);
        }
        ui.latest = r; ui.title.textContent = r.document?.title || r.title || r.id; ui.status.textContent = r.status || 'queued';
        ui.cancel.hidden = !['queued','running','pending','waiting_for_selection'].includes(r.status);
        const signature = JSON.stringify([r.status,r.error,r.outputs,(r.steps || []).map(step => [step.id,step.status,step.error,step.job?.scout])]);
        const needsChoice = (r.steps || []).some(step => step.job?.scout?.status === 'waiting_for_selection');
        if ((ui.expanded || needsChoice) && signature !== ui.signature) { renderRunDetails(ui.card,r); ui.expanded = true; }
        ui.signature = signature;
      }
      if (rows.some(r => ['queued','running','pending','waiting_for_selection','cancelling'].includes(r.status)) && runs.open) runsTimer = setTimeout(refreshRuns,5000);
    } catch(e) { if (!closed) tell(`Could not refresh runs: ${e.message}`,true); }
  }
  function safeMediaUrl(value) { try { const url = new URL(value,location.origin); return url.origin === location.origin && url.pathname.startsWith('/api/') ? url.href : null; } catch { return null; } }
  function renderRunDetails(card,run) {
    // Polling keeps this content mounted unless execution/choice state changes;
    // checking a running job must not dismiss its playing output or preview.
    const previous = card.querySelector('.wg-run-details');
    const previousOutputs = new Map([...previous?.querySelectorAll('[data-output-key]') || []].map(el => [el.dataset.outputKey,el]));
    const previousPreviews = new Map([...previous?.querySelectorAll('[data-preview-key]') || []].map(el => [el.dataset.previewKey,el]));
    const details = make('div','wg-run-details');
    for (const step of run.steps || []) {
      const row = make('p','wg-muted',`${step.title || step.node_id || step.id}: ${step.status}${step.error ? ` · ${step.error}` : ''}`); details.append(row);
      const scout = step.job?.scout || step.scout;
      if (scout) {
        const box = make('section','wg-scout'); box.append(make('h3','','Choose a Seed Scout preview'));
        box.append(make('p','wg-muted',scout.status === 'waiting_for_selection' ? 'Compare these previews, then continue the same render with your chosen seed.' : scout.selected_seed != null ? `Selected seed: ${scout.selected_seed}` : 'Seed previews are being prepared.'));
        if (scout.error) box.append(make('p','wg-error',scout.error));
        const grid = make('div','wg-scout-grid');
        for (const preview of scout.previews || []) {
          const choice = make('article','wg-scout-choice'), url = safeMediaUrl(preview.url);
          if (url) { const key = `${step.id}:${preview.seed}:${url}`, img = previousPreviews.get(key) || make('img'); if (!img.dataset.previewKey) { img.dataset.previewKey = key; img.src = url; img.alt = `Animated preview for seed ${preview.seed}`; img.loading = 'lazy'; } choice.append(img); }
          choice.append(make('strong','',`Seed ${preview.seed}`));
          if (scout.status === 'waiting_for_selection') {
            const use = button('Continue with this seed',async() => {
              use.disabled = true;
              try {
                const job = step.job_id || step.job?.id; if (!job) throw new Error('This step has no source job.');
                const response = await fetch(`/api/video/h3/jobs/${encodeURIComponent(job)}/scout/select`,{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({seed:preview.seed})});
                let value = {}; try { value = await response.json(); } catch {}
                if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : `Could not select seed (${response.status}).`);
                tell(`Continuing Seed Scout with seed ${preview.seed}.`); await refreshRuns();
              } catch(e) { tell(e.message,true); use.disabled = false; }
            },'wg-primary'); choice.append(use);
          }
          grid.append(choice);
        }
        box.append(grid); details.append(box);
      }
    }
    if (run.error) details.append(make('p','wg-error',run.error));
    for (const output of run.outputs || []) {
      if (output.status && output.status !== 'completed') continue;
      const url = safeMediaUrl(output.url || output.download_url || output.output_url); if (!url) continue;
      const kind = graphOutputKind(output), key = `${output.id || output.name || ''}:${url}:${kind}`;
      let block = previousOutputs.get(key);
      if (!block) {
        block = make('section','wg-output'); block.dataset.outputKey = key;
        const link = make('a','wg-button',kind === 'archive' ? 'Download results ZIP' : `Download ${output.name || output.title || 'output'}`); link.href = url; link.download = output.name || ''; block.append(link);
        if (kind === 'archive') block.append(make('p','wg-muted','Includes saved outputs and result.json with the execution result.'));
        if (['video','audio','image'].includes(kind)) {
          const preview = button(kind === 'image' ? 'View output' : 'Play output',() => {
            const player = make(kind === 'image' ? 'img' : kind,'wg-media-preview'); player.src = url;
            if (kind === 'image') player.alt = output.name || 'Workflow output'; else { player.controls = true; player.preload = 'metadata'; }
            block.append(player); preview.remove();
          }); block.append(preview);
        }
      }
      details.append(block);
    } previous?.remove(); card.append(details);
  }
  function keydown(event) {
    if (closed) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); wireStart = null; closePanel(); renderCanvas(); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
    if (event.key !== 'Tab' && event.target.closest('input,textarea,select,[contenteditable=true]')) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') { event.preventDefault(); event.shiftKey ? store?.redo() : store?.undo(); }
    if (event.key === 'Delete' && edgeSelected) { event.preventDefault(); store.disconnect(edgeSelected); edgeSelected = null; renderInspector(); }
    if (event.key === 'Tab') { const controls = [...root.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')].filter(n => n.getClientRects().length); if (event.shiftKey && document.activeElement === controls[0]) { event.preventDefault(); controls.at(-1)?.focus(); } else if (!event.shiftKey && document.activeElement === controls.at(-1)) { event.preventDefault(); controls[0]?.focus(); } }
  }
  // The workspace only closes with Close. Selection drags and backdrop clicks
  // cannot discard a draft, including while it is still being loaded.
  function close() { if (closed) return; promptHistory.destroy(); closed = true; persist(); loadToken++; validationController?.abort(); clearTimeout(validationTimer); clearTimeout(runsTimer); cancelAnimationFrame(frame); if (drag) drag.up(); for (const url of mediaUrls) URL.revokeObjectURL(url); window.removeEventListener('keydown',keydown,true); overlay.remove(); if (anchor?.isConnected) anchor.focus(); }
  window.addEventListener('keydown',keydown,true);
  async function init() {
    for (const b of [newBtn,libraryBtn,saveBtn,importBtn,exportBtn,addBtn,packagesBtn,validateBtn,runBtn,undoBtn,redoBtn]) b.disabled = true;
    try {
      catalog = await request('/catalog'); if (closed) return; inventory = catalog.inventory?.h3 || {};
      let draft, serialized; try { serialized = localStorage.getItem(DRAFT); draft = JSON.parse(serialized || 'null'); } catch {}
      const restored = initial.document || draft?.document || blankWorkflowGraph(); store = createWorkflowGraphStore(restored,catalog);
      savedId = initial.document ? null : draft?.savedId || null; revision = initial.document ? null : draft?.revision || null; bindings = initial.document ? initial.bindings || {} : retainedDraft && retainedDraft.serialized === serialized ? copyBindings(retainedDraft.bindings) : draft?.serverBindings || {}; selected = draft?.selected || restored.nodes[0]?.id || null;
      view = draft?.view === 'nodes' ? 'nodes' : 'pipeline'; lastSaved = ''; store.subscribe(handleChange);
      for (const b of [newBtn,libraryBtn,saveBtn,importBtn,exportBtn,addBtn,packagesBtn,validateBtn]) b.disabled = false;
      title.value = restored.title; handleChange(restored,'replace'); setView(view); title.focus();
      renderSetupNotes(initial.setup_notes || restored.metadata?.setup_notes);
      const missing = restored.nodes.filter(n => ['input.image','input.video','input.audio','input.refmod'].includes(n.type) && n.params?.name && !bindings[n.id]?.length);
      if (missing.length) tell(`${missing.length} saved media input${missing.length === 1 ? '' : 's'} need files attached again. Open the input or reference bundle and choose Attach again.`,true);
      if (!restored.nodes.length && !initial.document) openLibrary(true);
    } catch(e) { tell(`Could not open the workflow builder: ${e.message}`,true); saved.textContent = 'Server unavailable'; }
  }
  init();
  return {close,get store() { return store; },get bindings() { return bindings; },validate,element:root};
}
