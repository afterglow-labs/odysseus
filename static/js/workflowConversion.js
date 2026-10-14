// Imported graphs are inspected as data by the server. Applying a conversion
// changes the draft only; it never queues a render or executes a custom node.
export function createWorkflowConversion({ family, recipe = '', onApply }) {
  const make = (tag, text, cls) => { const el = document.createElement(tag); if (text) el.textContent = text; if (cls) el.className = cls; return el; };
  const button = (text, fn) => { const el = make('button', text, 'workflow-studio-button'); el.type = 'button'; el.onclick = fn; return el; };
  const panel = make('section', '', 'workflow-studio-conversion'); panel.hidden = true; panel.setAttribute('aria-label', 'ComfyUI conversion');
  const picker = make('input'); picker.type = 'file'; picker.accept = '.json,application/json'; picker.hidden = true;
  let workflow = null, report = null, controller = null, destroyed = false, generation = 0;
  const invalidate = () => { generation++; controller?.abort(); controller = null; report = null; return generation; };
  const open = button('Import ComfyUI', () => picker.click());
  const heading = make('div', '', 'workflow-studio-conversion-heading');
  const title = make('strong', 'ComfyUI conversion');
  const close = button('Close report', () => { invalidate(); panel.hidden = true; }); heading.append(title, close);
  const status = make('p'); status.setAttribute('role', 'status');
  const outputLabel = make('label', 'Output to convert '), outputs = make('select'); outputLabel.append(outputs); outputLabel.hidden = true;
  const content = make('div');
  const apply = button('Apply to draft', () => {
    if (apply.disabled || !report || destroyed) return;
    try { onApply(report); status.textContent = 'Applied to the draft. Review model setup and attach the listed media before generating.'; apply.disabled = true; }
    catch (error) { status.textContent = error.message || String(error); }
  }); apply.disabled = true;
  panel.append(heading, status, outputLabel, content, apply, picker);

  function render(result) {
    if (!result || result.format !== 'odysseus-comfyui-conversion' || result.version !== 1
      || typeof result.convertible !== 'boolean'
      || (result.convertible && (!result.config || typeof result.config !== 'object' || Array.isArray(result.config))))
      throw new Error('The server returned an invalid workflow conversion report.');
    report = result; content.replaceChildren();
    const matches = result.family === family && (result.config?.recipe || '') === recipe;
    const recipeName = result.config?.recipe === 'seed_hunter' ? 'H3 Seed Hunter' : result.family === 'bfs' ? 'BFS' : 'H3 Video';
    status.textContent = result.convertible
      ? matches ? 'Ready to apply. This replaces draft settings and clears regular attachments. Staged batch files remain; review them before queuing.' : `This converts to ${recipeName}. Import it in that workflow panel.`
      : 'Some processing needs a conversion adapter. Your draft has not changed.';
    apply.disabled = !result.convertible || !result.config || !matches;
    outputLabel.hidden = (result.outputs || []).length < 2;
    const selected = outputs.value; outputs.replaceChildren(new Option('Choose an output…', ''));
    for (const output of result.outputs || []) outputs.add(new Option(`${output.type} · node ${output.id}`, output.id));
    outputs.value = selected;
    const issues = Array.isArray(result.issues) ? result.issues : [];
    if (issues.length) {
      const list = make('ul');
      for (const issue of issues) list.append(make('li', `${issue.node_type ? `${issue.node_type}${issue.node_id ? ` · node ${issue.node_id}` : ''}: ` : ''}${issue.message}`));
      content.append(list);
    }
    if (result.document?.components) {
      const details = make('details'), label = make('summary', 'Model setup'); details.append(label);
      const list = make('ul');
      for (const [role, item] of Object.entries(result.document.components)) if (item?.name) list.append(make('li', `${role.replaceAll('_', ' ')}: ${item.name}`));
      details.append(list); content.append(details);
    }
    if (result.inputs?.length) {
      const details = make('details'), label = make('summary', 'Media to attach'); details.open = true; details.append(label);
      const list = make('ul');
      for (const input of result.inputs) list.append(make('li', `${input.field.replaceAll('_', ' ')}: ${input.name || 'Choose a file'}${input.required ? ' (required)' : ''}`));
      details.append(list); content.append(details);
    }
    if (!result.convertible) {
      const save = button('Download compatibility report', () => {
        // Exclude config, prompt and original graph; this is a structural report.
        const data = JSON.stringify({format:result.format, version:result.version, convertible:false, issues, outputs:result.outputs}, null, 2);
        const url = URL.createObjectURL(new Blob([data], {type:'application/json'})), link = make('a');
        link.href = url; link.download = 'odysseus-conversion-report.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }); content.append(save);
    }
  }
  async function inspect() {
    const token = invalidate(); controller = new AbortController(); const current = controller;
    panel.hidden = false; apply.disabled = true; status.textContent = 'Checking workflow connections and local components…'; content.replaceChildren();
    try {
      const response = await fetch('/api/video/workflows/convert-comfyui', {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, signal:current.signal,
        body:JSON.stringify({workflow, ...(outputs.value ? {output_node:outputs.value} : {})})});
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : result.detail?.message || `Conversion failed (${response.status}).`);
      if (destroyed || token !== generation || current !== controller) return;
      render(result);
    } catch (error) { if (!destroyed && token === generation && current === controller && error.name !== 'AbortError') status.textContent = error.message || String(error); }
  }
  outputs.onchange = inspect;
  picker.onchange = async () => {
    const file = picker.files?.[0]; picker.value = ''; if (!file) return;
    const token = invalidate(); workflow = null;
    panel.hidden = false; outputLabel.hidden = true; outputs.replaceChildren(); content.replaceChildren(); apply.disabled = true;
    try {
      if (file.size > 1024 * 1024) throw new Error('Workflow JSON exceeds 1 MiB.');
      const value = JSON.parse(await file.text()); if (destroyed || token !== generation) return; workflow = value;
      title.textContent = `ComfyUI conversion · ${file.name}`; await inspect();
    } catch (error) { if (!destroyed && token === generation) status.textContent = error.message || String(error); }
  };
  return { button:open, panel, destroy() { destroyed = true; invalidate(); } };
}
