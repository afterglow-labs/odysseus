import { topPortalZ } from './toolWindowZOrder.js';
import { bindMenuDismiss } from './escMenuStack.js';

const API = '/api/image-workflows';
const drafts = new Map();
const pendingIDs = new Map();
const make = (tag, text = '', className = '') => {
  const node = document.createElement(tag); node.textContent = text; node.className = className; return node;
};
const button = text => { const node = make('button', text, 'memory-toolbar-btn'); node.type = 'button'; return node; };
async function request(path, options = {}) {
  const response = await fetch(API + path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The image request failed.');
  return data;
}

export async function showImageWorkflows(recipe = 'qwen_rapid', anchor = document.activeElement) {
  if (!document.getElementById('h3-video-styles')) {
    const style = make('link'); style.id = 'h3-video-styles'; style.rel = 'stylesheet'; style.href = '/static/h3-video.css'; document.head.append(style);
  }
  const overlay = make('div', '', 'cookbook-edit-overlay h3-video-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', '', 'cookbook-edit-modal h3-video-dialog');
  dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'Image workflows');
  const header = make('div', '', 'h3-video-header'), close = button('Close');
  header.append(make('h2', 'Image workflows'), close);
  const body = make('div', '', 'h3-video-scroll'), error = make('p'); error.setAttribute('role', 'alert');
  const toolbar = make('div', '', 'h3-video-toolbar'), picker = make('select', '', 'cookbook-field-input'), refresh = button('Refresh models');
  picker.setAttribute('aria-label', 'Image workflow'); toolbar.append(picker, refresh);
  const details = make('div'), form = make('form', '', 'h3-video-form'), jobs = make('section');
  body.append(toolbar, details, error, form, make('h3', 'Recent images'), jobs); dialog.append(header, body); overlay.append(dialog); document.body.append(overlay);
  let inventory, selected, config, timer, closed = false, submissionID = null, busy = false, cancelRequest = null;
  let references, maskFile, maskCanvas, paintCanvas, painted = false, baseImage = null, brush;
  const cards = new Map();
  close.onclick = bindMenuDismiss(overlay, () => { closed = true; clearTimeout(timer); overlay.remove(); anchor?.focus?.(); });
  const invalidate = () => { submissionID = null; pendingIDs.delete(picker.value); };
  const fail = problem => { error.textContent = problem.message || String(problem); };
  function field(labelText, key, type = 'number', options = null) {
    const label = make('label', '', 'h3-video-field'); label.append(make('span', labelText));
    const input = make(options ? 'select' : type === 'textarea' ? 'textarea' : 'input', '', 'cookbook-field-input');
    if (options) for (const [value, text] of options) input.add(new Option(text, value));
    else if (type !== 'textarea') input.type = type;
    input.setAttribute('aria-label', labelText); input.name = key; input.value = config[key] ?? '';
    if (type === 'textarea') input.rows = 4;
    if (type === 'number') { input.step = ['cfg', 'denoise'].includes(key) ? '.05' : '1'; }
    input.oninput = () => { config[key] = type === 'number' ? Number(input.value) : input.value; invalidate(); };
    label.append(input); return { label, input };
  }
  function sourceField(labelText, multiple = false) {
    const label = make('label', '', 'h3-video-field'), input = make('input'); input.type = 'file'; input.accept = 'image/png,image/jpeg,image/webp'; input.multiple = multiple;
    input.setAttribute('aria-label', labelText); label.append(make('span', labelText), input); return { label, input };
  }
  function maskEditor(parent) {
    paintCanvas = make('canvas'); paintCanvas.style.cssText = 'width:100%;max-width:700px;touch-action:none;display:none;border:1px solid var(--border);';
    paintCanvas.setAttribute('aria-label', 'Paint the area to replace, or upload a mask above');
    maskCanvas = make('canvas'); const controls = make('div', '', 'h3-video-toolbar');
    brush = make('input'); brush.type = 'range'; brush.min = 5; brush.max = 150; brush.value = 40; brush.setAttribute('aria-label', 'Mask brush size');
    const clear = button('Clear painted mask'), all = button('Paint entire image'); controls.append(make('span', 'Brush size'), brush, clear, all);
    const redraw = () => {
      if (!baseImage) return;
      const ctx = paintCanvas.getContext('2d'); ctx.drawImage(baseImage, 0, 0, paintCanvas.width, paintCanvas.height);
      ctx.globalAlpha = .45; ctx.globalCompositeOperation = 'screen'; ctx.drawImage(maskCanvas, 0, 0); ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
    };
    const fill = value => { const ctx = maskCanvas.getContext('2d'); ctx.fillStyle = value ? 'white' : 'black'; ctx.fillRect(0, 0, maskCanvas.width, maskCanvas.height); painted = value; invalidate(); redraw(); };
    clear.onclick = () => fill(false); all.onclick = () => fill(true);
    references.onchange = async () => {
      invalidate(); painted = false; baseImage = null; paintCanvas.style.display = 'none';
      const file = references.files[0]; if (!file) return;
      const url = URL.createObjectURL(file), image = new Image();
      try {
        image.src = url; await image.decode(); baseImage = image;
        const scale = Math.min(1, 1024 / Math.max(image.width, image.height));
        paintCanvas.width = maskCanvas.width = Math.round(image.width * scale); paintCanvas.height = maskCanvas.height = Math.round(image.height * scale);
        fill(false); paintCanvas.style.display = 'block';
      } catch (problem) { fail(problem); } finally { URL.revokeObjectURL(url); }
    };
    let drawing = false;
    function draw(event) {
      if (!drawing || !baseImage) return;
      const rect = paintCanvas.getBoundingClientRect(), x = (event.clientX - rect.left) * paintCanvas.width / rect.width, y = (event.clientY - rect.top) * paintCanvas.height / rect.height;
      const ctx = maskCanvas.getContext('2d'); ctx.fillStyle = 'white'; ctx.beginPath(); ctx.arc(x, y, Number(brush.value) * paintCanvas.width / rect.width / 2, 0, Math.PI * 2); ctx.fill(); painted = true; invalidate(); redraw();
    }
    paintCanvas.onpointerdown = event => { drawing = true; paintCanvas.setPointerCapture(event.pointerId); draw(event); };
    paintCanvas.onpointermove = draw; paintCanvas.onpointerup = paintCanvas.onpointercancel = () => { drawing = false; };
    parent.append(make('p', 'Paint the area to replace. A supplied mask file takes precedence; white replaces, black preserves.'), controls, paintCanvas);
  }
  function renderDetails() {
    details.replaceChildren(make('p', selected.description, 'h3-video-description'), make('p', selected.encoder, 'h3-video-description'));
    const models = make('details', '', 'h3-workflow-help'); models.open = !selected.ready;
    models.append(make('summary', selected.ready ? 'Models installed' : 'Missing model files'), make('p', selected.adapters, 'h3-video-description'));
    for (const component of selected.components) models.append(make('p', `${component.available ? 'Installed' : 'Missing'}: ${component.name}`, 'h3-video-component-path'));
    const source = make('a', 'Publisher instructions', 'memory-toolbar-btn'); source.href = selected.source; source.target = '_blank'; source.rel = 'noopener noreferrer'; models.append(source); details.append(models);
  }
  function renderForm() {
    form.replaceChildren(); cards.clear(); jobs.replaceChildren(); submissionID = pendingIDs.get(picker.value) || null; painted = false; baseImage = null;
    selected = inventory.workflows.find(w => w.id === picker.value);
    config = drafts.get(selected.id) || { ...selected.defaults, recipe: selected.id, prompt: '', negative_prompt: '' }; drafts.set(selected.id, config);
    renderDetails();
    const operation = field('Operation', 'mode', 'text', selected.modes.map(v => [v, { generate: 'Text to image', edit: 'Edit reference images', inpaint: 'Inpaint an image' }[v]]));
    const promptFields = make('fieldset', '', 'h3-auto-refine-settings');
    promptFields.append(make('legend', 'Image'), operation.label, field('Prompt', 'prompt', 'textarea').label);
    if (selected.id === 'lustify_inpaint') promptFields.append(field('Negative prompt', 'negative_prompt', 'textarea').label);
    form.append(promptFields);
    const inputs = make('section'), sources = sourceField(selected.max_images > 1 ? 'Reference images (up to four)' : 'Source image', selected.max_images > 1);
    references = sources.input; references.onchange = invalidate; inputs.append(sources.label);
    const masks = sourceField('Mask file (optional if painted)'); maskFile = masks.input; maskFile.onchange = invalidate;
    if (selected.id === 'lustify_inpaint') { inputs.append(masks.label); maskEditor(inputs); }
    form.append(inputs);
    const updateMode = () => { inputs.hidden = config.mode === 'generate'; }; operation.input.addEventListener('change', updateMode); updateMode();
    const grid = make('div', '', 'h3-video-grid');
    for (const [label, key] of [['Width', 'width'], ['Height', 'height'], ['Steps', 'steps'], ['CFG', 'cfg'], ['Seed', 'seed']]) grid.append(field(label, key).label);
    grid.append(field('GPU', 'gpu', 'text', inventory.gpus.map(g => [g.id, `${g.name} · ${Math.round(g.memory_total_mib / 1024)} GB`])).label);
    grid.append(field('Sampler', 'sampler', 'text', inventory.samplers.map(v => [v, v])).label, field('Schedule', 'scheduler', 'text', inventory.schedulers.map(v => [v, v])).label);
    if (selected.id === 'lustify_inpaint') grid.append(field('Denoise strength', 'denoise').label);
    form.append(grid);
    const submit = button('Queue image'); submit.type = 'submit'; submit.disabled = !selected.ready || busy;
    const actions = make('div', '', 'h3-video-toolbar'); actions.append(submit); form.append(actions);
    form.onsubmit = async event => {
      event.preventDefault(); if (busy) return; error.textContent = '';
      if (!config.prompt.trim()) return fail(new Error('Enter a prompt.'));
      const imageFiles = config.mode === 'generate' ? [] : Array.from(references.files);
      if (imageFiles.length > selected.max_images) return fail(new Error(`Choose at most ${selected.max_images} images.`));
      if (config.mode !== 'generate' && !imageFiles.length) return fail(new Error('Choose a source image.'));
      if (config.mode === 'inpaint' && !maskFile.files[0] && !painted) return fail(new Error('Paint an area or supply a mask file.'));
      busy = true; submissionID ||= crypto.randomUUID(); pendingIDs.set(selected.id, submissionID);
      const submittedRecipe = selected.id, submittedID = submissionID;
      for (const control of form.elements) control.disabled = true;
      picker.disabled = refresh.disabled = true;
      const data = new FormData(); data.set('config', JSON.stringify(config));
      imageFiles.forEach(file => data.append('images', file));
      if (config.mode === 'inpaint') {
        const mask = maskFile.files[0] || await new Promise(resolve => maskCanvas.toBlob(resolve, 'image/png'));
        data.append('mask', mask, 'mask.png');
      }
      cancelRequest = new AbortController();
      try {
        await request('/jobs', { method: 'POST', headers: { 'X-Odysseus-Submission-Id': submittedID }, body: data, signal: cancelRequest.signal });
        if (pendingIDs.get(submittedRecipe) === submittedID) pendingIDs.delete(submittedRecipe);
        submissionID = null; await loadJobs();
      } catch (problem) { if (!closed) fail(problem); } finally {
        busy = false; cancelRequest = null;
        if (!closed) { for (const control of form.elements) control.disabled = false; picker.disabled = refresh.disabled = false; submit.disabled = !selected.ready; }
      }
    };
  }
  async function loadJobs() {
    const result = await request('/jobs'); if (closed) return;
    const shown = result.jobs.filter(j => j.config.recipe === selected.id);
    if (shown.length) jobs.querySelector('[data-empty]')?.remove();
    for (const job of shown) {
      let card = cards.get(job.id);
      if (!card) {
        const node = make('article', '', 'admin-card'), state = make('p'), stop = button('Stop'), use = button('Use settings');
        node.append(make('p', job.config.prompt), state, stop, use); jobs.append(node);
        card = { node, state, stop, image: null }; cards.set(job.id, card);
        stop.onclick = async () => { try { await request(`/jobs/${job.id}/stop`, { method: 'POST' }); await loadJobs(); } catch (problem) { fail(problem); } };
        use.onclick = () => { if (busy) return; invalidate(); drafts.set(selected.id, { ...job.config }); renderForm(); loadJobs().catch(fail); };
      }
      const p = job.progress || {};
      card.state.textContent = `${job.status} · ${job.status === 'failed' ? job.error || 'Generation failed' : p.phase || job.phase}${p.total_steps ? ` · ${p.step}/${p.total_steps}` : ''}`;
      card.stop.hidden = !['queued', 'running', 'stopping'].includes(job.status);
      if (job.image_url && !card.image) {
        const image = make('img'); image.src = job.image_url; image.alt = `Result from ${selected.name}`; image.style.cssText = 'display:block;width:100%;max-height:600px;object-fit:contain;';
        const link = make('a', 'Download image'); link.href = job.image_url; link.download = `${job.id}.png`; card.node.append(image, link); card.image = image;
      }
    }
    for (const job of shown) jobs.append(cards.get(job.id).node);
    if (!shown.length && !jobs.childNodes.length) { const empty = make('p', 'No images queued for this workflow yet.'); empty.dataset.empty = ''; jobs.append(empty); }
  }
  async function loadInventory() {
    inventory = await request('/inventory'); if (closed) return;
    picker.replaceChildren(); inventory.workflows.forEach(w => picker.add(new Option(w.name, w.id))); picker.value = recipe; renderForm();
  }
  picker.onchange = () => { recipe = picker.value; error.textContent = ''; renderForm(); loadJobs().catch(fail); };
  refresh.onclick = async () => { try { await loadInventory(); await loadJobs(); } catch (problem) { fail(problem); } };
  async function poll() { if (closed) return; try { await loadJobs(); } catch (problem) { if (!closed) fail(problem); } finally { if (!closed) timer = setTimeout(poll, 2500); } }
  try { await loadInventory(); picker.focus(); await poll(); } catch (problem) { if (!closed) fail(problem); }
}
