const make = (tag, text) => { const n = document.createElement(tag); if (text) n.textContent = text; return n; };

export function autoRefineParent(job) {
  return ['completed', 'failed', 'stopped'].includes(job.status) &&
    (job.seed_hunter?.stage === 'preview' || job.seed_hunter?.stage === 'scout' && !job.seed_hunter?.source_job_id && job.scout?.mode !== 'final');
}

export async function allPreviewJobs(request, scout) {
  const found = []; let page = 1, before;
  do {
    const query = new URLSearchParams({page: String(page), page_size: '50', recipe: 'seed_hunter', stage: scout ? 'scout' : 'not_scout'});
    if (before !== undefined) query.set('before', String(before));
    const result = await request(`/jobs?${query}`);
    found.push(...(result.jobs || []).filter(autoRefineParent));
    if (found.length > 1000) throw new Error('Select at most 1,000 preview jobs per bulk operation.');
    before = result.pagination?.before;
    if (!result.pagination || page >= result.pagination.total_pages) break;
    page++;
  } while (true);
  return found;
}

export function createAutoRefineSettings(onChange) {
  const node = make('fieldset'); node.className = 'h3-auto-refine-settings';
  node.append(make('legend', 'Batch refinement'));
  const project = make('input'); project.type = 'text'; project.maxLength = 80; project.className = 'cookbook-field-input'; project.id = 'h3-auto-project';
  const label = make('label', 'Project / batch name'); label.className = 'h3-video-field'; label.append(project); node.append(label);
  const all = make('input'); all.type = 'checkbox'; all.checked = true;
  const allLabel = make('label', ' All candidates'); allLabel.prepend(all); node.append(allLabel);
  const candidates = make('div'); candidates.className = 'h3-video-actions'; node.append(candidates);
  const existing = make('select'); existing.className = 'cookbook-field-input'; existing.setAttribute('aria-label', 'Existing results');
  existing.add(new Option('Skip candidates with an existing refinement', 'skip'));
  existing.add(new Option('Reprocess all selected candidates', 'rerender'));
  const policyLabel = make('label', 'Existing results'); policyLabel.className = 'h3-video-field'; policyLabel.append(existing); node.append(policyLabel);
  node.append(make('p', 'Each candidate keeps its saved seed, prompt, references and duration. Jobs render one at a time. Existing results are included in Project / Date / Candidate folders; filenames retain the input name with a unique suffix.'));
  const inherit = make('input'); inherit.type = 'checkbox'; inherit.checked = true;
  const inheritLabel = make('label', ' Keep each job’s final duration'); inheritLabel.prepend(inherit); node.append(inheritLabel);
  let rows = [];
  all.onchange = () => { rows.forEach(n => n.disabled = all.checked); onChange(); };
  node.addEventListener('change', onChange); project.addEventListener('input', onChange);
  return {node, inherit, setValue(value = {}, count = 3, scout = false) {
    project.value = value.project || ''; existing.value = value.existing || 'skip';
    all.checked = !value.candidates?.length || value.candidates === 'all';
    candidates.replaceChildren(); rows = [];
    for (let i = 1; i <= count; i++) {
      const input = make('input'); input.type = 'checkbox'; input.value = i; input.checked = Array.isArray(value.candidates) && value.candidates.includes(i); input.disabled = all.checked;
      const wrap = make('label', ` Candidate ${i}`); wrap.prepend(input); candidates.append(wrap); rows.push(input);
    }
    inherit.checked = scout || !value.refinement?.frames; inherit.disabled = scout;
  }, getValue(refinement) {
    const chosen = all.checked ? 'all' : rows.filter(n => n.checked).map(n => Number(n.value));
    if (!chosen.length) throw new Error('Choose one or more candidates, or All candidates.');
    if (!project.value.trim()) throw new Error('Name the project / batch for these outputs.');
    const patch = structuredClone(refinement); delete patch.seed;
    if (inherit.checked) delete patch.frames;
    return {project: project.value.trim(), candidates: chosen, existing: existing.value, refinement: patch};
  }, setDisabled(value) { node.disabled = value; }};
}
