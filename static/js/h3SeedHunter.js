import { bindMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { bindSeedHunterHelp } from './seedHunterHelp.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { createH3LoraEditor, h3LoraStack, h3LoraComponents, h3LoraIssue, insertH3LoraTrigger } from './h3Loras.js';
import { showH3LoraLibrary } from './h3LoraLibrary.js';
import { showVideoServerFiles } from './videoServerFiles.js';
import { referenceVideoRanges, restoreReferenceVideoRanges, createReferenceVideoRangeEditor, referenceVideoRangesField } from './referenceVideoRange.js';
import { appendVideoInputs } from './videoInputs.js';
import { createVideoBatchQueue } from './videoBatch.js';
import { createVideoJobReruns } from './videoJobRerun.js';
import { createVideoQueueControls, videoQueueField } from './videoQueue.js';
import { createVideoJobEditor, videoJobEditFormData } from './videoJobEdit.js';
import { showVideoWorkflowExportOptions } from './h3Video.js';
import * as videoWorkflow from './videoWorkflow.js';

const API = '/api/video/h3', STORAGE = 'odysseus-h3-seed-hunter-v1';
const ACTIVE = new Set(['queued', 'running']), TERMINAL = new Set(['completed', 'failed', 'stopped']);
const OUTER = ['mode', 'model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler', 'gpu', 'vae_gpu', 'width', 'height', 'frames', 'steps', 'seed', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'reference_size'];
const FINAL_FIELDS = new Set(['width','height','steps','seed','sampler','scheduler','gpu','vae_gpu','latent_upscaler','shift_video','shift_audio']);
const FINAL_OPTIONS = new Set(['denoise','output_fps','upscaler_chunking','upscaler_force_unload','shift_enabled','attention_backend','sparse_method','sparse_keep_percent','sparse_start','sparse_end','sparse_min_tokens','sparse_extra_tokens','sparse_sink_conditioning','ff_chunk_size','ff_chunk_threshold','low_vram_chunks']);
const NUMERIC = new Set(['width', 'height', 'frames', 'steps', 'seed', 'shift_video', 'shift_audio']);
const NESTED = ['stage', 'preview_count', 'denoise', 'shift_enabled', 'first_frame_guide', 'last_frame_guide', 'force_audio', 'continuation_overlap', 'output_fps', 'attention_backend', 'sparse_method', 'upscaler_chunking', 'upscaler_force_unload', 'source_start_seconds', 'source_end_seconds', 'audio_start_seconds', 'audio_end_seconds'];
const BASE = { mode: 'ref2va', width: 480, height: 864, frames: 73, steps: 6, seed: 42, sampler: 'er_sde', scheduler: 'beta', shift_video: 12, shift_audio: 6, reference_size: 'max', vae_gpu: '', loras: [] };
const OPTIONS = { stage: 'preview', preview_count: 3, denoise: 0.4, shift_enabled: false, first_frame_guide: false, last_frame_guide: false, force_audio: false, continuation_overlap: 22, output_fps: 24, attention_backend: 'comfy kitchen attention', sparse_method: 'none', upscaler_chunking: true, upscaler_force_unload: false, source_start_seconds: 0, source_end_seconds: null, audio_start_seconds: 0, audio_end_seconds: null, sparse_keep_percent:10,sparse_start:.2,sparse_end:1,sparse_min_tokens:12288,sparse_extra_tokens:256,sparse_sink_conditioning:'exact_kv_and_rows',ff_chunk_size:0,ff_chunk_threshold:2048,low_vram_chunks:0,refmod_strengths:[] };
const make = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const button = text => { const n = make('button', 'memory-toolbar-btn', text); n.type = 'button'; return n; };
const displayName = item => item.display_name || item.name;
const clone = value => JSON.parse(JSON.stringify(value));
const randomSeed = () => Math.floor(Math.random() * 2 ** 48);

export function seedHunterBatchChoices(stage) {
  const copies={field:'copies',label:'Copies of this draft'};
  if(stage==='final')return [];
  if(stage==='continuation')return [{field:'source_video',label:'Continuation source videos'}];
  if(stage==='dialogue')return [{field:'reference_audio',label:'Voice reference audio'},copies];
  return [{field:'reference_videos',label:'Reference videos'},{field:'reference_images',label:'Reference pictures'},{field:'reference_audio',label:'Reference audio'},copies];
}
export function seedHunterBatchConfig(value, automatic) {
  // Submission validation runs before the server replaces automatic frame counts.
  // A preceding continuation draft may still hold a valid shorter duration.
  return { ...value, ...(automatic && ['preview', 'single'].includes(value.seed_hunter?.stage)
    ? { frames: Math.max(73, Number(value.frames) || 73) } : {}) };
}
export function seedHunterFrames(seconds) {
  const raw = Math.max(5, Math.round(Number(seconds) * 24));
  return raw + (5 - raw % 17 + 17) % 17;
}
export function seedHunterResolution(megapixels, widthRatio, heightRatio, alignment = 32) {
  const values = [megapixels, widthRatio, heightRatio, alignment].map(Number);
  if (values.some(value => !Number.isFinite(value) || value <= 0)) throw new Error('Choose a positive resolution and aspect ratio.');
  const scale = Math.sqrt(values[0] * 1024 * 1024 / (values[1] * values[2]));
  return { width: Math.max(alignment, Math.round(values[1] * scale / alignment) * alignment), height: Math.max(alignment, Math.round(values[2] * scale / alignment) * alignment) };
}
export function seedHunterRefinementDefaults(source, defaults, remembered = {}) {
  return { ...defaults, denoise: source.seed_hunter?.denoise ?? defaults.denoise ?? .4, ...remembered };
}
export function seedHunterDialoguePrompt(dialogue, tone, template) {
  return String(template).replaceAll('{{dialogue}}', String(dialogue)).replaceAll('{{tone}}', String(tone));
}
export function seedHunterConfig(values, options, loras) {
  const config = { recipe: 'seed_hunter', prompt: values.prompt || '', loras: clone(loras), seed_hunter: { ...OPTIONS, ...options } };
  for (const key of OUTER) if (values[key] !== undefined) config[key] = NUMERIC.has(key) ? Number(values[key]) : values[key];
  if (['single', 'continuation'].includes(config.seed_hunter.stage)) config.seed_hunter.preview_count = 1;
  if(!['preview','final'].includes(config.seed_hunter.stage) || !config.latent_upscaler) delete config.latent_upscaler;
  return config;
}
export function seedHunterEnhancerInputs(uploads, options) {
  const images={}, counts={}; let missingImages=false;
  for(const key of ['reference_images','reference_videos','reference_audio','source_video','first_frame','last_frame']) {
    const active=key==='source_video'?options.stage==='continuation':key==='first_frame'?options.first_frame_guide:key==='last_frame'?options.last_frame_guide:true;
    const state=uploads[key]||{}, files=active?state.files||[]:[], retained=active?state.retained||[]:[];
    counts[key]=files.length+retained.length;
    if(['reference_images','first_frame','last_frame'].includes(key)){images[key]=files;missingImages ||= retained.length>0;}
  }
  return {images,counts,issue:missingImages?'Reattach the saved images to enhance this prompt with their context. Your job keeps its saved inputs.':''};
}
export function saveSeedHunterQueuedJob(send, id, body) {
  return send(`/jobs/${encodeURIComponent(id)}`, { method: 'PATCH', body });
}
async function request(path, options = {}) {
  const response = await fetch(API + path, { credentials: 'same-origin', ...options });
  let data = {}; try { data = await response.json(); } catch {}
  if (!response.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : data.error || `Request failed (${response.status}).`), { status: response.status });
  return data;
}

export function showH3SeedHunter({} = {}, anchor = document.activeElement) {
  document.querySelectorAll('.h3-seed-hunter-overlay').forEach(dismissOrRemove);
  if (!document.getElementById('h3-video-styles')) { const css = make('link'); css.id = 'h3-video-styles'; css.rel = 'stylesheet'; css.href = '/static/h3-video.css'; document.head.append(css); }
  let saved = {}; try { saved = JSON.parse(localStorage.getItem(STORAGE) || '{}'); } catch {}
  let inventory = null, jobs = [], closed = false, busy = false, loading = false, polling = false, revision = 0;
  let picker, library, queue, batchQueue, jobEditor, editingComponents = null, selection = null, finalRequest = null, finalBusy = false;
  let restoredRangeConfig = {}, optionSnapshot = {}, selecting = 0, finalComponents = [], enhancer = [], enhancing = false, enhancerController, previousPrompt = null;
  const fields = {}, options = {}, uploads = {}, finalFields = {}, jobNodes = new Map();
  const overlay = make('div', 'cookbook-edit-overlay h3-seed-hunter-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal h3-video-dialog h3-seed-hunter-dialog'); dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', 'H3 Seed Hunter');
  const header = make('div', 'h3-video-header'), closeButton = button('Close'); header.append(make('h2', '', 'H3 Seed Hunter'), closeButton);
  const scroll = make('div', 'h3-video-scroll'); dialog.append(header, scroll); overlay.append(dialog); document.body.append(overlay);
  scroll.append(make('p', 'h3-video-target', `Local Odysseus server · ${window.location.host}`), make('p', 'h3-video-description', 'Generate previews, choose a result, then refine its saved latent at a higher resolution. Each stage enters the shared video queue.'));
  const status = make('p', 'h3-video-muted', 'Checking Seed Hunter components…'); status.setAttribute('role', 'status');
  const error = make('p', 'h3-video-error'); error.hidden = true; error.setAttribute('role', 'alert');
  const toolbar = make('div', 'h3-video-toolbar'), refresh = button('Refresh components'), exportButton = button('Export workflow'), importButton = button('Import workflow');
  const importInput = make('input'); importInput.type = 'file'; importInput.accept = '.zip,.json'; importInput.hidden = true;
  toolbar.append(refresh, exportButton, importButton, importInput); scroll.append(status, toolbar, error);
  const form = make('form', 'h3-video-form'); scroll.append(form);
  const componentGrid = make('div', 'h3-video-grid'); form.append(componentGrid);
  function field(parent, key, label, control, target = fields) {
    const wrap = make('label', 'h3-video-field'); wrap.append(make('span', '', label), control); parent.append(wrap);
    control.id = `seed-hunter-${target === finalFields ? 'final-' : target === options ? 'option-' : ''}${key}`; control.classList.add('cookbook-field-input'); target[key] = control; return control;
  }
  function select(parent, key, label, choices = [], target = fields) { const n = make('select'); for (const [value, text] of choices) n.add(new Option(text, value)); return field(parent, key, label, n, target); }
  function number(parent, key, label, value, min, max, step = 1, target = fields) { const n = make('input'); n.type = 'number'; n.value = value; n.min = min; n.max = max; n.step = step; return field(parent, key, label, n, target); }
  function check(parent, key, label, checked = false) { const n = make('input'); n.type = 'checkbox'; n.checked = checked; field(parent, key, label, n, options); return n; }
  function group(parent, title, expanded = false) { const n = make('details', 'h3-seed-hunter-details'); n.open = expanded; n.append(make('summary', '', title)); const body = make('div', 'h3-video-grid'); n.append(body); parent.append(n); return body; }
  for (const [key, label] of [['latent_upscaler', 'Refinement latent upscaler'], ['model', 'H3 Ref2VA model'], ['encoder', 'Text encoder'], ['video_vae', 'Video VAE'], ['audio_vae', 'Audio VAE'], ['gpu', 'Model / encoder GPU'], ['vae_gpu', 'VAE GPU']]) select(componentGrid, key, label);
  fields.mode = make('input'); fields.mode.type = 'hidden'; fields.mode.value = 'ref2va'; form.append(fields.mode);
  const stage = select(componentGrid, 'stage', 'Generation', [['preview', 'Seed previews → selected upscale'], ['single', 'Single pass video'], ['continuation', 'Continue a video'], ['dialogue', 'Dialogue / audio previews'], ['final', 'Saved latent refinement']], options);
  const effectiveComponents = () => h3LoraComponents(inventory?.components || [], editingComponents);
  const loras = createH3LoraEditor({ idPrefix: 'seed-hunter', onChange: () => { revision++; save(); update(); },
    onManage: (selectedId, trigger) => { library?.close(); library = showH3LoraLibrary({ components: inventory?.components || [], selectedId, onChange: loadInventory }, trigger); },
    onInsertTrigger: component => { if(stage.value==='final'){fail('Refinement keeps the saved preview prompt.');return;} const profile = component.lora_profile; fields.prompt.value = insertH3LoraTrigger(fields.prompt.value, profile.trigger_words, profile.trigger_position, fields.prompt.selectionStart); changed(); fields.prompt.focus(); },
    onApplyRecommended: component => { for (const [key, value] of Object.entries(component.lora_profile?.recommended_settings || {})) if (fields[key] && (stage.value!=='final'||FINAL_FIELDS.has(key))) fields[key].value = value; changed(); },
  }); form.append(loras.node);
  const prompt = field(form, 'prompt', 'Prompt', make('textarea')); prompt.rows = 5; prompt.maxLength = 16000; prompt.required = true;
  const enhancerBar = make('div', 'h3-video-actions'), enhance = button('Enhance prompt'), restorePrompt = button('Restore original prompt'), enhancerSelect = make('select', 'cookbook-field-input'), checkModels = button('Check models'); enhancerSelect.setAttribute('aria-label', 'Enhancement model');
  enhance.dataset.seedHunterHelpAction = 'prompt_enhancement';
  enhancerBar.append(enhancerSelect, enhance, restorePrompt, checkModels); form.append(enhancerBar); const enhancerInfo=make('p','h3-video-muted');form.append(enhancerInfo); restorePrompt.hidden = true;
  const dialoguePanel = group(form, 'Dialogue prompt builder', true), dialogueText = field(dialoguePanel, 'dialogue_text', 'Line of dialogue', make('textarea'));
  const dialogueTone = field(dialoguePanel, 'dialogue_tone', 'Tone and delivery', make('textarea'));
  const dialogueTemplate = field(dialoguePanel, 'dialogue_template', 'Editable prompt template', make('textarea')); dialogueTemplate.rows = 5;
  dialogueTemplate.value = 'subject_definitions:\n<Subject 1> is a character.\n\nsummary:\n[audio reference] A character speaks to the camera.\n\nretention_analysis:\n<Audio 1>: reference - use its vocal timbre without copying its dialogue.\n\ndetailed_description:\n<Subject 1> says: <d>[English] {{dialogue}}</d> {{tone}}\n\noverall_soundscape:\nClear audible dialogue.\n\nnon_diegetic_music:\nN/A';
  const applyDialogue = button('Build visible prompt'), dialogueDefaults=button('Apply dialogue sampling defaults'); dialoguePanel.append(applyDialogue,dialogueDefaults); dialogueDefaults.onclick=()=>{fields.width.value=fields.height.value=256;fields.frames.value=158;fields.steps.value=16;fields.sampler.value='euler';fields.scheduler.value='simple';changed();}; applyDialogue.onclick = () => { prompt.value = seedHunterDialoguePrompt(dialogueText.value, dialogueTone.value, dialogueTemplate.value); changed(); };
  const dimensions = make('div', 'h3-video-grid'); form.append(dimensions);
  number(dimensions, 'width', 'Preview width', 480, 64, 4096, 32); number(dimensions, 'height', 'Preview height', 864, 64, 4096, 32);
  number(dimensions, 'frames', 'Frames at 24 fps', 73, 5, 362, 17); number(dimensions, 'steps', 'Preview steps', 6, 1, 100); number(dimensions, 'seed', 'Base seed', 42, 0, Number.MAX_SAFE_INTEGER);
  const reroll = button('Randomize base seed'); dimensions.append(reroll); reroll.onclick = () => { fields.seed.value = randomSeed(); changed(); };
  number(dimensions, 'preview_count', 'Preview count', 3, 1, 3, 1, options);
  const duration = make('p', 'h3-video-muted'); dimensions.append(duration);
  const sizeTools = make('div', 'h3-video-actions'), aspect = make('select', 'cookbook-field-input'), megapixels = make('input', 'cookbook-field-input'), applySize = button('Apply preview size');
  aspect.setAttribute('aria-label', 'Preview aspect ratio'); for (const value of ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9']) aspect.add(new Option(value, value)); aspect.value = '9:16';
  megapixels.type = 'number'; megapixels.value = '0.4'; megapixels.min = '0.1'; megapixels.max = '4'; megapixels.step = '0.1'; megapixels.setAttribute('aria-label', 'Preview megapixels'); sizeTools.append(megapixels, aspect, applySize); form.append(sizeTools);
  const seconds=make('input','cookbook-field-input'),applyDuration=button('Apply duration');seconds.type='number';seconds.min='3';seconds.max='15';seconds.step='.1';seconds.value='3';seconds.setAttribute('aria-label','Preview seconds');sizeTools.append(seconds,applyDuration);applyDuration.onclick=()=>{fields.frames.value=seedHunterFrames(seconds.value);changed();};
  applySize.onclick = () => { try { const size = seedHunterResolution(megapixels.value, ...aspect.value.split(':')); fields.width.value = size.width; fields.height.value = size.height; changed(); } catch (e) { fail(e); } };
  const advanced = group(form, 'Sampling and optional controls');
  select(advanced, 'sampler', 'Sampler', [['er_sde', 'ER-SDE'], ['euler', 'Euler'], ['res_multistep', 'RES multistep']]);
  select(advanced, 'scheduler', 'Scheduler', [['beta', 'Beta'], ['simple', 'Simple'], ['normal', 'Normal'], ['linear_quadratic', 'Linear quadratic']]);
  select(advanced, 'reference_size', 'Reference sizing', [['max', 'Maximum'], ['match', 'Match output']]);
  check(advanced, 'shift_enabled', 'Override video/audio shifts'); number(advanced, 'shift_video', 'Video shift', 12, .01, 100, .1); number(advanced, 'shift_audio', 'Audio shift', 6, .01, 100, .1);
  check(advanced, 'first_frame_guide', 'Use Picture 1 as first-frame guide'); check(advanced, 'last_frame_guide', 'Use Picture 2 as last-frame guide'); check(advanced, 'force_audio', 'Preserve supplied Audio 1 in the latent');
  select(advanced, 'output_fps', 'Output fps', [['24','24 fps'],['48','48 fps · RIFE interpolation']], options);
  select(advanced, 'attention_backend', 'Attention backend', [['comfy kitchen attention', 'Comfy Kitchen'], ['pytorch', 'PyTorch']], options);
  select(advanced, 'sparse_method', 'Sparse attention', [['none', 'Off'], ['sla', 'SLA'], ['sol_attn', 'Sol-Attn'], ['vsa', 'VSA']], options);
  number(advanced, 'sparse_keep_percent', 'Sparse attention kept (%)', 10, 1, 100, .1, options);
  number(advanced, 'sparse_start', 'Sparse attention start', .2, 0, 1, .05, options); number(advanced, 'sparse_end', 'Sparse attention end', 1, 0, 1, .05, options);
  number(advanced, 'sparse_min_tokens', 'Sparse minimum tokens', 12288, 0, 1000000, 1, options); number(advanced, 'sparse_extra_tokens', 'Sparse extra tokens', 256, 0, 8192, 1, options);
  select(advanced, 'sparse_sink_conditioning', 'Sparse reference preservation', [['exact_kv_and_rows','Preserve reference keys and rows'],['none','Off']], options);
  number(advanced, 'ff_chunk_size', 'Feed-forward chunks (0 = auto for INT8)', 0, 0, 64, 1, options); number(advanced, 'ff_chunk_threshold', 'Feed-forward chunk threshold', 2048, 1, 1000000, 1, options);
  number(advanced, 'low_vram_chunks', 'Attention chunks (0 = off)', 0, 0, 64, 1, options);
  number(advanced, 'denoise', 'Default refinement denoise', .4, 0, 1, .05, options);
  check(advanced, 'upscaler_chunking', 'Temporal chunking for latent upscale', true); check(advanced, 'upscaler_force_unload', 'Unload latent upscaler after use');
  const continuationPanel = group(form, 'Continuation source and timing', true);
  select(continuationPanel, 'continuation_overlap', 'Overlap frames', [5,22,39,56].map(n => [String(n), String(n)]), options);
  number(continuationPanel, 'source_start_seconds', 'Source start (seconds)', 0, 0, 86400, .01, options); number(continuationPanel, 'source_end_seconds', 'Source end (blank = end)', '', 0, 86400, .01, options);
  const audioPanel = group(form, 'Voice reference timing'); number(audioPanel, 'audio_start_seconds', 'Audio start (seconds)', 0, 0, 86400, .01, options); number(audioPanel, 'audio_end_seconds', 'Audio end (blank = end)', '', 0, 86400, .01, options);
  const batchContainer=make('div');form.append(batchContainer);
  const media = make('div', 'h3-video-inputs'); form.append(media);
  function upload(key, label, accept, multiple, limit) {
    const wrap = make('section', 'h3-video-upload'), title = make('label', 'h3-video-upload-label', label), input = make('input'); input.type = 'file'; input.accept = accept; input.multiple = multiple; input.id = `seed-hunter-input-${key}`; title.htmlFor = input.id;
    const list = make('ul', 'h3-video-file-list'), server = accept === 'video/*' ? button('Server files') : null;
    const state = { files: [], retained: [], strengths:[], rangeEditors:[], wrap, input, server, limit, render: () => {
      state.rangeEditors.forEach(editor=>editor.destroy());state.rangeEditors=[];
      list.replaceChildren(); for (const [i, file] of [...state.retained, ...state.files].entries()) { const row = make('li'), remove = button('Remove'); remove.setAttribute('aria-label', `Remove ${file.name}`); remove.onclick = () => { state.strengths.splice(i,1); if (i < state.retained.length) state.retained.splice(i, 1); else state.files.splice(i - state.retained.length, 1); state.render(); changed(); }; row.append(make('span', '', `${file.name}${i < state.retained.length ? ' · saved on server' : file.serverFile ? ` · ${file.serverFile.root.toUpperCase()}:` : ''}`), remove); if(key==='reference_mods'){const strength=make('input','cookbook-field-input');strength.type='number';strength.min=0;strength.max=1;strength.step=.05;strength.value=state.strengths[i]??1;strength.setAttribute('aria-label',`Strength for ${file.name}`);strength.oninput=()=>{state.strengths[i]=Number(strength.value);changed();};row.append(strength);} if(key==='reference_videos'){row.classList.add('h3-reference-video-row');const editor=createReferenceVideoRangeEditor(file,changed);state.rangeEditors.push(editor);row.append(editor.node);} list.append(row); }
    } }; uploads[key] = state;
    const add = files => { if(input.disabled) throw new Error('This input is unavailable for the current job or runtime.'); const next = multiple ? [...state.files, ...files] : files.slice(0, 1); if (next.length + (multiple ? state.retained.length : 0) > limit) throw new Error(`Choose at most ${limit} ${label.toLowerCase()}.`); if (!multiple) state.retained = []; state.files = next; state.render(); changed(); };
    input.onchange = () => { try { add([...input.files]); } catch (e) { fail(e); } input.value = ''; };
    if (server) server.onclick = () => { picker?.close(); picker = showVideoServerFiles({ family: 'h3', multiple, onSelect: add }, server); };
    wrap.addEventListener('dragover', event => { event.preventDefault(); }); wrap.addEventListener('drop', event => { event.preventDefault(); try { add([...event.dataTransfer.files]); } catch (e) { fail(e); } });
    wrap.append(title, input); if (server) wrap.append(server); wrap.append(list); if(key==='reference_videos')wrap.append(make('p','h3-video-muted','Choose any 2–15 second section of a longer video. Across multiple references, selected sections must total at most 15 seconds. Choose a generation length at least as long as the section to use all of it.')); media.append(wrap); return state;
  }
  upload('source_video', 'Continuation source video', 'video/*', false, 1);
  upload('first_frame','First-frame guide (optional; otherwise Picture 1)','image/*',false,1);upload('last_frame','Last-frame guide (optional; otherwise Picture 2)','image/*',false,1);
  upload('reference_images', 'Reference pictures (up to 9)', 'image/*', true, 9); upload('reference_videos', 'Reference videos (up to 3)', 'video/*', true, 3); upload('reference_audio', 'Reference audio (up to 3)', 'audio/*', true, 3);
  upload('reference_mods', 'Reference mod bundles (optional, up to 8)', '.safetensors', true, 8);
  const actions = make('div', 'h3-video-actions'), submit = button('Generate previews'); submit.type = 'submit'; submit.className = 'cookbook-btn'; actions.append(submit); form.append(actions);
  const queueContainer = make('div'); scroll.append(queueContainer);
  const finalSection = make('section', 'h3-seed-hunter-final'); finalSection.hidden = true; const finalTitle = make('h3'), finalDescription = make('p', 'h3-video-muted'), finalForm = make('form'), finalGrid = make('div', 'h3-video-grid');
  finalForm.append(finalGrid); finalSection.append(finalTitle, finalDescription, finalForm); scroll.append(finalSection);
  select(finalGrid, 'latent_upscaler', 'Latent upscaler', [], finalFields); number(finalGrid, 'width', 'Final width', 1088, 64, 4096, 32, finalFields); number(finalGrid, 'height', 'Final height', 1952, 64, 4096, 32, finalFields);
  number(finalGrid, 'steps', 'Refinement steps', 2, 1, 100, 1, finalFields); number(finalGrid, 'seed', 'Refinement seed', 42, 0, Number.MAX_SAFE_INTEGER, 1, finalFields); number(finalGrid, 'denoise', 'Denoise', .4, 0, 1, .05, finalFields);
  select(finalGrid, 'sampler', 'Refinement sampler', [['er_sde','ER-SDE'],['euler','Euler'],['res_multistep','RES multistep']], finalFields); select(finalGrid, 'scheduler', 'Refinement scheduler', [['linear_quadratic','Linear quadratic'],['beta','Beta'],['simple','Simple'],['normal','Normal']], finalFields);
  const finalLoras = createH3LoraEditor({ idPrefix: 'seed-hunter-final', onChange: () => { finalRequest = null; save(); } }); finalForm.append(make('p', 'h3-video-muted', 'This stack applies only to refinement. It starts with the preview’s saved LoRAs.'), finalLoras.node);
  const finalActions = make('div', 'h3-video-actions'), finalSubmit = button('Render selected preview'), finalReroll = button('Randomize refinement seed'), cancelSelection = button('Clear selection'); finalSubmit.dataset.seedHunterHelpAction = 'refine_preview'; finalSubmit.type = 'submit'; finalSubmit.className = 'cookbook-btn'; finalActions.append(finalSubmit, finalReroll, cancelSelection); finalForm.append(finalActions);
  finalReroll.onclick = () => { finalFields.seed.value = randomSeed(); finalRequest = null; save(); }; cancelSelection.onclick = () => { selecting++; selection = null; finalRequest = null; finalSection.hidden = true; save(); renderJobs(); };
  finalForm.addEventListener('input', () => { finalRequest = null; save(); }); finalForm.addEventListener('change', () => { finalRequest = null; save(); });
  const jobToolbar = make('div', 'h3-video-toolbar'), refreshJobs = button('Refresh jobs'); jobToolbar.append(make('h3', '', 'Seed Hunter jobs'), refreshJobs); const jobStatus = make('p', 'h3-video-muted'), jobList = make('div', 'h3-video-jobs'); scroll.append(jobToolbar, jobStatus, jobList);
  const disposeHelp = bindSeedHunterHelp(overlay);
  function fail(reason) { error.textContent = reason?.message || String(reason || ''); error.hidden = !error.textContent; }
  function readOptions() { return {...optionSnapshot, refmod_strengths:uploads.reference_mods?[...uploads.reference_mods.retained,...uploads.reference_mods.files].map((_,i)=>uploads.reference_mods.strengths[i]??1):[], ...Object.fromEntries(Object.keys(options).map(key => { const n = options[key]; return [key, n.type === 'checkbox' ? n.checked : n.type === 'number' || ['continuation_overlap','output_fps'].includes(key) ? (n.value === '' ? null : Number(n.value)) : n.value]; }))}; }
  function config() {
    const value = seedHunterConfig(Object.fromEntries([...OUTER, 'prompt'].map(key => [key, fields[key].value])), readOptions(), loras.getValue());
    // A final job already owns its conditioning. Preserve even the absence of
    // legacy range metadata so editing steps cannot alter its source contract.
    if (stage.value === 'final') return { ...value, ...restoredRangeConfig };
    return { ...value, reference_video_ranges: referenceVideoRanges(uploads.reference_videos) };
  }
  function currentUploads() { return Object.fromEntries(Object.entries(uploads).filter(([key]) => (key !== 'source_video' || stage.value === 'continuation') && (key !== 'first_frame' || options.first_frame_guide.checked) && (key !== 'last_frame' || options.last_frame_guide.checked)).map(([key, state]) => [key, [...state.files]])); }
  function finalConfig() { return { ...Object.fromEntries(Object.entries(finalFields).map(([key, n]) => [key, n.type === 'number' ? Number(n.value) : n.value])), loras: finalLoras.getValue() }; }
  function save() { if (jobEditor?.active) return; try { localStorage.setItem(STORAGE, JSON.stringify({ config: config(), dialogue_text: dialogueText.value, dialogue_tone: dialogueTone.value, dialogue_template: dialogueTemplate.value, enhancer: enhancer[Number(enhancerSelect.value)] ? {model:enhancer[Number(enhancerSelect.value)].model,endpoint_id:enhancer[Number(enhancerSelect.value)].endpoint_id} : null, selected: selection && { job_id: selection.job_id, index: selection.index }, final_config: finalConfig(), final_request: finalRequest })); } catch {} }
  function changed() { revision++; error.hidden = true; update(); save(); }
  function restore(values, inputFiles = {}, retained = {}) {
    restoredRangeConfig = Object.prototype.hasOwnProperty.call(values, 'reference_video_ranges') ? {reference_video_ranges: clone(values.reference_video_ranges)} : {};
    optionSnapshot = clone(values.seed_hunter || {});
    for(const role of ['model','encoder','video_vae','audio_vae','latent_upscaler']) fill(fields[role],(inventory?.components||[]).filter(c=>c.role===role&&(role!=='model'||c.variant==='ref2va')),values[role]??fields[role].value);
    for (const key of OUTER) if (values[key] !== undefined) fields[key].value = values[key]; prompt.value = values.prompt || '';
    for (const key of Object.keys(options)) { const n = options[key], value = values.seed_hunter?.[key] ?? OPTIONS[key] ?? n.value; if (n.type === 'checkbox') n.checked = value; else n.value = value ?? ''; }
    loras.setInventory(effectiveComponents()); loras.setValue(h3LoraStack(values));
    for (const [key, state] of Object.entries(uploads)) { state.files = [...(inputFiles[key] || [])]; state.retained = [...(retained[key] || [])]; state.strengths=key==='reference_mods'?[...(values.seed_hunter?.refmod_strengths||[])]:[]; if(key==='reference_videos')restoreReferenceVideoRanges(state,values.reference_video_ranges); state.render(); }
    revision++; update();
  }
  function batchIssue() {
    if(!inventory?.seed_hunter||!inventory.runtime_ready)return 'Seed Hunter components are not ready.';
    if(!seedHunterBatchChoices(stage.value).some(choice=>choice.field===batchQueue?.inputField))return 'Choose a supported batch input.';
    if(!['model','encoder','video_vae','audio_vae','gpu'].every(key=>fields[key].value))return 'Choose the models, VAEs and GPU first.';
    if(!prompt.value.trim())return 'Enter a prompt to use for every job.';
    const issue=h3LoraIssue(loras.getValue(),effectiveComponents(),'ref2va');if(issue)return issue;
    const selected=batchQueue?.inputField,counts=Object.fromEntries(['reference_images','reference_videos','reference_audio'].map(key=>[key,key===selected?1:uploads[key].files.length]));
    if(counts.reference_images>9||counts.reference_videos>3||counts.reference_audio>3||Object.values(counts).reduce((a,b)=>a+b,0)>12)return 'Too many shared references for each job.';
    if(!form.checkValidity())return 'Check the draft settings before queueing.';
    return '';
  }
  function batchSnapshot() {
    if(!form.reportValidity())throw new Error('Check the draft settings before queueing.');
    const issue=batchIssue();if(issue)throw new Error(issue);
    const field=batchQueue.inputField;
    return {config:seedHunterBatchConfig(config(),batchQueue.autoVideoLength),videoField:field,autoVideoLength:batchQueue.autoVideoLength,
      uploads:Object.fromEntries(Object.entries(currentUploads()).filter(([key])=>key!==field))};
  }
  function update() {
    batchQueue?.setInputChoices(seedHunterBatchChoices(stage.value));
    const batchBusy=!!batchQueue?.busy, batching=!!batchQueue?.active&&!jobEditor?.active&&stage.value!=='final',locked=busy||batchBusy;
    const finalEdit = stage.value === 'final', single = ['single', 'continuation','final'].includes(stage.value);
    fields.latent_upscaler.parentElement.hidden = !finalEdit && stage.value !== 'preview';
    for(const key of [...OUTER,'prompt']) if(!['shift_video','shift_audio'].includes(key)) fields[key].disabled = locked || enhancing || (finalEdit && !FINAL_FIELDS.has(key));
    for(const key of ['reference_images','reference_videos','reference_audio'])uploads[key].wrap.hidden=false;
    media.hidden = finalEdit; sizeTools.hidden = finalEdit; applyDialogue.disabled = finalEdit; reroll.disabled = locked;
    for(const state of Object.values(uploads)) {state.input.disabled = finalEdit || locked; if(state.server)state.server.disabled = finalEdit || locked;state.rangeEditors.forEach(editor=>editor.setDisabled(finalEdit||locked));}
    for(const key of Object.keys(options)) options[key].disabled = locked || (finalEdit && !FINAL_OPTIONS.has(key));
    const features = inventory?.seed_hunter?.features || {};
    stage.disabled = !!jobEditor?.active || locked;
    for (const item of stage.options) item.hidden = item.value === 'final' && !finalEdit;
    for (const item of stage.options) item.disabled = (item.value === 'final' && !finalEdit) || ['continuation', 'dialogue'].includes(item.value) && features[item.value] !== true;
    for (const [feature, keys] of Object.entries({ guides: ['first_frame_guide', 'last_frame_guide'], force_audio: ['force_audio'], attention_backend: ['attention_backend'], sparse_attention: ['sparse_method','sparse_keep_percent','sparse_start','sparse_end','sparse_min_tokens','sparse_extra_tokens','sparse_sink_conditioning'], ff_chunking: ['ff_chunk_size','ff_chunk_threshold'], low_vram_attention:['low_vram_chunks'], rife: ['output_fps'], latent_upscaler: ['upscaler_chunking', 'upscaler_force_unload'] })) {
      for (const key of keys) { options[key].disabled ||= features[feature] !== true; options[key].title = features[feature] === true ? '' : 'Unavailable in the installed Seed Hunter runtime.'; }
    }
    uploads.reference_mods.input.disabled ||= features.refmods !== true;
    uploads.first_frame.wrap.hidden = !options.first_frame_guide.checked; uploads.last_frame.wrap.hidden = !options.last_frame_guide.checked;
    uploads.reference_mods.wrap.title = features.refmods === true ? '' : 'Reference mods are unavailable in the installed Seed Hunter runtime.';
    options.preview_count.disabled = single || locked; options.preview_count.parentElement.hidden = single;
    dialoguePanel.parentElement.hidden = stage.value !== 'dialogue'; audioPanel.parentElement.hidden = false; continuationPanel.parentElement.hidden = stage.value !== 'continuation'; uploads.source_video.wrap.hidden = stage.value !== 'continuation';
    fields.seed.max = Number.MAX_SAFE_INTEGER - 1000 * ((single ? 1 : Number(options.preview_count.value)||1)-1);
    duration.textContent = `${(Number(fields.frames.value) / 24).toFixed(2)} seconds · seeds ${Array.from({ length: single ? 1 : Number(options.preview_count.value) || 1 }, (_, i) => Number(fields.seed.value) + i * 1000).join(', ')}`;
    fields.shift_video.disabled = fields.shift_audio.disabled = locked || !options.shift_enabled.checked;
    fields.frames.disabled ||= batching&&batchQueue.autoVideoLength;
    if(batching&&batchQueue.autoVideoLength)duration.textContent='Automatic: each batch video gets its own generation length after detection.';
    if(batching&&uploads[batchQueue.inputField]){const state=uploads[batchQueue.inputField];state.wrap.hidden=true;state.input.disabled=true;state.rangeEditors.forEach(editor=>editor.setDisabled(true));}
    fields.frames.min = stage.value === 'continuation' ? '5' : '73'; fields.width.min = fields.height.min = stage.value === 'dialogue' ? '64' : '256';
    loras.setDisabled(locked || loading || enhancing); loras.setLibraryAvailable(inventory?.lora_library === true && !jobEditor?.active);
    submit.disabled = locked || loading || enhancing || !inventory?.seed_hunter || !inventory?.runtime_ready || !!jobEditor?.blocked || (['continuation','dialogue'].includes(stage.value) && features[stage.value] !== true);
    submit.hidden=batching;
    submit.textContent = locked ? 'Submitting…' : jobEditor?.active ? 'Save queued job' : stage.value === 'preview' ? 'Generate previews' : stage.value === 'dialogue' ? 'Generate audio previews' : stage.value === 'continuation' ? 'Queue continuation' : 'Queue single pass';
    refresh.disabled = locked || loading; exportButton.disabled = importButton.disabled = locked || loading || !inventory || !!jobEditor?.active;
    const enhancerInputs=seedHunterEnhancerInputs(uploads,readOptions());enhancerInfo.textContent=enhancerInputs.issue;enhancerInfo.hidden=!enhancerInputs.issue;
    enhance.disabled = finalEdit || locked || enhancing || !enhancerSelect.value || !!enhancerInputs.issue; restorePrompt.hidden = previousPrompt === null;
    batchQueue?.setAvailable(!finalEdit&&!jobEditor?.active&&!jobEditor?.loading);
    batchQueue?.setServerFilesAvailable(inventory?.server_video_files===true);
    const batchBlock=busy||loading||enhancing||finalBusy||!!jobEditor?.active||!!jobEditor?.loading||!inventory?.runtime_ready;
    const batchReason=batchBlock?'Finish the current operation before adding a batch.':batchIssue();
    batchQueue?.setEnabled(!batchReason,batchReason,{snapshotEnabled:!batchBlock,snapshotReason:batchReason});
    queue?.setBlocked(locked || loading || finalBusy || !!jobEditor?.active); finalSubmit.disabled = finalBusy || !selection || !finalFields.latent_upscaler.value || features.latent_upscaler !== true; finalLoras.setDisabled(finalBusy); for(const n of Object.values(finalFields))n.disabled=finalBusy;finalReroll.disabled=cancelSelection.disabled=finalBusy;
  }
  function fill(n, items, selected, blank = false) { n.replaceChildren(); if (blank) n.add(new Option('Use model GPU', '')); for (const item of items) n.add(new Option(displayName(item), item.id)); if(selected!==undefined&&selected!==null){if(![...n.options].some(o=>o.value===String(selected)))n.add(new Option('Choose an available component',selected));n.value=selected;} }
  async function loadInventory() {
    if (closed) return; loading = true; update();
    try {
      const data = await request('/inventory'); if (closed) return;
      const first = !inventory, before = first ? { ...BASE, ...data.seed_hunter?.defaults, ...(saved.config || {}), seed_hunter: { ...OPTIONS, ...(data.seed_hunter?.defaults?.seed_hunter || {}), ...(saved.config?.seed_hunter || {}) } } : config(); inventory = data;
      optionSnapshot = clone(before.seed_hunter || {});
      if(first && !saved.config){const authored=data.components.find(c=>c.role==='model'&&c.name==='Minimax-h3_Singularity_ref2va_Pruned_v1.3_int8.safetensors');if(authored)before.model=authored.id;}
      for (const role of ['model','encoder','video_vae','audio_vae','latent_upscaler']) fill(fields[role], (data.components || []).filter(c => c.role === role && (role !== 'model' || c.variant === 'ref2va')), before[role]);
      fill(fields.gpu, data.gpus || [], before.gpu || data.gpus?.find(g => /5090/.test(g.name))?.id); fill(fields.vae_gpu, data.gpus || [], before.vae_gpu, true);
      for (const key of OUTER.filter(key => !['model','encoder','video_vae','audio_vae','latent_upscaler','gpu','vae_gpu'].includes(key))) if (before[key] !== undefined) fields[key].value = before[key];
      for (const key of Object.keys(options)) { const n = options[key], value = before.seed_hunter?.[key] ?? OPTIONS[key] ?? n.value; if (n.type === 'checkbox') n.checked = value; else n.value = value ?? ''; }
      if(first && !saved.config){const authored = data.components.find(c=>c.role==='lora'&&c.name==='minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors');if(authored)before.loras=[{id:authored.id,strength:1}];}
      loras.setInventory(effectiveComponents(), data.max_loras || 8); loras.setValue(h3LoraStack(before));
      fill(finalFields.latent_upscaler, data.components.filter(c => c.role === 'latent_upscaler'), finalFields.latent_upscaler.value || saved.final_config?.latent_upscaler);
      if (first) { prompt.value = before.prompt || ''; for (const key of ['dialogue_text','dialogue_tone','dialogue_template']) if (saved[key] !== undefined) fields[key].value = saved[key]; }
      status.textContent = !data.seed_hunter ? 'Seed Hunter needs the updated Odysseus server.' : !data.runtime_ready ? data.runtime_error || 'H3 runtime unavailable.' : 'Seed Hunter is ready. Preview and refinement are separate submissions.';
    } catch (e) { fail(e); } finally { loading = false; if (!closed) update(); }
  }
  form.addEventListener('input', changed); form.addEventListener('change', changed);
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (batchQueue?.active&&!jobEditor?.active || submit.disabled || !form.reportValidity()) return;
    const values = config(), issue = h3LoraIssue(values.loras, effectiveComponents(), values.mode);
    if (issue) { fail(issue); return; }
    if (!values.prompt.trim()) { fail('Enter a prompt, or build one with the dialogue controls.'); return; }
    if ((values.frames - 5) % 17) { fail('Frame count must be 17 × n + 5, such as 73, 90, 107 or 124.'); return; }
    const editedJobId=jobEditor.active?.id;
    busy = true; update(); error.hidden = true;
    try {
      if (jobEditor.active) {
        const body = videoJobEditFormData(jobEditor.active, values, currentUploads(), Object.fromEntries(Object.entries(uploads).map(([key,s]) => [key,s.retained])));
        try { await saveSeedHunterQueuedJob(request, jobEditor.active.id, body); jobEditor.finish(); }
        catch (e) { jobEditor.failed(e); return; }
      }
      else { const body = new FormData(); body.append('config', JSON.stringify(values)); appendVideoInputs(body, currentUploads()); await request('/jobs', { method: 'POST', body }); }
      if (!closed) { status.textContent = editedJobId ? 'Changes saved to the queued job. Its queue position is unchanged, and your previous draft has been restored.' : 'Job added to the shared video queue. Your draft remains available.'; await loadJobs(); }
    } catch (e) { fail(e); } finally { busy = false; if (!closed) update(); }
  });
  async function selectCandidate(job, candidate, remembered = false) {
    if (busy || finalBusy || !candidate.can_refine) return;
    const token = ++selecting;
    try {
      status.textContent = 'Loading saved preview settings…'; const data = await request(`/jobs/${encodeURIComponent(job.id)}/seed-hunter`); if (closed || token !== selecting) return;
      const source = data.config, size = seedHunterResolution(inventory.seed_hunter?.final_defaults?.megapixels || 2, source.width, source.height);
      selection = { job_id: job.id, index: candidate.index, seed: candidate.seed, width:source.width, height:source.height }; finalRequest = remembered ? saved.final_request || null : null;
      const defaults = seedHunterRefinementDefaults(source, { ...size, latent_upscaler:source.latent_upscaler||finalFields.latent_upscaler.value, steps: 2, seed: randomSeed(), sampler: 'er_sde', scheduler: 'linear_quadratic', denoise: .4, ...(inventory.seed_hunter?.final_defaults || {}) }, remembered ? saved.final_config || {} : {});
      for (const [key, n] of Object.entries(finalFields)) if (defaults[key] !== undefined) n.value = defaults[key];
      finalComponents = h3LoraComponents(inventory.components, data.lora_components); finalLoras.setInventory(finalComponents); finalLoras.setValue(remembered && saved.final_config?.loras ? saved.final_config.loras : h3LoraStack(source));
      finalTitle.textContent = `Refine preview ${candidate.index} · seed ${candidate.seed}`; finalDescription.textContent = `Saved job ${job.id}. Reuses this preview’s latent and inputs; the draft above is separate.`; finalSection.hidden = false;
      status.textContent = 'Preview selected. Review refinement settings before rendering.'; update(); save(); renderJobs(); if (!remembered) finalSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) { fail(e); }
  }
  finalForm.onsubmit = async event => {
    event.preventDefault(); if (finalSubmit.disabled || !selection || !finalForm.reportValidity()) return;
    const issue = h3LoraIssue(finalLoras.getValue(), finalComponents, 'ref2va'); if(issue){fail(issue);return;}
    const values=finalConfig();
    if(values.width<selection.width||values.height<selection.height||values.width*values.height>4194304){fail('Final dimensions must be at least the selected preview size, up to 4,194,304 pixels.');return;}
    const current = { candidate: selection.index, config: values };
    if (!finalRequest || finalRequest.source !== selection.job_id || JSON.stringify(finalRequest.payload) !== JSON.stringify(current)) finalRequest = { source: selection.job_id, payload: clone(current), request_id: crypto.randomUUID() };
    save(); finalBusy = true; update(); error.hidden = true;
    try { const result=await request(`/jobs/${encodeURIComponent(finalRequest.source)}/seed-hunter/finalize`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...finalRequest.payload, request_id: finalRequest.request_id }) }); if(!result.id&&!result.job?.id)throw new Error('The server did not confirm this refinement.'); finalRequest = null; save(); status.textContent = 'Refinement queued. The saved preview remains available for another variation.'; await loadJobs(); }
    catch (e) { fail(`${e.message} Retry keeps the same submission ID.`); }
    finally { finalBusy = false; if (!closed) update(); }
  };
  const reruns = createVideoJobReruns({ family: 'h3', request, onChange: () => renderJobs(), onQueued: loadJobs, isClosed: () => closed, isBlocked: () => busy || finalBusy });
  async function stopJob(job) { try { await request(`/jobs/${job.id}`, { method: 'DELETE' }); await loadJobs(); } catch (e) { fail(e); } }
  function renderJobs() {
    jobStatus.textContent = `${jobs.filter(j => j.status === 'running').length} rendering · ${jobs.filter(j => j.status === 'queued').length} queued · saved previews stay available after closing this panel.`;
    for (const [id, node] of jobNodes) if (!jobs.some(j => j.id === id)) { node.card.remove(); jobNodes.delete(id); }
    for (const job of jobs) {
      let nodes = jobNodes.get(job.id);
      if (!nodes) {
        const card = make('article', 'h3-video-job'), heading = make('h4'), line = make('p', 'h3-video-muted'), tools = make('div', 'h3-video-job-actions'), stop = button('Stop'), retry = button('Retry'), edit = button('Edit queued job'), editRerun = button('Edit & rerun'), remove = button('Delete job');
        const jobError = make('p', 'h3-video-error'), candidates = make('div', 'h3-seed-hunter-candidates'), output = make('div', 'h3-video-output'), details = make('details'), log = make('pre', 'h3-video-log'), promptDetails = make('details'), usedPrompt = make('pre', 'h3-video-prompt-used');
        details.append(make('summary','','Runtime log'), log); promptDetails.append(make('summary','','Prompt used'),usedPrompt); tools.append(stop,retry,edit,editRerun,remove); card.append(heading,line,tools,jobError,candidates,output,promptDetails,details); jobList.append(card);
        const deletion = make('div','h3-video-delete-confirmation'), keep = button('Keep job'), confirm = button('Delete permanently'); deletion.hidden = true; deletion.append(make('p','','Delete this job and its saved previews, latents and outputs?'),keep,confirm); card.append(deletion);
        nodes = {card,heading,line,stop,retry,edit,editRerun,remove,jobError,candidates,output,log,usedPrompt,previews:new Map(),deletion}; jobNodes.set(job.id,nodes);
        stop.onclick = () => stopJob(job); retry.onclick = () => reruns.run(jobs.find(j => j.id===job.id)||job); edit.onclick = () => jobEditor.open(job.id); editRerun.onclick = () => queue.openRerun(job.id,async(source,patch)=>{const result=await reruns.run(source,patch,true);return {result,...reruns.state(source)};});
        remove.onclick=()=>{deletion.hidden=false;};keep.onclick=()=>{deletion.hidden=true;};confirm.onclick=async()=>{confirm.disabled=true;try{const result=await request(`/jobs/${job.id}/record`,{method:'DELETE'});if(!result.deleted)throw new Error('Deletion was not confirmed.');if(selection?.job_id===job.id){selection=null;finalSection.hidden=true;save();}await loadJobs();}catch(e){fail(e);}finally{confirm.disabled=false;}};
      }
      const stageName=job.seed_hunter?.stage||'preview'; nodes.heading.textContent=`${stageName} · ${job.status} · ${job.id}`;nodes.line.textContent=[job.phase,job.total_steps?`Step ${job.step||0}/${job.total_steps}`:''].filter(Boolean).join(' · ');
      const state=reruns.state(job);nodes.retry.hidden=nodes.editRerun.hidden=state.hidden;nodes.retry.disabled=nodes.editRerun.disabled=state.disabled;nodes.retry.textContent=state.label;nodes.edit.hidden=job.status!=='queued';nodes.stop.hidden=!ACTIVE.has(job.status);nodes.remove.hidden=!TERMINAL.has(job.status);
      nodes.jobError.textContent=job.error||state.error||'';nodes.jobError.hidden=!nodes.jobError.textContent;nodes.log.textContent=(job.log_tail||[]).join?.('\n')||job.log_tail||'';if(nodes.usedPrompt.textContent!==job.prompt)nodes.usedPrompt.textContent=job.prompt||'Prompt unavailable.';
      for(const candidate of job.seed_hunter?.candidates||[]){let c=nodes.previews.get(candidate.index);if(!c){const card=make('section','h3-seed-hunter-candidate'),label=make('h5'),video=make('video'),choose=button('Use this preview'),download=make('a','memory-toolbar-btn','Download preview');video.controls=true;video.playsInline=true;video.preload='metadata';const url=`${API}/jobs/${encodeURIComponent(job.id)}/seed-hunter/candidates/${candidate.index}/video`;video.src=download.href=url;download.download=`h3-preview-${candidate.index}.mp4`;card.append(label,video,choose,download);nodes.candidates.append(card);c={card,label,video,choose};nodes.previews.set(candidate.index,c);choose.onclick=()=>selectCandidate(jobs.find(j=>j.id===job.id)||job,(jobs.find(j=>j.id===job.id)?.seed_hunter?.candidates||[]).find(p=>p.index===candidate.index)||candidate);}
        if(candidate.audio_url&&!c.audio){const audio=make('audio'),link=make('a','memory-toolbar-btn','Download audio');audio.controls=true;audio.preload='none';audio.src=link.href=`${API}/jobs/${encodeURIComponent(job.id)}/seed-hunter/candidates/${candidate.index}/audio`;link.download=`h3-dialogue-${candidate.index}.flac`;c.card.append(audio,link);c.audio=audio;}
        c.label.textContent=`Preview ${candidate.index} · seed ${candidate.seed}`;c.choose.hidden=!candidate.can_refine;c.choose.disabled=finalBusy;c.card.classList.toggle('selected',selection?.job_id===job.id&&selection.index===candidate.index);c.choose.textContent=selection?.job_id===job.id&&selection.index===candidate.index?'Selected preview':'Use this preview';}
      if(job.status==='completed'&&stageName!=='preview'&&!(job.seed_hunter?.candidates?.length)&&!nodes.output.childElementCount){const video=make('video');video.controls=true;video.playsInline=true;video.preload='metadata';video.src=`${API}/jobs/${job.id}/video`;nodes.output.append(video);}
    }
  }
  async function loadJobs() { if(polling||closed)return;polling=true;try{const data=await request('/jobs');if(closed)return;jobs=(data.jobs||[]).filter(j=>j.seed_hunter||j.recipe==='seed_hunter');queue?.update(data.queue);jobEditor?.observe(jobs);renderJobs();if(saved.selected&&!selection&&inventory){const job=jobs.find(j=>j.id===saved.selected.job_id),candidate=job?.seed_hunter?.candidates?.find(c=>c.index===saved.selected.index);const remembered=saved.selected;saved.selected=null;if(candidate?.can_refine)await selectCandidate(job,candidate,true);else if(remembered)save();}}catch(e){if(!closed)jobStatus.textContent=e.message;}finally{polling=false;}}
  refresh.onclick=loadInventory;refreshJobs.onclick=loadJobs;
  jobEditor=createVideoJobEditor({form,request,isClosed:()=>closed,isBusy:()=>busy||loading||finalBusy||!!batchQueue?.busy,getDraft:()=>({config:config(),uploads:Object.fromEntries(Object.entries(uploads).map(([key,s])=>[key,[...s.files]]))}),applyEdit:value=>{editingComponents=value.lora_components||null;restore(value.config,{},value.inputs);},restoreDraft:value=>{editingComponents=null;restore(value.config,value.uploads);},onChange:update,onError:fail});
  batchQueue=createVideoBatchQueue({family:'h3',recipe:'seed_hunter',sessionKey:'h3:seed_hunter',container:batchContainer,getSnapshot:batchSnapshot,onJob:job=>{if(!closed){jobs=[job,...jobs.filter(item=>item.id!==job.id)];renderJobs();}},onChange:()=>{if(!closed){revision++;update();}}});
  function nestedEditor(onChange, savedConfig) {
    const node=make('div','h3-video-grid'), rows=[];
    const allowed=savedConfig?.seed_hunter?.stage==='final'||!savedConfig ? FINAL_OPTIONS : new Set(Object.keys(options).filter(k=>k!=='stage'));
    for(const key of allowed){const original=options[key];if(!original)continue;const wrap=make('label','h3-video-field'), enabled=make('input'), control=original.cloneNode(true);enabled.type='checkbox';enabled.setAttribute('aria-label',`Change ${key.replaceAll('_',' ')}`);control.removeAttribute('id');control.value=original.value;control.checked=original.checked;control.disabled=true;control.setAttribute('aria-label',`New ${key.replaceAll('_',' ')}`);wrap.append(make('span','',key.replaceAll('_',' ')),enabled,control);node.append(wrap);const row={key,enabled,control,unavailable:!!original.title};rows.push(row);enabled.onchange=()=>{control.disabled=!enabled.checked;onChange();};control.oninput=control.onchange=()=>{enabled.checked=true;onChange();};}
    return {node,setDisabled(value){for(const r of rows){r.enabled.disabled=value||r.unavailable;r.control.disabled=value||r.unavailable||!r.enabled.checked;}},setValue(value){for(const r of rows)if(value[r.key]!==undefined){if(r.control.type==='checkbox')r.control.checked=value[r.key];else r.control.value=value[r.key]??'';}},getValue(){return Object.fromEntries(rows.filter(r=>r.enabled.checked).map(r=>[r.key,r.control.type==='checkbox'?r.control.checked:r.control.type==='number'||['continuation_overlap','output_fps'].includes(r.key)?r.control.value===''?null:Number(r.control.value):r.control.value]));}};
  }
  queue=createVideoQueueControls({container:queueContainer,family:'h3',request,reload:loadJobs,isClosed:()=>closed,onChange:update,filterJobs:job=>job.recipe==='seed_hunter'||!!job.seed_hunter,targetName:'Seed Hunter',getFields:(savedConfig,savedJob)=>{
    const final=savedConfig?.seed_hunter?.stage==='final', shared=final||!savedConfig;
    const keys=OUTER.filter(k=>k!=='mode'&&(shared?FINAL_FIELDS.has(k):k!=='latent_upscaler'));
    return keys.map(key=>videoQueueField(key,key.replaceAll('_',' '),fields[key],NUMERIC.has(key))).concat(shared?[]:[videoQueueField('prompt','Prompt',fields.prompt,false)]).concat(!shared&&savedConfig?.reference_video_ranges?.length?[referenceVideoRangesField(savedConfig.reference_video_ranges)]:[]).concat([
      {key:'seed_hunter',label:'Seed Hunter settings',create:onChange=>nestedEditor(onChange,savedConfig)},
      {key:'loras',label:'LoRA stack',getSavedValue:h3LoraStack,create:onChange=>createH3LoraEditor({components:h3LoraComponents(inventory?.components||[],savedJob?.lora_components),value:loras.getValue(),idPrefix:'seed-hunter-queue',onChange})}]);
  }});
  exportButton.onclick=()=>showVideoWorkflowExportOptions(settings=>videoWorkflow.exportVideoWorkflow({family:'h3',config:config(),uploads:currentUploads(),...settings}),exportButton);
  importButton.onclick=()=>importInput.click();importInput.onchange=async()=>{const file=importInput.files?.[0];importInput.value='';if(!file||busy)return;const captured=revision;busy=true;update();try{const result=await videoWorkflow.importVideoWorkflow(file,{family:'h3',inventory});if(closed)return;if(captured!==revision)throw new Error('The draft changed while importing. Import again to keep your edits.');if(result.config.recipe!=='seed_hunter')throw new Error('Choose a Seed Hunter workflow for this panel.');inventory=result.inventory||inventory;restore(result.config,result.uploads);save();status.textContent=['Seed Hunter workflow imported. Review settings and inputs before submitting.',...(result.warnings||[])].join(' ');}catch(e){fail(e);}finally{busy=false;if(!closed)update();}};
  async function checkEnhancer(){try{const data=await request('/prompt-enhancer');if(closed)return;const before=enhancer[Number(enhancerSelect.value)] || saved.enhancer || data.default;enhancer=data.models||[];enhancerSelect.replaceChildren(new Option('Choose enhancement model…',''),...enhancer.map((m,i)=>new Option(m.label||m.model,String(i))));enhancerSelect.value=String(enhancer.findIndex(m=>m.model===before?.model&&m.endpoint_id===before?.endpoint_id));update();}catch(e){enhancerSelect.replaceChildren(new Option('Enhancer unavailable',''));update();}}
  enhancerSelect.onchange=()=>{update();save();};
  enhance.onclick=async()=>{const model=enhancer[Number(enhancerSelect.value)];if(!model||enhance.disabled)return;const before=prompt.value,captured=revision;enhancing=true;enhancerController=new AbortController();update();try{const payload={recipe:'seed_hunter',prompt:before,mode:'ref2va',width:Number(fields.width.value),height:Number(fields.height.value),frames:Number(fields.frames.value),endpoint_id:model.endpoint_id,model:model.model,reference_counts:seedHunterEnhancerInputs(uploads,readOptions()).counts};const body=new FormData();body.append('config',JSON.stringify(payload));for(const [key,files] of Object.entries(seedHunterEnhancerInputs(uploads,readOptions()).images))for(const file of files)body.append(key,file,file.name);const result=await request('/enhance-prompt',{method:'POST',body,signal:enhancerController.signal});if(!closed&&captured===revision&&prompt.value===before){if(!result.prompt?.trim()||result.prompt.length>16000)throw new Error('Enhancement returned no usable prompt.');previousPrompt=before;prompt.value=result.prompt;changed();status.textContent='Prompt enhanced. Review it before generating.';}}catch(e){if(e.name!=='AbortError')fail(e);}finally{enhancing=false;if(!closed)update();}};
  restorePrompt.onclick=()=>{if(previousPrompt!==null){prompt.value=previousPrompt;previousPrompt=null;changed();}};checkModels.onclick=checkEnhancer;
  function onKey(event){if(document.querySelector('.settings-help-tooltip'))return;if(document.querySelector('.video-server-files-overlay,.h3-lora-library-overlay,.video-workflow-export-overlay'))return;if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();}else if(event.key==='Tab'){const controls=[...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,a[href]')].filter(n=>n.getClientRects().length);if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus();}}}
  const close=bindMenuDismiss(overlay,()=>{closed=true;disposeHelp();clearInterval(timer);window.removeEventListener('keydown',onKey,true);enhancerController?.abort();picker?.close();library?.close();queue?.destroy();batchQueue?.destroy();Object.values(uploads).forEach(state=>state.rangeEditors.forEach(editor=>editor.destroy()));overlay.remove();if(anchor?.isConnected)anchor.focus();},()=>false);
  closeButton.onclick=close;window.addEventListener('keydown',onKey,true);closeButton.focus();
  for(const [key,value]of Object.entries({...BASE,...(saved.config||{})}))if(fields[key])fields[key].value=value;
  for(const key of NESTED){const n=options[key],v=saved.config?.seed_hunter?.[key]??OPTIONS[key];if(n.type==='checkbox')n.checked=v;else n.value=v??'';}
  update();loadInventory();loadJobs();checkEnhancer();const timer=setInterval(()=>{if(!document.hidden)loadJobs();},3000);
  return{overlay,close};
}
