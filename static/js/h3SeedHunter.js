import { createAutoRefineSettings, autoRefineParent, allPreviewJobs } from './h3AutoRefine.js';
import { bindWorkflowPromptHistory } from './workflowPromptHistory.js';
import { createVideoJobPager, observePagedVideoJobEditor } from './videoJobPage.js';
import { createVideoRefinements } from './videoRefinements.js';
import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';
import { createVideoJobSources } from './videoJobSources.js';
import { bindMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { bindSeedHunterHelp } from './seedHunterHelp.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { createH3LoraEditor, h3LoraStack, h3LoraComponents, h3LoraIssue, insertH3LoraTrigger } from './h3Loras.js';
import { showH3LoraLibrary } from './h3LoraLibrary.js';
import { showVideoServerFiles } from './videoServerFiles.js';
import { referenceVideoRanges, restoreReferenceVideoRanges, createReferenceVideoRangeEditor, referenceVideoRangesField } from './referenceVideoRange.js';
import { appendVideoInputs, videoFormIssue } from './videoInputs.js';
import { createVideoBatchQueue } from './videoBatch.js';
import { createVideoJobReruns, BASE_RERUN_LABEL, BASE_RERUN_HELP } from './videoJobRerun.js';
import { createVideoQueueControls, videoQueueField } from './videoQueue.js';
import { createVideoJobEditor, videoJobEditFormData } from './videoJobEdit.js';
import { showVideoWorkflowExportOptions } from './h3Video.js';
import * as videoWorkflow from './videoWorkflow.js';
import { createWorkflowPipeline } from './workflowPipeline.js';
import { SEED_SCOUT_OPTIONS, seedScoutDraft, isSeedScoutJob, createSeedScoutPreviews } from './h3SeedScout.js';

const API = '/api/video/h3', STORAGE = 'odysseus-h3-seed-hunter-v1';
const ACTIVE = new Set(['queued', 'running']), TERMINAL = new Set(['completed', 'failed', 'stopped']);
const OUTER = ['mode', 'model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler', 'gpu', 'vae_gpu', 'width', 'height', 'frames', 'steps', 'seed', 'sampler', 'scheduler', 'shift_video', 'shift_audio', 'reference_size'];
const FINAL_FIELDS = new Set(['prompt','width','height','steps','seed','sampler','scheduler','gpu','vae_gpu','latent_upscaler','shift_video','shift_audio']);
const FINAL_OPTIONS = new Set(['denoise','output_fps','upscaler_chunking','upscaler_force_unload','shift_enabled','attention_backend','sparse_method','sparse_keep_percent','sparse_start','sparse_end','sparse_min_tokens','sparse_extra_tokens','sparse_sink_conditioning','ff_chunk_size','ff_chunk_threshold','low_vram_chunks']);
const NUMERIC = new Set(['width', 'height', 'frames', 'steps', 'seed', 'shift_video', 'shift_audio']);
const NESTED = ['stage', 'preview_count', 'denoise', 'shift_enabled', 'first_frame_guide', 'last_frame_guide', 'force_audio', 'continuation_overlap', 'output_fps', 'attention_backend', 'sparse_method', 'upscaler_chunking', 'upscaler_force_unload', 'source_start_seconds', 'source_end_seconds', 'audio_start_seconds', 'audio_end_seconds'];
const BASE = { mode: 'ref2va', width: 480, height: 864, frames: 73, steps: 20, seed: 42, sampler: 'er_sde', scheduler: 'beta', shift_video: 12, shift_audio: 6, reference_size: 'max', vae_gpu: '', loras: [] };
const OPTIONS = { stage: 'preview', preview_count: 3, denoise: 0.4, shift_enabled: false, first_frame_guide: false, last_frame_guide: false, force_audio: false, continuation_overlap: 22, output_fps: 24, attention_backend: 'comfy kitchen attention', sparse_method: 'none', upscaler_chunking: true, upscaler_force_unload: false, source_start_seconds: 0, source_end_seconds: null, audio_start_seconds: 0, audio_end_seconds: null, sparse_keep_percent:10,sparse_start:.2,sparse_end:1,sparse_min_tokens:12288,sparse_extra_tokens:256,sparse_sink_conditioning:'exact_kv_and_rows',ff_chunk_size:0,ff_chunk_threshold:2048,low_vram_chunks:0,refmod_strengths:[] };
const make = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const button = text => { const n = make('button', 'memory-toolbar-btn', text); n.type = 'button'; return n; };
const displayName = item => item.display_name || item.name;
const clone = value => JSON.parse(JSON.stringify(value));
const randomSeed = () => Math.floor(Math.random() * 2 ** 48);

export function seedHunterComponentId(value, components = []) {
  if (components.some(item => String(item.id) === String(value))) return value;
  const matches = components.filter(item => item.aliases?.includes(String(value)));
  return matches.length === 1 ? matches[0].id : value;
}

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
  return { ...value, ...(automatic && ['preview', 'single', 'scout'].includes(value.seed_hunter?.stage)
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
export function seedHunterPreviewCount(value) {
  const count=Number(value);return Number.isFinite(count)?Math.max(1,Math.min(3,Math.round(count))):3;
}
export function seedHunterConfig(values, options, loras) {
  const config = { recipe: 'seed_hunter', prompt: values.prompt || '', loras: clone(loras), seed_hunter: { ...OPTIONS, ...options } };
  config.seed_hunter.preview_count=seedHunterPreviewCount(config.seed_hunter.preview_count);
  for (const key of OUTER) if (values[key] !== undefined) config[key] = NUMERIC.has(key) ? Number(values[key]) : values[key];
  if (['single', 'continuation'].includes(config.seed_hunter.stage)) config.seed_hunter.preview_count = 1;
  if(!(['preview','final'].includes(config.seed_hunter.stage)||config.seed_hunter.stage==='scout'&&config.seed_hunter.scout_mode!=='scout'&&config.seed_hunter.scout_upscale) || !config.latent_upscaler) delete config.latent_upscaler;
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

export function showH3SeedScout(options = {}, anchor = document.activeElement) { return showH3SeedHunter({ ...options, scout: true }, anchor); }

export function showH3SeedHunter({ scout = false, layout = 'classic' } = {}, anchor = document.activeElement) {
  const storageKey = (scout ? 'odysseus-h3-seed-scout-v1' : STORAGE) + (layout === 'pipeline' ? ':pipeline' : ''), title = scout ? 'H3 Seed Scout' : 'H3 Seed Hunter';
  const defaults = { ...OPTIONS, ...(scout ? SEED_SCOUT_OPTIONS : {}) }, base = { ...BASE, ...(scout ? { steps: 20, sampler:'euler', scheduler:'simple', shift_audio:3 } : {}) };
  if (scout && !document.getElementById('h3-scout-styles')) { const css = make('link'); css.id = 'h3-scout-styles'; css.rel = 'stylesheet'; css.href = '/static/h3-seed-scout.css'; document.head.append(css); }
  document.querySelectorAll('.h3-seed-hunter-overlay').forEach(dismissOrRemove);
  const retained = scout ? seedScoutDraft(undefined,layout) : null;
  let pipeline = null, promptHistory = null;
  if (!document.getElementById('h3-video-styles')) { const css = make('link'); css.id = 'h3-video-styles'; css.rel = 'stylesheet'; css.href = '/static/h3-video.css'; document.head.append(css); }
  let saved = {}; try { saved = JSON.parse(localStorage.getItem(storageKey) || '{}'); } catch {}
  if (retained) saved = { ...saved, config: retained.config };
  const bulkSelected = new Map(); let bulkPending = saved.auto_request || null, bulkPartialResult = false;
  let inventory = null, jobs = [], closed = false, busy = false, loading = false, jobPager = null, revision = 0;
  let picker, library, queue, batchQueue, jobEditor, editingComponents = null, selection = null, finalRequest = null, finalBusy = false;
  let restoredRangeConfig = {}, optionSnapshot = {}, selecting = 0, finalComponents = [], enhancer = [], enhancing = false, enhancerController, previousPrompt = null;
  const fields = {}, options = {}, uploads = {}, finalFields = {}, jobNodes = new Map();
  const overlay = make('div', 'cookbook-edit-overlay h3-seed-hunter-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal h3-video-dialog h3-seed-hunter-dialog'); dialog.setAttribute('role', 'dialog'); dialog.setAttribute('aria-modal', 'true'); dialog.setAttribute('aria-label', title);
  const header = make('div', 'h3-video-header'), closeButton = button('Close'); header.append(make('h2', '', title), closeButton);
  const scroll = make('div', 'h3-video-scroll'); dialog.append(header, scroll); overlay.append(dialog); document.body.append(overlay);
  scroll.append(make('p', 'h3-video-target', `Local Odysseus server · ${window.location.host}`), make('p', 'h3-video-description', scout ? 'Compare early seed previews, continue the chosen seed, then optionally upscale its finished latent and refine the final video.' : 'Generate previews, then refine a selected result.'));
  const status = make('p', 'h3-video-muted', `Checking ${title} components…`); status.setAttribute('role', 'status');
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
  const stage = select(componentGrid, 'stage', 'Generation', scout ? [['scout', 'Partial previews → choose seed → finish']] : [['preview', 'Seed previews → selected upscale'], ['single', 'Single pass video'], ['continuation', 'Continue a video'], ['dialogue', 'Dialogue / audio previews'], ['final', 'Saved latent refinement']], options);
  const effectiveComponents = () => h3LoraComponents(inventory?.components || [], editingComponents);
  const loras = createH3LoraEditor({ idPrefix: 'seed-hunter', onChange: () => { revision++; save(); update(); },
    onManage: (selectedId, trigger) => { library?.close(); library = showH3LoraLibrary({ components: inventory?.components || [], selectedId, onChange: loadInventory }, trigger); },
    onInsertTrigger: component => { if(stage.value==='final'){fail('Refinement keeps the saved preview prompt.');return;} const profile = component.lora_profile; fields.prompt.value = insertH3LoraTrigger(fields.prompt.value, profile.trigger_words, profile.trigger_position, fields.prompt.selectionStart); changed(); fields.prompt.focus(); },
    onApplyRecommended: component => { for (const [key, value] of Object.entries(component.lora_profile?.recommended_settings || {})) if (fields[key] && (stage.value!=='final'||FINAL_FIELDS.has(key))) fields[key].value = value; changed(); },
  }); form.append(loras.node);
  const prompt = field(form, 'prompt', 'Prompt', make('textarea')); prompt.dataset.workflowPrompt = ''; prompt.rows = 5; prompt.maxLength = 16000; prompt.required = true;
  const enhancerBar = make('div', 'h3-video-actions'), enhance = button('Enhance prompt'), restorePrompt = button('Restore original prompt'), enhancerSelect = make('select', 'cookbook-field-input'), checkModels = button('Check models'); enhancerSelect.setAttribute('aria-label', 'Enhancement model');
  enhance.dataset.seedHunterHelpAction = 'prompt_enhancement';
  enhancerBar.append(enhancerSelect, enhance, restorePrompt, checkModels); form.append(enhancerBar); const enhancerInfo=make('p','h3-video-muted');form.append(enhancerInfo); restorePrompt.hidden = true;
  const dialoguePanel = group(form, 'Dialogue prompt builder', true), dialogueText = field(dialoguePanel, 'dialogue_text', 'Line of dialogue', make('textarea'));
  const dialogueTone = field(dialoguePanel, 'dialogue_tone', 'Tone and delivery', make('textarea'));
  const dialogueTemplate = field(dialoguePanel, 'dialogue_template', 'Editable prompt template', make('textarea')); dialogueTemplate.rows = 5;
  dialogueTemplate.value = 'subject_definitions:\n<Subject 1> is a character.\n\nsummary:\n[audio reference] A character speaks to the camera.\n\nretention_analysis:\n<Audio 1>: reference - use its vocal timbre without copying its dialogue.\n\ndetailed_description:\n<Subject 1> says: <d>[English] {{dialogue}}</d> {{tone}}\n\noverall_soundscape:\nClear audible dialogue.\n\nnon_diegetic_music:\nN/A';
  const applyDialogue = button('Build visible prompt'), dialogueDefaults=button('Apply dialogue sampling defaults'); dialoguePanel.append(applyDialogue,dialogueDefaults); dialogueDefaults.onclick=()=>{fields.width.value=fields.height.value=256;fields.frames.value=158;fields.steps.value=16;fields.sampler.value='euler';fields.scheduler.value='simple';changed();}; applyDialogue.onclick = () => { prompt.value = seedHunterDialoguePrompt(dialogueText.value, dialogueTone.value, dialogueTemplate.value); changed(); };
  const dimensions = make('div', 'h3-video-grid'); form.append(dimensions);
  number(dimensions, 'width', scout ? 'Width' : 'Preview width', 480, 64, 4096, 32); number(dimensions, 'height', scout ? 'Height' : 'Preview height', 864, 64, 4096, 32);
  number(dimensions, 'frames', 'Frames at 24 fps', 73, 5, 362, 17); number(dimensions, 'steps', scout ? 'Full schedule steps' : 'Preview steps', 20, 1, 100); number(dimensions, 'seed', 'Base seed', 42, 0, Number.MAX_SAFE_INTEGER);
  const reroll = button('Randomize base seed'); dimensions.append(reroll); reroll.onclick = () => { fields.seed.value = randomSeed(); changed(); };
  select(dimensions, 'preview_count', 'Preview count', [1,2,3].map(value=>[String(value),String(value)]), options);
  options.preview_count.value='3';options.preview_count.parentElement.append(make('small','h3-video-muted','Up to 3 previews per video.'));
  if (scout) {
    number(dimensions, 'scout_count', 'Seed choices', 3, 1, 32, 1, options);
    number(dimensions, 'scout_step', 'Preview after step', 3, 1, 99, 1, options);
    select(dimensions, 'scout_preview_mode', 'Preview decoding', [['vae','Video VAE'],['latent2rgb','Fast latent preview']], options);
  }
  const duration = make('p', 'h3-video-muted'); dimensions.append(duration);
  const scoutUpscale = scout ? group(form, 'Final latent upscale', true) : null;
  const scoutUpscaleSummary = scout ? make('p', 'h3-video-muted') : null;
  if (scout) {
    check(scoutUpscale, 'scout_upscale', 'Upscale the selected final video');
    for (const axis of ['width','height']) {
      const control=number(scoutUpscale,`scout_final_${axis}`,`Final ${axis} (blank = 2× scouting ${axis})`,'',32,4096,32,options);
      control.placeholder='Automatic · 2×';
    }
    number(scoutUpscale,'scout_refine_steps','Upscale refinement steps',2,1,100,1,options);
    number(scoutUpscale,'denoise','Upscale refinement denoise',.4,0,1,.05,options);
    scoutUpscale.append(scoutUpscaleSummary);
  }
  const sizeTools = make('div', 'h3-video-actions'), aspect = make('select', 'cookbook-field-input'), megapixels = make('input', 'cookbook-field-input'), applySize = button('Apply preview size');
  aspect.setAttribute('aria-label', 'Preview aspect ratio'); for (const value of ['1:1', '2:3', '3:2', '3:4', '4:3', '9:16', '16:9', '21:9']) aspect.add(new Option(value, value)); aspect.value = '9:16';
  megapixels.type = 'number'; megapixels.value = '0.4'; megapixels.min = '0.1'; megapixels.max = '4'; megapixels.step = 'any'; megapixels.setAttribute('aria-label', 'Preview megapixels'); sizeTools.append(megapixels, aspect, applySize); form.append(sizeTools);
  function syncSizeControls(remembered) {
    const width = Number(fields.width.value), height = Number(fields.height.value);
    if (!(width > 0 && height > 0)) return;
    const ratios = [...aspect.options].filter(option => !option.dataset.custom).map(option => option.value);
    const nearest = ratios.reduce((best, ratio) => {
      const [w, h] = ratio.split(':').map(Number), [bw, bh] = best.split(':').map(Number);
      return Math.abs(Math.log(width / height / (w / h))) < Math.abs(Math.log(width / height / (bw / bh))) ? ratio : best;
    });
    const [rw, rh] = nearest.split(':').map(Number);
    let ratio = Math.abs(width / height / (rw / rh) - 1) < .06 ? nearest : `${width}:${height}`;
    if (remembered && ratios.includes(remembered.aspect)) ratio = remembered.aspect;
    aspect.querySelectorAll('option[data-custom]').forEach(option => option.remove());
    if (!ratios.includes(ratio)) { const option = new Option(`Custom · ${ratio}`, ratio); option.dataset.custom = 'true'; aspect.add(option); }
    aspect.value = ratio;
    const mp = Number(remembered?.megapixels);
    megapixels.value = String(mp >= .1 && mp <= 4 ? mp : Number((width * height / 1048576).toFixed(3)));
  }
  for (const key of ['width', 'height']) fields[key].addEventListener('input', () => syncSizeControls());
  const seconds=make('input','cookbook-field-input'),applyDuration=button('Apply duration');seconds.type='number';seconds.min='3';seconds.max='15.083';seconds.step='any';seconds.setAttribute('aria-label','Preview seconds');sizeTools.append(seconds,applyDuration);
  function syncDuration() {const frames=Number(fields.frames.value);if(Number.isFinite(frames)&&frames>0)seconds.value=String(Number((frames/24).toFixed(3)));}
  function applyDurationValue() {if(!seconds.value||!seconds.validity.valid)return;fields.frames.value=seedHunterFrames(seconds.value);changed();}
  seconds.addEventListener('input',applyDurationValue);seconds.addEventListener('blur',()=>{if(seconds.validity.valid)syncDuration();});applyDuration.onclick=applyDurationValue;
  fields.frames.addEventListener('input',syncDuration);
  applySize.onclick = () => { try { const size = seedHunterResolution(megapixels.value, ...aspect.value.split(':')); fields.width.value = size.width; fields.height.value = size.height; changed(); } catch (e) { fail(e); } };
  const advanced = group(form, 'Sampling and optional controls');
  if (scout) { select(advanced,'scout_mode','Scout mode',[['interactive','Choose a seed and continue'],['scout','Partial previews only'],['final','Single full pass']],options);number(advanced,'scout_timeout','Selection timeout (seconds; 0 waits)',0,0,86400,1,options); }
  select(advanced, 'sampler', 'Sampler', [['er_sde', 'ER-SDE'], ['euler', 'Euler'], ['res_multistep', 'RES multistep']]);
  select(advanced, 'scheduler', 'Scheduler', [['beta', 'Beta'], ['simple', 'Simple'], ['normal', 'Normal'], ['linear_quadratic', 'Linear quadratic']]);
  select(advanced, 'reference_size', 'Reference sizing', [['max', 'Maximum'], ['match', 'Match output']]);
  check(advanced, 'shift_enabled', 'Override video/audio shifts'); number(advanced, 'shift_video', 'Video shift', 12, .01, 100, .01); number(advanced, 'shift_audio', 'Audio shift', 6, .01, 100, .01);
  check(advanced, 'first_frame_guide', 'Use Picture 1 as first-frame guide'); check(advanced, 'last_frame_guide', 'Use Picture 2 as last-frame guide'); check(advanced, 'force_audio', 'Preserve supplied Audio 1 in the latent');
  const fpsChoices = [['24','24 fps · RIFE off'],['48','48 fps · RIFE interpolation']];
  select(dimensions, 'output_fps', 'Output FPS / RIFE', fpsChoices, options).setAttribute('aria-label', 'Output FPS / RIFE');
  const rifeStatus = make('p', 'h3-video-muted'); dimensions.append(rifeStatus);
  select(advanced, 'attention_backend', 'Attention backend', [['comfy kitchen attention', 'Comfy Kitchen'], ['pytorch', 'PyTorch']], options);
  select(advanced, 'sparse_method', 'Sparse attention', [['none', 'Off'], ['sla', 'SLA'], ['sol_attn', 'Sol-Attn'], ['vsa', 'VSA']], options);
  number(advanced, 'sparse_keep_percent', 'Sparse attention kept (%)', 10, 1, 100, .1, options);
  number(advanced, 'sparse_start', 'Sparse attention start', .2, 0, 1, .05, options); number(advanced, 'sparse_end', 'Sparse attention end', 1, 0, 1, .05, options);
  number(advanced, 'sparse_min_tokens', 'Sparse minimum tokens', 12288, 0, 1000000, 1, options); number(advanced, 'sparse_extra_tokens', 'Sparse extra tokens', 256, 0, 8192, 1, options);
  select(advanced, 'sparse_sink_conditioning', 'Sparse reference preservation', [['exact_kv_and_rows','Preserve reference keys and rows'],['none','Off']], options);
  number(advanced, 'ff_chunk_size', 'Feed-forward chunks (0 = auto for INT8)', 0, 0, 64, 1, options); number(advanced, 'ff_chunk_threshold', 'Feed-forward chunk threshold', 2048, 1, 1000000, 1, options);
  number(advanced, 'low_vram_chunks', 'Attention chunks (0 = off)', 0, 0, 64, 1, options);
  if (!scout) number(advanced, 'denoise', 'Default refinement denoise', .4, 0, 1, .05, options);
  check(advanced, 'upscaler_chunking', 'Temporal chunking for latent upscale', true); check(advanced, 'upscaler_force_unload', 'Unload latent upscaler after use');
  const continuationPanel = group(form, 'Continuation source and timing', true);
  select(continuationPanel, 'continuation_overlap', 'Overlap frames', [5,22,39,56].map(n => [String(n), String(n)]), options);
  number(continuationPanel, 'source_start_seconds', 'Source start (seconds)', 0, 0, 86400, .01, options); number(continuationPanel, 'source_end_seconds', 'Source end (blank = end)', '', 0, 86400, .01, options);
  const audioPanel = group(form, 'Voice reference timing'); number(audioPanel, 'audio_start_seconds', 'Audio start (seconds)', 0, 0, 86400, .01, options); number(audioPanel, 'audio_end_seconds', 'Audio end (blank = end)', '', 0, 86400, .01, options);
  const batchContainer=make('div');form.append(batchContainer);
  const media = make('div', 'h3-video-inputs'); form.append(media);
  function upload(key, label, accept, multiple, limit) {
    const wrap = make('section', 'h3-video-upload'), title = make('label', 'h3-video-upload-label', label), input = make('input'); input.type = 'file'; input.accept = accept; input.multiple = multiple; input.id = `seed-hunter-input-${key}`; title.htmlFor = input.id;
    const mediaKind=accept.startsWith('image/')?'image':accept.startsWith('audio/')?'audio':'video';
    const list = make('ul', 'h3-video-file-list'), server = accept === '.safetensors' ? null : button('Server files');
    const state = { files: [], retained: [], strengths:[], rangeEditors:[], wrap, input, server, limit, render: () => {
      state.rangeEditors.forEach(editor=>editor.destroy());state.rangeEditors=[];
      list.replaceChildren(); for (const [i, file] of [...state.retained, ...state.files].entries()) { const row = make('li'), remove = button('Remove'); remove.setAttribute('aria-label', `Remove ${file.name}`); remove.onclick = () => { state.strengths.splice(i,1); if (i < state.retained.length) state.retained.splice(i, 1); else state.files.splice(i - state.retained.length, 1); state.render(); changed(); }; row.append(make('span', '', `${file.name}${i < state.retained.length ? ' · saved on server' : file.serverFile ? ` · ${file.serverFile.root.toUpperCase()}:` : ''}`), remove); if(key==='reference_mods'){const strength=make('input','cookbook-field-input');strength.type='number';strength.min=0;strength.max=1;strength.step=.05;strength.value=state.strengths[i]??1;strength.setAttribute('aria-label',`Strength for ${file.name}`);strength.oninput=()=>{state.strengths[i]=Number(strength.value);changed();};row.append(strength);} if(key==='reference_videos'){row.classList.add('h3-reference-video-row');const editor=createReferenceVideoRangeEditor(file,changed);state.rangeEditors.push(editor);row.append(editor.node);} list.append(row); }
    } }; uploads[key] = state;
    const add = files => { if(input.disabled) throw new Error('This input is unavailable for the current job or runtime.'); const next = multiple ? [...state.files, ...files] : files.slice(0, 1); if (next.length + (multiple ? state.retained.length : 0) > limit) throw new Error(`Choose at most ${limit} ${label.toLowerCase()}.`); if (!multiple) state.retained = []; state.files = next; state.render(); changed(); };
    input.onchange = () => { try { add([...input.files]); } catch (e) { fail(e); } input.value = ''; };
    if (server) server.onclick = () => { picker?.close(); picker = showVideoServerFiles({ family: 'h3', kind:mediaKind, multiple, onSelect: add }, server); };
    wrap.addEventListener('dragover', event => { event.preventDefault(); }); wrap.addEventListener('drop', event => { event.preventDefault(); try { add([...event.dataTransfer.files]); } catch (e) { fail(e); } });
    wrap.append(title, input); if (server) wrap.append(server); wrap.append(list); media.append(wrap); return state;
  }
  upload('source_video', 'Continuation source video', 'video/*', false, 1);
  upload('first_frame','First-frame guide (optional; otherwise Picture 1)','image/*',false,1);upload('last_frame','Last-frame guide (optional; otherwise Picture 2)','image/*',false,1);
  upload('reference_images', 'Reference pictures (up to 9)', 'image/*', true, 9); upload('reference_videos', 'Reference videos (up to 3)', 'video/*', true, 3); upload('reference_audio', 'Reference audio (up to 3)', 'audio/*,.mka', true, 3);
  upload('reference_mods', 'Reference mod bundles (optional, up to 8)', '.safetensors', true, 8);
  const actions = make('div', 'h3-video-actions'), submit = button('Generate previews'); submit.type = 'submit'; submit.className = 'cookbook-btn'; actions.append(submit); form.append(actions);
  const autoBar = make('section', 'h3-auto-refine-bar'), autoConfigure = button('Automatic refinements…'), autoOff = button('Turn off automatic refinements'), autoDescription = make('p', 'h3-video-muted');
  autoBar.append(autoConfigure, autoOff, autoDescription); form.insertBefore(autoBar, actions);
  autoConfigure.onclick = () => openAutomatic(); autoOff.onclick = () => { delete optionSnapshot.auto_refine; changed(); };
  const queueContainer = make('div'); scroll.append(queueContainer);
  const finalSection = make('section', 'h3-seed-hunter-final'); finalSection.hidden = true; const finalTitle = make('h3'), finalDescription = make('p', 'h3-video-muted'), finalForm = make('form'), finalGrid = make('div', 'h3-video-grid');
  const autoSettings = createAutoRefineSettings(() => { finalRequest = null; update(); save(); }); autoSettings.node.hidden = true; finalForm.append(autoSettings.node, finalGrid); finalSection.append(finalTitle, finalDescription, finalForm); scroll.append(finalSection);
  select(finalGrid, 'latent_upscaler', 'Latent upscaler', [], finalFields); number(finalGrid, 'width', 'Final width', 1088, 64, 4096, 32, finalFields); number(finalGrid, 'height', 'Final height', 1952, 64, 4096, 32, finalFields);
  select(finalGrid,'frames','Final output length',Array.from({length:18},(_,index)=>73+17*index).filter(frames=>frames<=362).map(frames=>[String(frames),`${(frames/24).toFixed(2)} seconds · ${frames} frames`]),finalFields);
  finalFields.frames.prepend(new Option('Choose final output length',''));finalFields.frames.required=true;
  number(finalGrid, 'steps', 'Refinement steps', 2, 1, 100, 1, finalFields); number(finalGrid, 'seed', 'Refinement seed', 42, 0, Number.MAX_SAFE_INTEGER, 1, finalFields); number(finalGrid, 'denoise', 'Denoise', .4, 0, 1, .05, finalFields);
  select(finalGrid, 'sampler', 'Refinement sampler', [['er_sde','ER-SDE'],['euler','Euler'],['res_multistep','RES multistep']], finalFields); select(finalGrid, 'scheduler', 'Refinement scheduler', [['linear_quadratic','Linear quadratic'],['beta','Beta'],['simple','Simple'],['normal','Normal']], finalFields);
  select(finalGrid, 'output_fps', 'Final output FPS / RIFE', fpsChoices, finalFields).setAttribute('aria-label', 'Final output FPS / RIFE');
  const finalRifeStatus = make('p', 'h3-video-muted'); finalForm.append(finalRifeStatus);
  const finalLoras = createH3LoraEditor({ idPrefix: 'seed-hunter-final', onChange: () => { finalRequest = null; save(); } }); finalForm.append(finalLoras.node);
  const finalActions = make('div', 'h3-video-actions'), finalSubmit = button('Render selected preview'), finalReroll = button('Randomize refinement seed'), cancelSelection = button('Clear selection'); finalSubmit.dataset.seedHunterHelpAction = 'refine_preview'; finalSubmit.type = 'submit'; finalSubmit.className = 'cookbook-btn'; finalActions.append(finalSubmit, finalReroll, cancelSelection); finalForm.append(finalActions);
  finalReroll.onclick = () => { finalFields.seed.value = randomSeed(); finalRequest = null; save(); }; cancelSelection.onclick = () => { selecting++; selection = null; finalRequest = null; finalSection.hidden = true; save(); renderJobs(); };
  finalForm.addEventListener('input', () => { finalRequest = null; describeRefinement();update();save(); }); finalForm.addEventListener('change', () => { finalRequest = null; describeRefinement();update();save(); });
  const selectAllPreviews = button('Select all preview jobs'), bulkRefine = button('Refine selected jobs…'), retryBulk = button('Retry bulk refinement submission'), finishBulk = button('Finish with queued refinements');
  const jobToolbar = make('div', 'h3-video-toolbar'), refreshJobs = button('Refresh jobs'); jobToolbar.append(make('h3', '', `${title} jobs`), refreshJobs, selectAllPreviews, bulkRefine, retryBulk, finishBulk);
  finishBulk.onclick = () => { if(finalBusy || !bulkPartialResult)return; bulkPending=null;bulkPartialResult=false;bulkSelected.clear();selection=null;finalSection.hidden=true;save();update(); };
  selectAllPreviews.onclick = async () => { try { selectAllPreviews.disabled = true; const rows = await allPreviewJobs(request, scout); bulkSelected.clear(); rows.forEach(j => bulkSelected.set(j.id, j)); update(); } catch(e) { fail(e); } finally { selectAllPreviews.disabled = false; } };
  bulkRefine.onclick = () => openAutomatic([...bulkSelected.values()]); retryBulk.onclick = () => submitAutomatic(); const jobStatus = make('p', 'h3-video-muted'), jobList = make('div', 'h3-video-jobs'); scroll.append(jobToolbar, jobStatus, jobList);
  jobPager = createVideoJobPager({ container: scroll, beforeNode: jobList, request: (path, options) => request(path.startsWith('/jobs?') ? path + (scout ? '&stage=scout' : '&stage=not_scout&group_refinements=true') : path, options), recipe: 'seed_hunter', isClosed: () => closed,
    onData: async data => {
      jobs = data.jobs.filter(job => scout ? isSeedScoutJob(job) : !isSeedScoutJob(job)); queue?.update(data.queue); renderJobs();
      await observePagedVideoJobEditor(jobEditor, data.legacyJobs || jobs.flatMap(job => [job, ...(job.refinements || [])]), request, () => closed);
      if (!closed) update();
      if (!scout && saved.selected && !selection && inventory) {
        const remembered = saved.selected;
        let job = (data.legacyJobs || jobs).find(job => job.id === remembered.job_id);
        if (!job) {
          try { job = await request(`/jobs/${encodeURIComponent(remembered.job_id)}`); }
          catch (error) {
            if (error.status !== 404) throw error;
            if (!closed && saved.selected === remembered) { saved.selected = null; save(); }
            return;
          }
        }
        if (closed || selection || saved.selected !== remembered) return;
        const candidate = job.seed_hunter?.candidates?.find(item => item.index === remembered.index);
        saved.selected = null;
        if (candidate?.can_refine) await selectCandidate(job, candidate, true); else save();
      }
    }, onError: error => { jobStatus.textContent = `Could not refresh jobs: ${error.message}`; },
  });
  const disposeHelp = bindSeedHunterHelp(overlay, scout ? { seed: { title:'Base seed', body:'Seed Scout tries consecutive seeds: the base seed, base + 1, base + 2, and so on. Each reaches the chosen preview step. Selecting one continues its saved partial latent through the rest of the full schedule.' }, steps: {title:'Full schedule steps',body:'Total sampling updates for the final video. Each candidate runs up to Preview after step; your selected seed continues the remaining updates. For 20 total steps and previews after step 3, the selected seed continues with 17 steps.'} } : {});
  function fail(reason) { error.textContent = reason?.message || String(reason || ''); error.hidden = !error.textContent; }
  function readOptions() {
    const value={...optionSnapshot, refmod_strengths:uploads.reference_mods?[...uploads.reference_mods.retained,...uploads.reference_mods.files].map((_,i)=>uploads.reference_mods.strengths[i]??1):[], ...Object.fromEntries(Object.keys(options).map(key => { const n = options[key]; return [key, n.type === 'checkbox' ? n.checked : n.type === 'number' || ['preview_count','continuation_overlap','output_fps','scout_timeout'].includes(key) ? (n.value === '' ? null : Number(n.value)) : n.value]; }))};
    delete value.auto_refine;
    if(inventory?.seed_hunter?.features?.scout_upscale!==true)for(const key of ['scout_upscale','scout_final_width','scout_final_height','scout_refine_steps'])delete value[key];
    return value;
  }
  function config() {
    const value = seedHunterConfig(Object.fromEntries([...OUTER, 'prompt'].map(key => [key, fields[key].value])), readOptions(), loras.getValue());
    // A final job already owns its conditioning. Preserve even the absence of
    // legacy range metadata so editing steps cannot alter its source contract.
    if (stage.value === 'final') return { ...value, ...restoredRangeConfig };
    return { ...value, reference_video_ranges: referenceVideoRanges(uploads.reference_videos) };
  }
  function currentUploads() { return Object.fromEntries(Object.entries(uploads).filter(([key]) => (key !== 'source_video' || stage.value === 'continuation') && (key !== 'first_frame' || options.first_frame_guide.checked) && (key !== 'last_frame' || options.last_frame_guide.checked)).map(([key, state]) => [key, [...state.files]])); }
  function finalConfig() { return { ...Object.fromEntries(Object.entries(finalFields).filter(([key])=>key!=='output_fps'&&(key!=='frames'||inventory?.seed_hunter?.features?.separate_final_length===true)).map(([key, n]) => [key, n.type === 'number'||key==='frames' ? Number(n.value) : n.value])), loras: finalLoras.getValue(), seed_hunter: { output_fps: Number(finalFields.output_fps.value) } }; }
  function describeRefinement() {
    if(!selection)return; if(selection.automatic){finalDescription.textContent='Shared refinement settings apply once to every selected candidate. Each job retains its own seed, saved latent, prompt, files and selected media sections.';return;}
    const preview=selection.frames,frames=inventory?.seed_hunter?.features?.separate_final_length?Number(finalFields.frames.value)||preview:preview;
    if(inventory?.seed_hunter?.features?.separate_final_length&&!finalFields.frames.value){finalDescription.textContent='Choose a final output length before rendering. '+(selection.final_length?.error||'The source video duration could not be determined.');return;}
    finalDescription.textContent=frames!==preview
      ? `Preview: ${(preview/24).toFixed(2)} seconds. Final output: ${(frames/24).toFixed(2)} seconds. The selected seed is rendered at the final length before upscaling and refinement, using the saved prompt and references.`
      : `Preview and final output: ${(preview/24).toFixed(2)} seconds. Reuses the selected preview’s latent and saved inputs.`;
  }
  function save() { if (jobEditor?.active) return; try { localStorage.setItem(storageKey, JSON.stringify({ config: config(), sizing: { aspect: aspect.value, megapixels: megapixels.value }, dialogue_text: dialogueText.value, dialogue_tone: dialogueTone.value, dialogue_template: dialogueTemplate.value, enhancer: enhancer[Number(enhancerSelect.value)] ? {model:enhancer[Number(enhancerSelect.value)].model,endpoint_id:enhancer[Number(enhancerSelect.value)].endpoint_id} : null, auto_request: bulkPending, selected: selection && !selection.automatic && { job_id: selection.job_id, index: selection.index }, final_config: finalConfig(), final_request: finalRequest })); } catch {} }
  function changed() { revision++; error.hidden = true; update(); save(); }
  function restore(values, inputFiles = {}, retained = {}) {
    restoredRangeConfig = Object.prototype.hasOwnProperty.call(values, 'reference_video_ranges') ? {reference_video_ranges: clone(values.reference_video_ranges)} : {};
    optionSnapshot = clone(values.seed_hunter || {});
    for(const role of ['model','encoder','video_vae','audio_vae','latent_upscaler']) fill(fields[role],(inventory?.components||[]).filter(c=>c.role===role&&(role!=='model'||c.variant==='ref2va')),values[role]??fields[role].value);
    for (const key of OUTER) if (values[key] !== undefined) fields[key].value = values[key]; prompt.value = values.prompt || '';
    for (const key of Object.keys(options)) { const n = options[key], value = values.seed_hunter?.[key] ?? defaults[key] ?? n.value; if (n.type === 'checkbox') n.checked = value; else n.value = key==='preview_count'?seedHunterPreviewCount(value):value ?? ''; }
    loras.setInventory(effectiveComponents()); loras.setValue(h3LoraStack(values));
    for (const [key, state] of Object.entries(uploads)) { state.files = [...(inputFiles[key] || [])]; state.retained = [...(retained[key] || [])]; state.strengths=key==='reference_mods'?[...(values.seed_hunter?.refmod_strengths||[])]:[]; if(key==='reference_videos')restoreReferenceVideoRanges(state,values.reference_video_ranges); state.render(); }
    syncSizeControls();
    revision++; update();
  }
  function batchIssue() {
    if(!inventory?.seed_hunter||!inventory.runtime_ready||scout&&!inventory.seed_hunter.features?.seed_scout)return `${title} components are not ready.`;
    if(!seedHunterBatchChoices(stage.value).some(choice=>choice.field===batchQueue?.inputField))return 'Choose a supported batch input.';
    if(!['model','encoder','video_vae','audio_vae','gpu'].every(key=>fields[key].value))return 'Choose the models, VAEs and GPU first.';
    if(!prompt.value.trim())return 'Enter a prompt to use for every job.';
    if(readOptions().auto_refine?.candidates?.length&&!readOptions().auto_refine?.project?.trim())return 'Name the automatic refinement project.';
    const issue=h3LoraIssue(loras.getValue(),effectiveComponents(),'ref2va');if(issue)return issue;
    const formIssue=videoFormIssue(form);if(formIssue)return formIssue;
    return '';
  }
  function batchSnapshot() {
    const formIssue=videoFormIssue(form);if(formIssue)throw new Error(formIssue);
    const issue=batchIssue();if(issue)throw new Error(issue);
    const field=batchQueue.inputField;
    const automatic=batchQueue.autoVideoLength&&(stage.value!=='preview'||inventory?.seed_hunter?.features?.separate_final_length===true);
    return {config:seedHunterBatchConfig(config(),automatic),videoField:field,autoVideoLength:automatic,
      uploads:Object.fromEntries(Object.entries(currentUploads()).filter(([key])=>key!==field))};
  }
  function update() {
    batchQueue?.setLengthContext(stage.value==='preview'?{label:'Match final output length to each video',automaticHelp:'Previews keep the preview length above. The selected final output matches each source video.',manualHelp:'Previews keep the preview length above. Choose the final output length when selecting a preview.',notice:inventory&&!inventory.seed_hunter?.features?.separate_final_length?'Update the Odysseus server to match final output length automatically. This server will keep the short preview length.':''}:{});
    batchQueue?.setInputChoices(seedHunterBatchChoices(stage.value));
    batchQueue?.setCompanionFields?.([{field:'reference_audio',label:'Audio for this job'},{field:'reference_images',label:'Images for this job'},{field:'reference_videos',label:'Extra videos for this job'},...(options.first_frame_guide.checked?[{field:'first_frame',label:'First-frame guide for this job'}]:[]),...(options.last_frame_guide.checked?[{field:'last_frame',label:'Last-frame guide for this job'}]:[])]);
    const batchBusy=!!batchQueue?.busy, batching=!!batchQueue?.active&&!jobEditor?.active&&stage.value!=='final',locked=busy||batchBusy;
    const finalEdit = stage.value === 'final', single = ['single', 'continuation','final'].includes(stage.value);
    const scoutUpscaling=scout&&options.scout_upscale.checked&&options.scout_mode.value!=='scout';
    fields.latent_upscaler.parentElement.hidden = !finalEdit && stage.value !== 'preview' && !scoutUpscaling;
    for(const key of [...OUTER,'prompt']) if(!['shift_video','shift_audio'].includes(key)) fields[key].disabled = locked || enhancing || (finalEdit && !FINAL_FIELDS.has(key));
    for(const key of ['reference_images','reference_videos','reference_audio'])uploads[key].wrap.hidden=false;
    media.hidden = finalEdit; sizeTools.hidden = finalEdit; applyDialogue.disabled = finalEdit; reroll.disabled = locked;
    for(const state of Object.values(uploads)) {state.input.disabled = finalEdit || locked; if(state.server)state.server.disabled = finalEdit || locked;state.rangeEditors.forEach(editor=>editor.setDisabled(finalEdit||locked));}
    for(const key of Object.keys(options)) options[key].disabled = locked || (finalEdit && !FINAL_OPTIONS.has(key));
    const features = inventory?.seed_hunter?.features || {};
    stage.disabled = !!jobEditor?.active || locked;
    for (const item of stage.options) item.hidden = item.value === 'final' && !finalEdit;
    for (const item of stage.options) item.disabled = (item.value === 'final' && !finalEdit) || ['continuation', 'dialogue'].includes(item.value) && features[item.value] !== true;
    for (const [feature, keys] of Object.entries({ guides: ['first_frame_guide', 'last_frame_guide'], force_audio: ['force_audio'], attention_backend: ['attention_backend'], sparse_attention: ['sparse_method','sparse_keep_percent','sparse_start','sparse_end','sparse_min_tokens','sparse_extra_tokens','sparse_sink_conditioning'], ff_chunking: ['ff_chunk_size','ff_chunk_threshold'], low_vram_attention:['low_vram_chunks'], latent_upscaler: ['upscaler_chunking', 'upscaler_force_unload'] })) {
      for (const key of keys) { options[key].disabled ||= features[feature] !== true; options[key].title = features[feature] === true ? '' : 'Unavailable in the installed Seed Hunter runtime.'; }
    }
    for (const control of [options.output_fps, finalFields.output_fps]) control.querySelector('option[value="48"]').disabled = features.rife !== true;
    rifeStatus.textContent = finalRifeStatus.textContent = features.rife === true ? 'RIFE v4.26 installed · Choose 48 fps to enable frame interpolation after rendering. H3 generates at 24 fps; duration and audio stay unchanged.' : 'RIFE unavailable · 24 fps needs no interpolation model. 48 fps requires rife-v4.26.safetensors in models/hzwer/RIFE and the frame interpolation runtime.';
    uploads.reference_mods.input.disabled ||= features.refmods !== true;
    uploads.first_frame.wrap.hidden = !options.first_frame_guide.checked; uploads.last_frame.wrap.hidden = !options.last_frame_guide.checked;
    uploads.reference_mods.wrap.title = features.refmods === true ? '' : 'Reference mods are unavailable in the installed Seed Hunter runtime.';
    options.preview_count.disabled = single || locked; options.preview_count.parentElement.hidden = scout || single;
    if (scout) {
      stage.disabled = true; const fullPass=options.scout_mode.value==='final';options.scout_count.disabled ||= fullPass;options.scout_step.disabled ||= fullPass;options.scout_preview_mode.disabled ||= fullPass;options.scout_timeout.disabled ||= options.scout_mode.value!=='interactive';options.scout_step.max = Math.max(1, Number(fields.steps.value) - 1);
      options.scout_upscale.disabled ||= inventory?.seed_hunter?.features?.scout_upscale!==true||options.scout_mode.value==='scout';
      for(const key of ['scout_final_width','scout_final_height','scout_refine_steps','denoise'])options[key].disabled ||= !scoutUpscaling;
      for (const key of ['upscaler_chunking','upscaler_force_unload']) options[key].parentElement.hidden = !scoutUpscaling;
      const width=options.scout_final_width.value===''?Number(fields.width.value)*2:Number(options.scout_final_width.value),height=options.scout_final_height.value===''?Number(fields.height.value)*2:Number(options.scout_final_height.value);
      scoutUpscaleSummary.textContent=inventory?.seed_hunter?.features?.scout_upscale!==true?'Final latent upscaling needs the updated server and an installed H3 latent upscaler.':scoutUpscaling?`${fields.width.value} × ${fields.height.value} → ${width} × ${height}. The chosen seed finishes its remaining schedule first, then the learned upscaler runs with ${options.scout_refine_steps.value} refinement steps. Sampler and scheduler follow the controls below.`:'The final video keeps the scouting resolution.';
    }
    dialoguePanel.parentElement.hidden = stage.value !== 'dialogue'; audioPanel.parentElement.hidden = false; continuationPanel.parentElement.hidden = stage.value !== 'continuation'; uploads.source_video.wrap.hidden = stage.value !== 'continuation';
    const choiceCount = scout ? Math.max(1,Math.min(32,Number(options.scout_count.value)||3)) : single ? 1 : Math.max(1,Math.min(3,Number(options.preview_count.value)||1));
    fields.seed.max = Number.MAX_SAFE_INTEGER - (scout ? 1 : 1000) * (choiceCount - 1);
    duration.textContent = `${stage.value==='preview'?'Preview length: ':''}${(Number(fields.frames.value) / 24).toFixed(2)} seconds · seeds ${Array.from({ length: choiceCount }, (_, i) => Number(fields.seed.value) + i * (scout ? 1 : 1000)).join(', ')}`;
    fields.shift_video.disabled = fields.shift_audio.disabled = locked || !options.shift_enabled.checked;
    const automaticGenerationLength=batching&&batchQueue.autoVideoLength&&stage.value!=='preview';
    fields.frames.disabled ||= automaticGenerationLength;
    seconds.disabled=applyDuration.disabled=locked||enhancing||finalEdit||automaticGenerationLength;seconds.min=stage.value==='continuation'?'0.2':'3';
    if(document.activeElement!==seconds)syncDuration();
    if(batching&&batchQueue.autoVideoLength&&stage.value!=='preview')duration.textContent='Automatic: each batch video gets its own generation length after detection.';
    if(batching&&uploads[batchQueue.inputField]){const state=uploads[batchQueue.inputField];state.wrap.hidden=true;state.input.disabled=true;state.rangeEditors.forEach(editor=>editor.setDisabled(true));}
    fields.frames.min = stage.value === 'continuation' ? '5' : '73'; fields.width.min = fields.height.min = stage.value === 'dialogue' ? '64' : '256';
    loras.setDisabled(locked || loading || enhancing); loras.setLibraryAvailable(inventory?.lora_library === true && !jobEditor?.active);
    submit.disabled = locked || loading || enhancing || !inventory?.seed_hunter || !inventory?.runtime_ready || scout && inventory?.seed_hunter?.features?.seed_scout !== true || !!jobEditor?.blocked || (['continuation','dialogue'].includes(stage.value) && features[stage.value] !== true);
    submit.hidden=batching;
    submit.textContent = locked ? 'Submitting…' : jobEditor?.active ? 'Save queued job' : scout ? options.scout_mode.value==='final'?'Generate full video':options.scout_mode.value==='scout'?'Generate partial previews':'Start seed scout' : stage.value === 'preview' ? 'Generate previews' : stage.value === 'dialogue' ? 'Generate audio previews' : stage.value === 'continuation' ? 'Queue continuation' : 'Queue single pass';
    refresh.disabled = locked || loading; exportButton.disabled = importButton.disabled = locked || loading || !inventory || !!jobEditor?.active;
    const enhancerInputs=seedHunterEnhancerInputs(uploads,readOptions());enhancerInfo.textContent=enhancerInputs.issue;enhancerInfo.hidden=!enhancerInputs.issue;
    enhance.disabled = finalEdit || locked || enhancing || !enhancerSelect.value || !!enhancerInputs.issue; restorePrompt.hidden = previousPrompt === null;
    batchQueue?.setAvailable(!finalEdit&&!jobEditor?.active&&!jobEditor?.loading);
    batchQueue?.setServerFilesAvailable(inventory?.server_video_files===true);
    const batchBlock=busy||loading||enhancing||finalBusy||!!jobEditor?.active||!!jobEditor?.loading||!inventory?.runtime_ready;
    const batchReason=batchBlock?'Finish the current operation before adding a batch.':batchIssue();
    batchQueue?.setEnabled(!batchReason,batchReason,{snapshotEnabled:!batchBlock,snapshotReason:batchReason});
    const automatic = optionSnapshot.auto_refine, autoOn = !!automatic?.candidates?.length;
    autoBar.hidden = true; // Refinement is available only through explicit saved-job actions.
    autoConfigure.disabled = finalBusy || locked || loading || !!jobEditor?.active;
    autoOff.hidden = !autoOn; autoOff.disabled = autoConfigure.disabled;
    autoDescription.textContent = autoOn ? `Automatic: ${automatic.candidates==='all'?'all candidates':'candidates '+automatic.candidates.join(', ')} · ${automatic.project} · ${automatic.existing==='rerender'?'reprocess all':'skip existing'}. Each result keeps its own seed and inputs.` : 'Optional: choose candidates and configure refinement once before queuing a batch.';
    selectAllPreviews.hidden = bulkRefine.hidden = features.auto_refine !== true;
    bulkRefine.textContent = `Refine selected jobs (${bulkSelected.size})…`; bulkRefine.disabled = !bulkSelected.size || finalBusy || !!bulkPending;
    retryBulk.hidden = !bulkPending; retryBulk.disabled = finalBusy;
    finishBulk.hidden = !bulkPending || !bulkPartialResult; finishBulk.disabled = finalBusy;
    queue?.setBlocked(locked || loading || finalBusy || !!jobEditor?.active); finalSubmit.disabled = finalBusy || (finalFields.output_fps.value==='48'&&features.rife!==true) || !selection || !finalFields.latent_upscaler.value || features.latent_upscaler !== true || features.separate_final_length===true&&!finalFields.frames.value&&!(selection?.automatic&&autoSettings.inherit.checked); finalLoras.setDisabled(finalBusy); for(const n of Object.values(finalFields))n.disabled=finalBusy;finalFields.frames.disabled||=features.separate_final_length!==true;finalFields.frames.parentElement.hidden=features.separate_final_length!==true;finalReroll.disabled=cancelSelection.disabled=finalBusy;
    const bulkMode=!!selection?.automatic, pendingBulk=bulkMode && !!bulkPending;
    autoSettings.node.hidden=!bulkMode; autoSettings.setDisabled(finalBusy || pendingBulk);
    if(pendingBulk){ for(const n of Object.values(finalFields))n.disabled=true; finalLoras.setDisabled(true); finalSubmit.disabled=finalBusy; }
    finalFields.seed.parentElement.hidden=finalReroll.hidden=bulkMode; finalFields.seed.disabled ||= bulkMode;
    if(bulkMode){finalFields.frames.disabled ||= autoSettings.inherit.checked; finalFields.frames.parentElement.hidden ||= scout;}
    finalSubmit.textContent = pendingBulk ? 'Retry bulk refinement submission' : bulkMode ? selection.jobs?.length ? 'Queue bulk refinements' : 'Save automatic refinement settings' : 'Render selected preview';
    if (jobNodes.size) renderJobs();
  }
  function fill(n, items, selected, blank = false) { selected=seedHunterComponentId(selected,items); n.replaceChildren(); if (blank) n.add(new Option('Use model GPU', '')); for (const item of items) n.add(new Option(displayName(item), item.id)); if(selected!==undefined&&selected!==null){if(![...n.options].some(o=>o.value===String(selected)))n.add(new Option('Choose an available component',selected));n.value=selected;} }
  async function loadInventory() {
    if (closed) return; loading = true; update();
    try {
      const data = await request('/inventory'); if (closed) return;
      const first = !inventory, serverDefaults = (scout ? data.seed_hunter?.scout_defaults : data.seed_hunter?.defaults) || {}, before = first ? { ...base, ...serverDefaults, steps:20, loras:[], ...(saved.config || {}), seed_hunter: { ...defaults, ...(serverDefaults.seed_hunter || {}), ...(scout ? SEED_SCOUT_OPTIONS : {}), ...(saved.config?.seed_hunter || {}) } } : config(); inventory = data;
      if(scout&&first&&before.seed_hunter.scout_upscale===undefined)before.seed_hunter.scout_upscale=data.seed_hunter?.features?.scout_upscale===true;
      optionSnapshot = clone(before.seed_hunter || {});
      for (const role of ['model','encoder','video_vae','audio_vae','latent_upscaler']) fill(fields[role], (data.components || []).filter(c => c.role === role && (role !== 'model' || c.variant === 'ref2va')), before[role] || (scout&&role==='latent_upscaler'?data.components.find(c=>c.role===role)?.id:undefined));
      fill(fields.gpu, data.gpus || [], before.gpu || data.gpus?.find(g => /5090/.test(g.name))?.id); fill(fields.vae_gpu, data.gpus || [], before.vae_gpu, true);
      for (const key of OUTER.filter(key => !['model','encoder','video_vae','audio_vae','latent_upscaler','gpu','vae_gpu'].includes(key))) if (before[key] !== undefined) fields[key].value = before[key];
      for (const key of Object.keys(options)) { const n = options[key], value = before.seed_hunter?.[key] ?? defaults[key] ?? n.value; if (n.type === 'checkbox') n.checked = value; else n.value = key==='preview_count'?seedHunterPreviewCount(value):value ?? ''; }
      loras.setInventory(effectiveComponents(), data.max_loras || 8); loras.setValue(h3LoraStack(before));
      fill(finalFields.latent_upscaler, data.components.filter(c => c.role === 'latent_upscaler'), finalFields.latent_upscaler.value || saved.final_config?.latent_upscaler);
      if (first) { prompt.value = before.prompt || ''; for (const key of ['dialogue_text','dialogue_tone','dialogue_template']) if (saved[key] !== undefined) fields[key].value = saved[key]; }
      if (first) syncSizeControls(saved.sizing);
      if (first && retained) { restore(retained.config, retained.uploads); syncSizeControls(saved.sizing); }
      status.textContent = !data.seed_hunter ? `${title} needs the updated Odysseus server.` : scout && !data.seed_hunter.features?.seed_scout ? 'Seed Scout is not installed in this runtime.' : !data.runtime_ready ? data.runtime_error || 'H3 runtime unavailable.' : `${title} is ready.`;
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
      if (!closed) { status.textContent = editedJobId ? 'Changes saved to the queued job. Its queue position is unchanged, and your previous draft has been restored.' : 'Job added to the shared video queue. Your draft remains available.'; await loadJobs({ reset: !editedJobId }); }
    } catch (e) { fail(e); } finally { busy = false; if (!closed) update(); }
  });
  async function openAutomatic(selectedJobs = []) {
    if (busy || finalBusy || !inventory) return;
    if (bulkPending) { fail('Retry the pending bulk submission first to avoid duplicates.'); return; }
    try {
      const data = selectedJobs.length ? await request(`/jobs/${encodeURIComponent(selectedJobs[0].id)}/seed-hunter`) : {config:config()};
      const source = data.config, settings = source.seed_hunter?.auto_refine || optionSnapshot.auto_refine || {};
      const size = seedHunterResolution(2, source.width, source.height);
      const values = { ...size, steps:2, seed:source.seed, frames:source.frames, denoise:.4, sampler:'er_sde', scheduler:'linear_quadratic', latent_upscaler:source.latent_upscaler || finalFields.latent_upscaler.value, ...settings.refinement };
      selection = {automatic:true, jobs:selectedJobs.map(j=>j.id), width:source.width, height:source.height}; finalRequest=null;
      const count = Math.max(scout ? source.seed_hunter?.scout_count || 3 : source.seed_hunter?.preview_count || 3, ...selectedJobs.map(j=>j.scout?.seeds?.length || j.seed_hunter?.candidates?.length || 1));
      autoSettings.setValue(settings, count, scout);
      for(const [key,n] of Object.entries(finalFields)) if(values[key] !== undefined)n.value=values[key];
      finalFields.output_fps.value = String(settings.refinement?.seed_hunter?.output_fps ?? source.seed_hunter?.output_fps ?? 24);
      finalComponents=h3LoraComponents(inventory.components,data.lora_components);finalLoras.setInventory(finalComponents);finalLoras.setValue(settings.refinement?.loras || h3LoraStack(source));
      finalTitle.textContent=selectedJobs.length ? `Refine candidates from ${selectedJobs.length} jobs` : 'Automatic refinement for new jobs';
      finalSection.hidden=false; describeRefinement(); update(); pipeline?.select('refine'); finalSection.scrollIntoView({behavior:'instant',block:'start'});
    } catch(e){fail(e);}
  }
  async function submitAutomatic() {
    if(!bulkPending || finalBusy)return;
    finalBusy=true; save(); update();
    try {
      const result=await request('/auto-refine',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(bulkPending)});
      if(!result.jobs || !Number.isInteger(result.created))throw new Error('The server did not confirm the bulk submission.');
      const errors=Object.entries(result.errors||{});
      bulkPartialResult=errors.length>0;
      status.textContent=`${result.created} refinements queued · ${result.skipped} candidates already have refinements. Output folder: ${result.output_folder}`;
      if(errors.length)fail(errors.map(([id,message])=>`${id}: ${message}`).join(' · '));
      else {bulkPending=null;bulkSelected.clear();selection=null;finalSection.hidden=true;}
      save(); await loadJobs();
    } catch(e){fail(e.message+' Retry checks the same request and avoids duplicate jobs.');}
    finally{finalBusy=false;update();}
  }
  async function selectCandidate(job, candidate, remembered = false) {
    if (busy || finalBusy || !candidate.can_refine) return;
    const token = ++selecting;
    try {
      status.textContent = 'Loading saved preview settings…'; const data = await request(`/jobs/${encodeURIComponent(job.id)}/seed-hunter`); if (closed || token !== selecting) return;
      const source = data.config, size = seedHunterResolution(inventory.seed_hunter?.final_defaults?.megapixels || 2, source.width, source.height);
      selection = { job_id: job.id, index: candidate.index, seed: candidate.seed, width:source.width, height:source.height,frames:source.frames,final_length:data.final_length }; finalRequest = remembered ? saved.final_request || null : null;
      const defaults = seedHunterRefinementDefaults(source, { ...size, latent_upscaler:source.latent_upscaler||finalFields.latent_upscaler.value, steps: 2, seed: candidate.seed, sampler: 'er_sde', scheduler: 'linear_quadratic', denoise: .4, ...(inventory.seed_hunter?.final_defaults || {}),...(data.refinement_defaults||{}),frames:data.refinement_defaults?.frames??source.seed_hunter?.final_frames??source.frames }, remembered ? saved.final_config || {} : {});
      for (const [key, n] of Object.entries(finalFields)) if (defaults[key] !== undefined) n.value = defaults[key];
      finalFields.output_fps.value = String((remembered ? saved.final_config?.seed_hunter?.output_fps : undefined) ?? source.seed_hunter?.output_fps ?? 24);
      if(data.final_length?.requires_manual&&!remembered)finalFields.frames.value='';
      finalComponents = h3LoraComponents(inventory.components, data.lora_components); finalLoras.setInventory(finalComponents); finalLoras.setValue(remembered && saved.final_config?.loras ? saved.final_config.loras : h3LoraStack(source));
      finalTitle.textContent = `Refine preview ${candidate.index} · seed ${candidate.seed}`;describeRefinement();finalSection.hidden = false;
      status.textContent = 'Preview selected. Review refinement settings before rendering.'; update(); save(); renderJobs(); if (!remembered) { pipeline?.select('refine'); finalSection.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    } catch (e) { fail(e); }
  }
  finalForm.onsubmit = async event => {
    event.preventDefault(); if (finalSubmit.disabled || !selection) return;
    if(selection.automatic && bulkPending){await submitAutomatic();return;}
    if(!finalForm.reportValidity())return;
    const issue = h3LoraIssue(finalLoras.getValue(), finalComponents, 'ref2va'); if(issue){fail(issue);return;}
    const values=finalConfig();
    if(values.width<selection.width||values.height<selection.height||values.width*values.height>4194304){fail('Final dimensions must be at least the selected preview size, up to 4,194,304 pixels.');return;}
    if(selection.automatic){
      try {
        const settings=autoSettings.getValue(values);
        if(selection.jobs.length){bulkPending={jobs:selection.jobs,settings,request_id:crypto.randomUUID()};save();await submitAutomatic();}
        else {optionSnapshot.auto_refine=settings;selection=null;finalSection.hidden=true;changed();status.textContent='Automatic refinement settings saved for new jobs and batches.';}
      } catch(e){fail(e);} return;
    }
    const current = { candidate: selection.index, config: values };
    if (!finalRequest || finalRequest.source !== selection.job_id || JSON.stringify(finalRequest.payload) !== JSON.stringify(current)) finalRequest = { source: selection.job_id, payload: clone(current), request_id: crypto.randomUUID() };
    save(); finalBusy = true; update(); error.hidden = true;
    try { const result=await request(`/jobs/${encodeURIComponent(finalRequest.source)}/seed-hunter/finalize`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...finalRequest.payload, request_id: finalRequest.request_id }) }); if(!result.id&&!result.job?.id)throw new Error('The server did not confirm this refinement.'); finalRequest = null; save(); status.textContent = 'Refinement queued beneath its source preview. You can refine another seed too.'; await loadJobs(); }
    catch (e) { fail(`${e.message} Retry keeps the same submission ID.`); }
    finally { finalBusy = false; if (!closed) update(); }
  };
  const reruns = createVideoJobReruns({ family: 'h3', request, getBaseConfig: config,
    isBaseBlocked: () => loading || enhancing || !inventory || !!jobEditor?.active || !!jobEditor?.loading || !!queue?.busy || !!queue?.editing,
    onChange: () => renderJobs(), onQueued: () => loadJobs(), isClosed: () => closed, isBlocked: () => busy || finalBusy });
  async function stopJob(job) { try { await request(`/jobs/${job.id}`, { method: 'DELETE' }); await loadJobs(); } catch (e) { fail(e); } }
  function renderJobs() {
    jobStatus.textContent = `${jobPager?.counts.running || 0} rendering · ${jobPager?.counts.queued || 0} queued`;
    for (const [id, node] of jobNodes) if (!jobs.some(j => j.id === id)) { releaseVideoJobMedia(node.card); node.card.remove(); jobNodes.delete(id); }
    for (const [index, job] of jobs.entries()) {
      let nodes = jobNodes.get(job.id);
      if (!nodes) {
        const card = make('article', 'h3-video-job'), heading = make('h4'), line = make('p', 'h3-video-muted'), tools = make('div', 'h3-video-job-actions'), stop = button('Stop'), retry = button('Retry'), baseRerun = button(BASE_RERUN_LABEL), edit = button('Edit queued job'), editRerun = button('Edit & rerun'), remove = button('Delete job');
        const jobError = make('p', 'h3-video-error'), candidates = make('div', 'h3-seed-hunter-candidates'), output = make('div', 'h3-video-output'), details = make('details'), log = make('pre', 'h3-video-log'), promptDetails = make('details'), usedPrompt = make('pre', 'h3-video-prompt-used');
        card.dataset.jobId = job.id;
        const bulkCheck = make('input'); bulkCheck.type='checkbox'; bulkCheck.setAttribute('aria-label','Select job for bulk refinement');
        const bulkLabel=make('label','',' Select for bulk refinement');bulkLabel.prepend(bulkCheck);card.prepend(bulkLabel);
        bulkCheck.onchange=()=>{if(bulkCheck.checked)bulkSelected.set(job.id,jobs.find(j=>j.id===job.id)||job);else bulkSelected.delete(job.id);update();};
        const autoStatus=make('p','h3-video-muted');card.append(autoStatus);
        const originals = createVideoJobSources();
        const rerunStatus = make('p', 'h3-video-muted'); rerunStatus.hidden = true; rerunStatus.setAttribute('role', 'status');
        details.append(make('summary','','Runtime log'), log); promptDetails.append(make('summary','','Prompt used'),usedPrompt); tools.append(stop,retry,baseRerun,edit,editRerun,remove); card.append(heading,line,tools,rerunStatus,jobError,originals.node,candidates,output,promptDetails,details); jobList.append(card);
        const deletion = make('div','h3-video-delete-confirmation'), keep = button('Keep job'), confirm = button('Delete permanently'); deletion.hidden = true; deletion.append(make('p','','Delete this job and its saved previews, latents and outputs?'),keep,confirm); card.append(deletion);
        nodes = {bulkCheck,bulkLabel,autoStatus,card,heading,line,stop,retry,baseRerun,rerunStatus,edit,editRerun,remove,jobError,originals,candidates,output,log,usedPrompt,previews:new Map(),deletion}; jobNodes.set(job.id,nodes);
        baseRerun.title = BASE_RERUN_HELP; baseRerun.onclick = () => reruns.runBase(jobs.find(j => j.id === job.id) || job);
        retry.title = 'Queue this job again with its original prompt, settings and saved files.';
        editRerun.title = 'Edit this job’s saved settings before queuing a new copy. Changes apply only to that copy.';
        stop.onclick = () => stopJob(job); retry.onclick = () => reruns.run(jobs.find(j => j.id===job.id)||job); edit.onclick = () => jobEditor.open(job.id); editRerun.onclick = () => queue.openRerun(job.id,async(source,patch)=>{const result=await reruns.run(source,patch,true);return {result,...reruns.state(source)};});
        remove.onclick=()=>{deletion.hidden=false;};keep.onclick=()=>{deletion.hidden=true;};confirm.onclick=async()=>{confirm.disabled=true;try{const result=await request(`/jobs/${job.id}/record`,{method:'DELETE'});if(!result.deleted)throw new Error('Deletion was not confirmed.');if(selection?.job_id===job.id){selection=null;finalSection.hidden=true;save();}await loadJobs();}catch(e){fail(e);}finally{confirm.disabled=false;}};
      }
      if (jobList.children[index] !== nodes.card) jobList.insertBefore(nodes.card, jobList.children[index] || null);
      nodes.bulkLabel.hidden = inventory?.seed_hunter?.features?.auto_refine!==true || !autoRefineParent(job);
      nodes.bulkCheck.checked=bulkSelected.has(job.id);nodes.bulkCheck.disabled=finalBusy;
      const automation=job.auto_refine;
      nodes.autoStatus.textContent=automation?`Project: ${automation.project} · ${automation.status}. ${automation.error||automation.output_paths?.join(' · ')||automation.output_folder||''}`:'';nodes.autoStatus.hidden=!automation;
      nodes.originals.update(job.input_videos);
      if (isSeedScoutJob(job)) { if (!nodes.scout) { nodes.scout = createSeedScoutPreviews(job.id, { select: seed => request(`/jobs/${encodeURIComponent(job.id)}/scout/select`, { method: 'POST', headers: {'Content-Type':'application/json'}, body:JSON.stringify({seed}) }), render: async (seed, request_id) => { const result = await request(`/jobs/${encodeURIComponent(job.id)}/scout/render`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({seed,request_id}) }); await loadJobs(); return result; }, onError:fail }); nodes.candidates.append(nodes.scout.node); } nodes.scout.update(job); }
      const stageName=job.seed_hunter?.stage||'preview'; nodes.heading.textContent=`${stageName} · ${job.status} · ${job.id}`;nodes.line.textContent=[job.phase,job.total_steps?`Step ${job.step||0}/${job.total_steps}`:''].filter(Boolean).join(' · ');
      const state=reruns.state(job);nodes.retry.hidden=nodes.baseRerun.hidden=nodes.editRerun.hidden=state.hidden;nodes.retry.disabled=state.disabled;nodes.editRerun.disabled=state.disabled||state.pending;nodes.retry.textContent=state.label;nodes.baseRerun.disabled=state.baseDisabled;nodes.edit.hidden=job.status!=='queued';nodes.stop.hidden=!ACTIVE.has(job.status);nodes.remove.hidden=!TERMINAL.has(job.status);
      nodes.rerunStatus.textContent=state.message;nodes.rerunStatus.hidden=!state.message;
      nodes.jobError.textContent=state.error||job.error||'';nodes.jobError.hidden=!nodes.jobError.textContent;nodes.log.textContent=(job.log_tail||[]).join?.('\n')||job.log_tail||'';if(nodes.usedPrompt.textContent!==job.prompt)nodes.usedPrompt.textContent=job.prompt||'Prompt unavailable.';
      for(const candidate of job.seed_hunter?.candidates||[]){let c=nodes.previews.get(candidate.index);if(!c){const card=make('section','h3-seed-hunter-candidate'),label=make('h5'),choose=button('Use this preview'),download=make('a','memory-toolbar-btn','Download preview');const url=`${API}/jobs/${encodeURIComponent(job.id)}/seed-hunter/candidates/${candidate.index}/video`;const player=createVideoJobMedia(url,{label:`Play preview ${candidate.index}`});download.href=url;download.download=`h3-preview-${candidate.index}.mp4`;card.append(label,player.node,choose,download);nodes.candidates.append(card);c={card,label,video:player.media,choose};nodes.previews.set(candidate.index,c);choose.onclick=()=>selectCandidate(jobs.find(j=>j.id===job.id)||job,(jobs.find(j=>j.id===job.id)?.seed_hunter?.candidates||[]).find(p=>p.index===candidate.index)||candidate);}
        if(candidate.audio_url&&!c.audio){const link=make('a','memory-toolbar-btn','Download audio');link.href=`${API}/jobs/${encodeURIComponent(job.id)}/seed-hunter/candidates/${candidate.index}/audio`;const player=createVideoJobMedia(link.href,{type:'audio',label:'Play audio'});link.download=`h3-dialogue-${candidate.index}.flac`;c.card.append(player.node,link);c.audio=player.media;}
        c.label.textContent=`Preview ${candidate.index} · seed ${candidate.seed}`;c.choose.hidden=!candidate.can_refine;c.choose.disabled=finalBusy;c.card.classList.toggle('selected',selection?.job_id===job.id&&selection.index===candidate.index);c.choose.textContent=selection?.job_id===job.id&&selection.index===candidate.index?'Selected preview':'Use this preview';
        if (!c.refinements) {
          c.refinements = createVideoRefinements({ stop: stopJob, edit: item => jobEditor.open(item.id),
            retry: item => reruns.run(item), reprocessBase: item => reruns.runBase(item), rerun: item => queue.openRerun(item.id, async(source, patch) => { const result = await reruns.run(source, patch, true); return {result, ...reruns.state(source)}; }),
            remove: async item => { const result = await request(`/jobs/${item.id}/record`, {method:'DELETE'}); if (!result.deleted) throw new Error('Deletion was not confirmed.'); await loadJobs(); },
            state: item => reruns.state(item), onError: fail });
          c.card.append(c.refinements.node);
        }
        c.refinements.update((job.refinements || []).filter(item => item.seed_hunter?.source_candidate === candidate.index));
      }
      if (job.refinements?.length) {
        nodes.remove.textContent = 'Delete previews';
        nodes.deletion.querySelector('p').textContent = 'Delete these previews and their saved inputs? Existing refinements are kept and become separate history entries.';
      } else {
        nodes.remove.textContent = 'Delete job';
        nodes.deletion.querySelector('p').textContent = 'Delete this job and its saved previews, latents and outputs?';
      }
      if(job.status==='completed'&&stageName!=='preview'&&!(job.seed_hunter?.candidates?.length)&&!nodes.output.childElementCount){nodes.output.append(createVideoJobMedia(`${API}/jobs/${encodeURIComponent(job.id)}/video`).node);}
    }
  }
  function loadJobs(options = {}) { return jobPager.load(options); }
  refresh.onclick=loadInventory;refreshJobs.onclick=()=>loadJobs({reset:true});
  jobEditor=createVideoJobEditor({form,request,isClosed:()=>closed,isBusy:()=>busy||loading||finalBusy||!!batchQueue?.busy,getDraft:()=>({config:config(),uploads:Object.fromEntries(Object.entries(uploads).map(([key,s])=>[key,[...s.files]]))}),applyEdit:value=>{editingComponents=value.lora_components||null;restore(value.config,{},value.inputs);pipeline?.select('prepare');},restoreDraft:value=>{editingComponents=null;restore(value.config,value.uploads);},onChange:update,onError:fail});
  batchQueue=createVideoBatchQueue({family:'h3',recipe:'seed_hunter',sessionKey:(scout?'h3:seed_scout':'h3:seed_hunter')+(layout==='pipeline'?':pipeline':''),container:batchContainer,getSnapshot:batchSnapshot,onJob:()=>{if(!closed)loadJobs({reset:true});},onChange:()=>{if(!closed){revision++;update();}}});
  function nestedEditor(onChange, savedConfig) {
    const node=make('div','h3-video-grid'), rows=[];
    const allowed=savedConfig?.seed_hunter?.stage==='final'||!savedConfig&&!scout ? FINAL_OPTIONS : new Set(Object.keys(options).filter(k=>k!=='stage'&&options[k].type!=='hidden'&&(!scout||k!=='preview_count')));
    for(const key of allowed){const original=options[key];if(!original)continue;const wrap=make('label','h3-video-field'), enabled=make('input'), control=original.cloneNode(true);enabled.type='checkbox';enabled.setAttribute('aria-label',`Change ${key.replaceAll('_',' ')}`);control.removeAttribute('id');control.value=original.value;control.checked=original.checked;control.disabled=true;control.setAttribute('aria-label',`New ${key.replaceAll('_',' ')}`);wrap.append(make('span','',key.replaceAll('_',' ')),enabled,control);node.append(wrap);const row={key,enabled,control,unavailable:!!original.title};rows.push(row);enabled.onchange=()=>{control.disabled=!enabled.checked;onChange();};control.oninput=control.onchange=()=>{enabled.checked=true;onChange();};}
    return {node,setDisabled(value){for(const r of rows){r.enabled.disabled=value||r.unavailable;r.control.disabled=value||r.unavailable||!r.enabled.checked;}},setValue(value){for(const r of rows)if(value[r.key]!==undefined){if(r.control.type==='checkbox')r.control.checked=value[r.key];else r.control.value=r.key==='preview_count'?seedHunterPreviewCount(value[r.key]):value[r.key]??'';}},getValue(){return Object.fromEntries(rows.filter(r=>r.enabled.checked).map(r=>[r.key,r.control.type==='checkbox'?r.control.checked:r.control.type==='number'||['preview_count','continuation_overlap','output_fps'].includes(r.key)?r.control.value===''?null:Number(r.control.value):r.control.value]));}};
  }
  queue=createVideoQueueControls({container:queueContainer,historyContainer:jobToolbar,historyFilter:{recipe:'seed_hunter',stage:scout?'scout':'not_scout'},family:'h3',request,reload:loadJobs,isClosed:()=>closed,onChange:update,filterJobs:job=>scout?isSeedScoutJob(job):!isSeedScoutJob(job)&&(job.recipe==='seed_hunter'||!!job.seed_hunter),targetName:title,getFields:(savedConfig,savedJob)=>{
    const final=savedConfig?.seed_hunter?.stage==='final', shared=final||!savedConfig&&!scout;
    const keys=OUTER.filter(k=>k!=='mode'&&(shared?FINAL_FIELDS.has(k):scout||k!=='latent_upscaler'));
    return [videoQueueField('prompt','Prompt',fields.prompt,false)].concat(keys.map(key=>videoQueueField(key,key.replaceAll('_',' '),fields[key],NUMERIC.has(key)))).concat(!shared&&savedConfig?.reference_video_ranges?.length?[referenceVideoRangesField(savedConfig.reference_video_ranges)]:[]).concat([
      {key:'seed_hunter',label:'Seed Hunter settings',create:onChange=>nestedEditor(onChange,savedConfig)},
      {key:'loras',label:'LoRA stack',getSavedValue:h3LoraStack,create:onChange=>createH3LoraEditor({components:h3LoraComponents(inventory?.components||[],savedJob?.lora_components),value:loras.getValue(),idPrefix:'seed-hunter-queue',onChange})}]);
  }});
  exportButton.onclick=()=>showVideoWorkflowExportOptions(settings=>videoWorkflow.exportVideoWorkflow({family:'h3',config:config(),uploads:currentUploads(),...settings}),exportButton);
  importButton.onclick=()=>importInput.click();importInput.onchange=async()=>{const file=importInput.files?.[0];importInput.value='';if(!file||busy)return;const captured=revision;busy=true;update();try{const result=await videoWorkflow.importVideoWorkflow(file,{family:'h3',inventory});if(closed)return;if(captured!==revision)throw new Error('The draft changed while importing. Import again to keep your edits.');if(result.config.recipe!=='seed_hunter'||(result.config.seed_hunter?.stage==='scout')!==scout)throw new Error(`Choose a ${title} workflow for this panel.`);inventory=result.inventory||inventory;restore(result.config,result.uploads);save();status.textContent=[`${title} workflow imported. Review settings and inputs before submitting.`,...(result.warnings||[])].join(' ');}catch(e){fail(e);}finally{busy=false;if(!closed)update();}};
  async function checkEnhancer(){try{const data=await request('/prompt-enhancer');if(closed)return;const before=enhancer[Number(enhancerSelect.value)] || saved.enhancer || data.default;enhancer=data.models||[];enhancerSelect.replaceChildren(new Option('Choose enhancement model…',''),...enhancer.map((m,i)=>new Option(m.label||m.model,String(i))));enhancerSelect.value=String(enhancer.findIndex(m=>m.model===before?.model&&m.endpoint_id===before?.endpoint_id));update();}catch(e){enhancerSelect.replaceChildren(new Option('Enhancer unavailable',''));update();}}
  enhancerSelect.onchange=()=>{update();save();};
  enhance.onclick=async()=>{const model=enhancer[Number(enhancerSelect.value)];if(!model||enhance.disabled)return;const before=prompt.value,captured=revision;enhancing=true;enhancerController=new AbortController();update();try{const payload={recipe:'seed_hunter',prompt:before,mode:'ref2va',width:Number(fields.width.value),height:Number(fields.height.value),frames:Number(fields.frames.value),endpoint_id:model.endpoint_id,model:model.model,reference_counts:seedHunterEnhancerInputs(uploads,readOptions()).counts};const body=new FormData();body.append('config',JSON.stringify(payload));for(const [key,files] of Object.entries(seedHunterEnhancerInputs(uploads,readOptions()).images))for(const file of files)body.append(key,file,file.name);const result=await request('/enhance-prompt',{method:'POST',body,signal:enhancerController.signal});if(!closed&&captured===revision&&prompt.value===before){if(!result.prompt?.trim()||result.prompt.length>16000)throw new Error('Enhancement returned no usable prompt.');previousPrompt=before;prompt.value=result.prompt;changed();status.textContent='Prompt enhanced. Review it before generating.';}}catch(e){if(e.name!=='AbortError')fail(e);}finally{enhancing=false;if(!closed)update();}};
  restorePrompt.onclick=()=>{if(previousPrompt!==null){prompt.value=previousPrompt;previousPrompt=null;changed();}};checkModels.onclick=checkEnhancer;
  function onKey(event){if(document.querySelector('.settings-help-tooltip'))return;if(document.querySelector('.video-queue-confirmation[open],.video-server-files-overlay,.h3-lora-library-overlay,.video-workflow-export-overlay'))return;if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();}else if(event.key==='Tab'){const controls=[...dialog.querySelectorAll('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,a[href]')].filter(n=>n.getClientRects().length);if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus();}}}
  const close=bindMenuDismiss(overlay,()=>{if(scout&&!jobEditor?.active){save();seedScoutDraft({config:config(),uploads:currentUploads()},layout);}promptHistory?.destroy(); pipeline?.destroy();closed=true;disposeHelp();jobPager?.destroy();releaseVideoJobMedia(jobList);clearInterval(timer);window.removeEventListener('keydown',onKey,true);enhancerController?.abort();picker?.close();library?.close();queue?.destroy();batchQueue?.destroy();Object.values(uploads).forEach(state=>state.rangeEditors.forEach(editor=>editor.destroy()));overlay.remove();if(anchor?.isConnected)anchor.focus();},()=>false);
  closeButton.onclick=close;window.addEventListener('keydown',onKey,true);closeButton.focus();
  for(const [key,value]of Object.entries({...base,...(saved.config||{})}))if(fields[key])fields[key].value=value;
  for(const key of [...NESTED,...(scout?Object.keys(SEED_SCOUT_OPTIONS):[])]){const n=options[key],v=saved.config?.seed_hunter?.[key]??defaults[key];if(n.type==='checkbox')n.checked=v;else n.value=key==='preview_count'?seedHunterPreviewCount(v):v??'';}
  promptHistory = bindWorkflowPromptHistory(dialog, { workflow: scout ? 'seed_scout' : 'seed_hunter' });
  if (layout === 'pipeline') pipeline = createWorkflowPipeline({container:scroll,dialog,form,family:'h3',title,
    sections:[
      {id:'inputs',title:'Inputs',parent:form,nodes:[stage.parentElement,batchContainer,media]},
      {id:'prepare',title:'Prompt',parent:form,nodes:[prompt.parentElement,enhancerBar,enhancerInfo,dialoguePanel.parentElement,continuationPanel.parentElement,audioPanel.parentElement]},
      {id:'setup',title:'Model setup',parent:form,nodes:[componentGrid,loras.node]},
      {id:'generate',title:'Generate',parent:form,nodes:[dimensions,sizeTools,autoBar,...(scoutUpscale?[scoutUpscale.parentElement]:[]),advanced.parentElement]},
      {id:'review',title:scout?'Choose seed / Results':'Review',parent:scroll,nodes:[jobToolbar,jobStatus,jobPager.node,jobList,jobPager.bottomNode]},
      {id:'refine',title:'Refine',parent:scroll,nodes:[finalSection]},
    ],actions:[actions],queueNodes:[queueContainer],
    getInputs:()=>Object.values(uploads).filter(state=>!state.wrap.hidden).flatMap(state=>[...state.retained,...state.files]),
    getSummary:()=>({model:fields.model.selectedOptions[0]?.text,loras:loras.getValue().map(row=>({...row,name:effectiveComponents().find(item=>item.id===row.id)?.display_name||effectiveComponents().find(item=>item.id===row.id)?.name||row.id}))}),
    setLoras:value=>{loras.setValue(value);changed();},
  });
  update();loadInventory();loadJobs();checkEnhancer();const timer=setInterval(()=>{if(!document.hidden)loadJobs();},3000);
  return{overlay,close};
}
