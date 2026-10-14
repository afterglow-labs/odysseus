// Executable graph state shared by the pipeline and canvas editors. Layout is
// persisted with the document, while media Files and server run state stay out.
export const GRAPH_FORMAT = 'odysseus-workflow-graph';
const copy = value => JSON.parse(JSON.stringify(value));
export const graphId = prefix => `${prefix}_${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`}`;
export function workflowRequestId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16); globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const h = Array.from(bytes,b => b.toString(16).padStart(2,'0')).join('');
  return `${h.slice(0,8)}-${h.slice(8,12)}-${h.slice(12,16)}-${h.slice(16,20)}-${h.slice(20)}`;
}
export const blankWorkflowGraph = () => ({format:GRAPH_FORMAT,version:1,title:'Untitled workflow',nodes:[],edges:[]});
export function graphTypes(catalog = {}) {
  const value = catalog.node_types || catalog.types || [];
  return new Map(Array.isArray(value) ? value.map(item => [item.type || item.id,item]) : Object.entries(value));
}
export function graphPorts(type, direction) {
  const value = type?.[direction] || [];
  return Array.isArray(value) ? value.map(p => typeof p === 'string' ? {id:p,name:p,type:p} : {...p,id:p.id || p.name}) : Object.entries(value).map(([id,p]) => ({...(typeof p === 'string' ? {type:p} : p),id,name:id}));
}
export function graphFields(type) {
  const value = type?.params || type?.parameters || type?.fields || [];
  return Array.isArray(value) ? value.map(p => ({...p,id:p.id || p.name || p.key})) : Object.entries(value).map(([id,p]) => ({...(typeof p === 'object' && p ? p : {default:p}),id}));
}
export function graphOrder(document) {
  const pending = new Map(document.nodes.map(n => [n.id,n])), result = [];
  while (pending.size) {
    const ready = [...pending.values()].filter(n => !document.edges.some(e => e.target.node === n.id && pending.has(e.source.node)));
    if (!ready.length) { result.push(...pending.values()); break; }
    for (const n of ready) { result.push(n); pending.delete(n.id); }
  }
  return result;
}
export function connectionIssue(document, types, source, target, ignoreEdge) {
  const from = document.nodes.find(n => n.id === source.node), to = document.nodes.find(n => n.id === target.node);
  if (!from || !to) return 'Both operations must exist.';
  if (from.id === to.id) return 'An operation cannot connect to itself.';
  const output = graphPorts(types.get(from.type),'outputs').find(p => p.id === source.port), input = graphPorts(types.get(to.type),'inputs').find(p => p.id === target.port);
  if (!output || !input) return 'That port is unavailable. Unknown imported operations must be resolved before connecting.';
  const accepts = input.accepts || String(input.type).split('|'), provides = String(output.type).split('|');
  if (!provides.includes('*') && !accepts.includes('*') && !provides.some(type => accepts.includes(type))) return `${output.type} cannot connect to ${input.type}.`;
  const remaining = document.edges.filter(e => e.id !== ignoreEdge && (input.multiple || e.target.node !== target.node || e.target.port !== target.port));
  const visited = new Set(), visit = id => {
    if (id === from.id) return true;
    if (visited.has(id)) return false;
    visited.add(id); return remaining.filter(e => e.source.node === id).some(e => visit(e.target.node));
  };
  if (visit(to.id)) return 'This connection would create a cycle.';
  return null;
}

export function createWorkflowGraphStore(initial = blankWorkflowGraph(), catalog = {}) {
  let document = copy(initial), undo = [], redo = [];
  const subscribers = new Set(), types = graphTypes(catalog);
  const emit = reason => subscribers.forEach(callback => callback(document,reason));
  const change = (mutate,reason = 'edit') => {
    const next = copy(document); mutate(next);
    if (JSON.stringify(next) === JSON.stringify(document)) return false;
    undo.push(document); if (undo.length > 100) undo.shift(); redo = []; document = next; emit(reason); return true;
  };
  const store = {
    get document() { return document; }, get types() { return types; },
    get canUndo() { return !!undo.length; }, get canRedo() { return !!redo.length; },
    subscribe(callback) { subscribers.add(callback); return () => subscribers.delete(callback); },
    replace(value) { undo = []; redo = []; document = copy(value); emit('replace'); },
    title(value) { change(d => { d.title = value; },'title'); },
    add(type,position) {
      const spec = types.get(type); if (!spec) throw new Error('This operation is not in the catalog.');
      const id = graphId('node'), params = {};
      for (const field of graphFields(spec)) if (field.default !== undefined) params[field.id] = copy(field.default);
      change(d => d.nodes.push({id,type,params,state:'active',position:position || {x:48+d.nodes.length%4*272,y:64+Math.floor(d.nodes.length/4)*224}})); return id;
    },
    patch(id,patch,reason = 'edit') { change(d => { const n = d.nodes.find(n => n.id === id); if (n) Object.assign(n,copy(patch),{id:n.id,type:n.type}); },reason); },
    parameter(id,key,value) { change(d => { const n = d.nodes.find(n => n.id === id); if (n) { n.params ||= {}; n.params[key] = copy(value); } },'parameter'); },
    remove(id) { change(d => { d.nodes = d.nodes.filter(n => n.id !== id); d.edges = d.edges.filter(e => e.source.node !== id && e.target.node !== id); }); },
    duplicate(id) {
      const n = document.nodes.find(n => n.id === id); if (!n) return null; const nextId = graphId('node');
      change(d => { const clone = {...copy(n),id:nextId,title:`${n.title || types.get(n.type)?.label || n.type} copy`,position:{x:(n.position?.x || 48)+32,y:(n.position?.y || 48)+32}}; d.nodes.push(clone); for (const edge of d.edges.filter(e => e.target.node === id)) d.edges.push({...copy(edge),id:graphId('edge'),target:{...edge.target,node:nextId}}); }); return nextId;
    },
    connect(source,target,replaceEdge) {
      const issue = connectionIssue(document,types,source,target,replaceEdge); if (issue) throw new Error(issue);
      const input = graphPorts(types.get(document.nodes.find(n => n.id === target.node).type),'inputs').find(p => p.id === target.port);
      if (document.edges.some(e => e.source.node === source.node && e.source.port === source.port && e.target.node === target.node && e.target.port === target.port)) return;
      change(d => { d.edges = d.edges.filter(e => e.id !== replaceEdge && (input.multiple || e.target.node !== target.node || e.target.port !== target.port)); d.edges.push({id:graphId('edge'),source:{...source},target:{...target}}); });
    },
    disconnect(id) { change(d => { d.edges = d.edges.filter(e => e.id !== id); }); },
    undo() { if (!undo.length) return; redo.push(document); document = undo.pop(); emit('undo'); },
    redo() { if (!redo.length) return; undo.push(document); document = redo.pop(); emit('redo'); },
    layout() { change(d => {
      const ordered = graphOrder(d), depths = new Map(), rows = new Map();
      // Align short input branches beside their consumer instead of stacking
      // every source in one enormous column and shrinking the whole canvas.
      for (const n of [...ordered].reverse()) {
        const downstream = d.edges.filter(e => e.source.node === n.id).map(e => (depths.get(e.target.node) ?? -1)+1);
        depths.set(n.id,Math.max(0,...downstream));
      }
      const maxDepth = Math.max(0,...depths.values());
      const priority = n => n.type === 'hub.setup' || n.type === 'model.lora' ? 0 : n.type === 'input.references' ? 2 : 1;
      const arranged = [...ordered].sort((a,b) => (maxDepth-depths.get(a.id))-(maxDepth-depths.get(b.id)) || priority(a)-priority(b));
      for (const n of arranged) {
        const column = maxDepth-depths.get(n.id), top = rows.get(column) || 56;
        const ports = Math.max(graphPorts(types.get(n.type),'inputs').length,graphPorts(types.get(n.type),'outputs').length);
        rows.set(column,top+Math.max(112,73+ports*23)+48); n.position = {x:48+column*288,y:top};
      }
    },'layout'); },
  };
  return store;
}
