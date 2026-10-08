"""Curated BFS video workflows, cached assets and private render supervision."""
import hashlib
import json
import math
import os
from pathlib import Path
import re

from src.h3_video import (H3JobManager, PROJECT_ROOT, JOB_ROOT as H3_JOB_ROOT,
                         cache_roots, gpu_inventory, runtime_error, _read)

JOB_ROOT = H3_JOB_ROOT.with_name('bfs')
REPOSITORY = 'Alissonerdx/BFS-Best-Face-Swap-Video'
UPLOAD_EXTENSIONS = {
    'identity_image': {'.jpg', '.jpeg', '.png', '.webp'},
    'source_video': {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'},
    'last_frame': {'.jpg', '.jpeg', '.png', '.webp'},
    'mask_video': {'.mp4', '.mov', '.m4v', '.webm', '.mkv', '.avi'},
}


def slot(key, label, pattern, required=True):
    return {'key': key, 'label': label, 'pattern': pattern, 'required': required}


def number(key, label, default, low, high, step=1):
    return dict(key=key, label=label, type='number', default=default, min=low, max=high, step=step)


def choice(key, label, default, options):
    return dict(key=key, label=label, type='select', default=default,
                options=[{'value': value, 'label': label} for value, label in options])


def definition(identity, label, family, source_workflow, adapter_paths, slots, *, notes=(), mask=False, last=False):
    h3 = family == 'h3'
    step, minimum, frames = (17, 5, 124) if h3 else (4, 1, 73) if family == 'wan22' else (8, 1, 121)
    controls = [number('width', 'Output width', 640, 256, 1920, 32),
                number('height', 'Output height', 384, 256, 1920, 32),
                number('frames', 'Maximum frames from source', frames, minimum, 362 if h3 else 241, step),
                number('start_seconds', 'Source start (seconds)', 0, 0, 86400, 0.01),
                number('steps', 'Steps', 40 if family == 'wan22' else 20 if h3 or family == 'ltx2' else 8, 2 if family == 'wan22' else 1, 100),
                number('seed', 'Seed', 42, 0, 4294967295),
                number('lora_scale', 'Head-swap strength', 1, 0.01, 4, 0.05),
                choice('audio_mode', 'Output audio', 'source', [('source', 'Keep source audio'), ('silent', 'Silent')]),
                choice('resize_mode', 'Fit source to output', 'contain', [('contain', 'Fit whole frame'), ('crop', 'Crop to fill')])]
    if h3:
        controls += [choice('sampler', 'Sampler', 'euler', [('euler', 'Euler'), ('res_multistep', 'Res multistep')]),
                     choice('scheduler', 'Scheduler', 'beta', [('beta', 'Beta'), ('simple', 'Simple'), ('normal', 'Normal')]),
                     number('shift_video', 'Video shift', 12, 0.01, 100, 0.01),
                     number('shift_audio', 'Audio shift', 3, 0.01, 100, 0.01),
                     number('speed_lora_scale', 'Speed LoRA strength', 1, 0, 4, 0.05),
                     choice('reference_size', 'Identity detail', 'match', [('match', 'Match output'), ('max', 'Maximum')])]
    else:
        controls += [number('cfg', 'Guidance', 5 if family == 'wan22' else 4 if family == 'ltx2' else 1, 1, 20, 0.1)]
    requirements = {'identity_image': {'required': True}, 'source_video': {'required': True},
                    'notes': list(notes) + ['The selected source segment is trimmed to the model’s frame grid at 24 fps.']}
    if last:
        requirements['last_frame'] = {'allowed': True, 'required': False}
    if mask:
        requirements['mask_video'] = {'allowed': True, 'required': True}
    return dict(id=identity, label=label, family=family, source_workflow=source_workflow,
                adapter_paths=adapter_paths, slots=slots, controls=controls,
                defaults={c['key']: c['default'] for c in controls}, source_requirements=requirements,
                frame_step=step, frame_min=minimum, fps=24)


H3_SLOTS = [slot('model', 'H3 Ref2VA transformer', r'minimax.*h3.*ref2va'),
            slot('encoder', 'H3 Qwen3-VL encoder', r'(?:qwen.*minimax.*h3|h3.*(?:qwen|encoder))'),
            slot('video_vae', 'H3 video VAE', r'minimax.*h3.*video.*vae'),
            slot('audio_vae', 'H3 audio VAE', r'minimax.*h3.*audio.*vae'),
            slot('lora', 'BFS H3 head-swap LoRA', r'minimax.*h3.*head.swap'),
            slot('speed_lora', 'H3 speed LoRA (optional)', r'minimax.*h3.*taomate', False)]


def ltx_slots(version, adapter):
    family = version.replace('.', '[._-]?')
    encoder = r'gemma.*(?:3.*12|12.*3)' if version != '2.5' else r'gemma.*4.*12.*ltx.*2[._-]?5'
    slots = [slot('model', 'LTX-' + version + (' distilled' if version != '2' else '') + ' transformer', rf'ltx[._-]?{family}.*' + (r'distilled.*' if version != '2' else r'(?:transformer|dev).*')),
             slot('encoder', 'Gemma encoder', encoder),
             slot('video_vae', 'LTX-' + version + ' video VAE', rf'ltx[._-]?{family}.*video.*vae'),
             slot('audio_vae', 'LTX-' + version + ' audio VAE', rf'ltx[._-]?{family}.*audio.*vae'),
             slot('lora', 'BFS head-swap LoRA', adapter)]
    if version != '2.5':
        slots += [slot('connector', 'LTX text projection', rf'ltx[._-]?{family}.*(?:projection|embeddings.connector)')]
    return slots


WORKFLOWS = [
    definition('h3_head_swap', 'MiniMax H3 · Head swap', 'h3', 'h3/minimax_h3_head_swap_workflow.json',
               ['h3/minimax_h3_head_swap_v1.0_r32.safetensors'], H3_SLOTS),
    definition('ltx2_v1', 'LTX-2 · First-frame head swap', 'ltx2', 'workflows/workflow_ltx2_head_swap_drag_and_drop.json',
               ['ltx-2/head_swap_v1_13500_first_frame.safetensors', 'ltx-2/head_swap_v1_8750_first_and_last_frame.safetensors'],
               ltx_slots('2', r'head.swap.v1.(?:13500|8750)'), last=True,
               notes=['Supply an already head-swapped first frame as the identity image; this version propagates that prepared identity through the source video.']),
    definition('ltx2_v2', 'LTX-2 · Masked head swap v2', 'ltx2', 'workflows/workflow_ltx2_head_swap_drag_and_drop_v2.0.json',
               ['ltx-2/head_swap_v2_multimodes.safetensors'], ltx_slots('2', r'head.swap.v2.multimodes'), mask=True,
               notes=['Supply a mask video matching the source segment: white covers the entire original head, black preserves the scene. The worker makes the required magenta guide.']),
    definition('ltx23_v3', 'LTX-2.3 · Persistent identity v3', 'ltx23', 'workflows/workflow_ltx2_head_swap_drag_and_drop_v3.0.json',
               ['ltx-2.3/head_swap_v3_rank_64.safetensors', 'ltx-2.3/head_swap_v3_rank_adaptive_fro_098.safetensors'],
               ltx_slots('2.3', r'head.swap.v3.rank')),
    definition('ltx25_v1', 'LTX-2.5 · Head swap v1', 'ltx25', 'workflows/workflow_ltx2.5_head_swap_drag_and_drop.json',
               ['ltx-2.5/head_swap_ltx25_r128_v1.safetensors', 'ltx-2.5/head_swap_ltx25_r64_v1.safetensors'],
               ltx_slots('2.5', r'head.swap.ltx25.r(?:128|64).v1\.safetensors$')),
    definition('ltx25_v11', 'LTX-2.5 · Head swap v1.1', 'ltx25', 'workflows/workflow_ltx2.5_v1.1_head_swap_drag_and_drop.json',
               ['ltx-2.5/head_swap_ltx25_r128_v1.1.safetensors', 'ltx-2.5/head_swap_ltx25_r64_v1.1.safetensors'],
               ltx_slots('2.5', r'head.swap.ltx25.r(?:128|64).v1.1\.safetensors$')),
    definition('wan22_head_swap', 'Wan 2.2 · Bernini head swap', 'wan22', 'workflows/workflow_wan22_bernini_head_swap_drag_and_drop_v1.0.json',
               ['wan22/headswap_bernini_r64_73f640_step3000_high_noise.safetensors', 'wan22/headswap_bernini_r64_73f640_step3000_low_noise.safetensors'],
               [slot('model', 'Bernini-R high-noise transformer', r'wan.*bernini.*high.noise'),
                slot('model_low', 'Bernini-R low-noise transformer', r'wan.*bernini.*low.noise'),
                slot('encoder', 'UMT5-XXL encoder', r'umt5.*xxl'), slot('video_vae', 'Wan 2.1 VAE', r'wan.*2[._-]?1.*vae'),
                slot('lora', 'BFS high-noise LoRA', r'headswap.bernini.*high.noise'),
                slot('lora_low', 'BFS low-noise LoRA', r'headswap.bernini.*low.noise')]),
]
BY_ID = {w['id']: w for w in WORKFLOWS}
for _workflow in WORKFLOWS:
    if _workflow['id'] == 'ltx25_v11':
        _workflow['defaults']['lora_scale'] = 0.8
        next(c for c in _workflow['controls'] if c['key'] == 'lora_scale')['default'] = 0.8


def helper_error(workflow, runtime, project):
    adapter = 'bfs_h3.py' if workflow['family'] == 'h3' else 'bfs_ltx_wan.py'
    if not (Path(project) / 'scripts' / adapter).is_file():
        return 'BFS inference adapter is missing from this installation'
    if workflow['family'] not in {'ltx23', 'ltx25'}:
        return ''
    try:
        manifest = json.loads((Path(project) / 'scripts/bfs_runtime_sources.json').read_text())
        for name, expected in manifest['files'].items():
            if hashlib.sha256((Path(runtime).parent / 'bfs_nodes' / name).read_bytes()).hexdigest() != expected:
                raise ValueError('Changed helper')
    except (OSError, ValueError, KeyError):
        return 'BFS helpers are missing or changed. Run venv/bin/python scripts/setup_bfs_runtime.py'
    return ''


def discover_components(roots):
    from src.model_library import component_aliases
    result, seen, known_ids, metadata_cache = [], set(), set(), {}
    patterns = [s['pattern'] for w in WORKFLOWS for s in w['slots']]
    for root in roots:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in {'blobs', 'refs'} and not Path(directory, d).is_symlink()]
            for name in sorted(files):
                if not name.lower().endswith('.safetensors') or not any(re.search(p, name, re.I) for p in patterns):
                    continue
                path = Path(directory, name)
                try:
                    resolved = path.resolve(strict=True)
                    if resolved in seen or not resolved.is_file() or resolved.stat().st_size < 16:
                        continue
                    seen.add(resolved)
                except OSError:
                    continue
                identity = hashlib.sha256(str(resolved).encode()).hexdigest()[:32]
                aliases = component_aliases(resolved, metadata_cache)
                if identity in known_ids:
                    continue
                known_ids.update([identity, *aliases])
                result.append({'id': identity, **({'aliases': aliases} if aliases else {}),
                               'name': name, 'path': str(path.absolute()), 'nvfp4': 'nvfp4' in name.lower()})
    return sorted(result, key=lambda c: (0 if c['nvfp4'] else 1 if 'int8' in c['name'].lower() else 2, c['name']))


def matches(component, spec, workflow):
    name = component['name']
    if not re.search(spec['pattern'], name, re.I):
        return False
    # The original LTX-2 must never accept newer, incompatible LTX-2.3/2.5 tensors.
    if workflow['family'] == 'ltx2' and re.search(r'ltx[._-]?2[._-]?[35]', name, re.I):
        return False
    if spec['key'] == 'model' and re.search(r'lora|vae|encoder|projection|connector|upscal', name, re.I):
        return False
    return True


def validate_config(raw, inventory, uploads):
    if not isinstance(raw, dict) or not isinstance(raw.get('workflow_id'), str) or raw['workflow_id'] not in BY_ID:
        raise ValueError('Choose an available BFS workflow')
    workflow = BY_ID[raw['workflow_id']]
    allowed = {'workflow_id', 'components', 'gpu', 'prompt'} | {c['key'] for c in workflow['controls']}
    if raw.keys() - allowed:
        raise ValueError('Unknown BFS setting: ' + sorted(raw.keys() - allowed)[0])
    if not inventory['runtime_ready']:
        raise ValueError(inventory['runtime_error'])
    listed = next((w for w in inventory.get('workflows', []) if w['id'] == workflow['id']), None)
    if listed and listed.get('runtime_error'):
        raise ValueError(listed['runtime_error'])
    choices = raw.get('components')
    if not isinstance(choices, dict) or choices.keys() - {s['key'] for s in workflow['slots']}:
        raise ValueError('Invalid BFS component selections')
    gpu = next((g for g in inventory['gpus'] if g['id'] == raw.get('gpu')), None)
    if not gpu:
        raise ValueError('Choose an available NVIDIA GPU')
    config = dict(workflow_id=workflow['id'], workflow_label=workflow['label'], family=workflow['family'], gpu=gpu['id'], fps=workflow['fps'])
    for spec in workflow['slots']:
        selected = choices.get(spec['key'])
        if not selected and not spec['required']:
            config[spec['key']] = None
            continue
        component = next((c for c in inventory['components'] if c['id'] == selected or selected in c.get('aliases', [])), None)
        if not component or not matches(component, spec, workflow) or not Path(component['path']).is_file():
            raise ValueError('Choose a compatible cached ' + spec['label'])
        if component['nvfp4'] and not gpu.get('nvfp4'):
            raise ValueError('NVFP4 requires the Blackwell GPU; choose your RTX 5090')
        config[spec['key']] = component['path']
    for control in workflow['controls']:
        value = raw.get(control['key'], control['default'])
        if control['type'] == 'select':
            if value not in [o['value'] for o in control['options']]:
                raise ValueError('Invalid ' + control['label'])
        else:
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not control['min'] <= value <= control['max']:
                raise ValueError('Invalid ' + control['label'])
            if control['step'] >= 1 and (int(value) != value or (value - control['min']) % control['step']):
                raise ValueError(control['label'] + ' does not match the supported step')
            if control['step'] >= 1:
                value = int(value)
        config[control['key']] = value
    if config['width'] * config['height'] > 768 * 1344:
        raise ValueError('Output dimensions exceed 1,032,192 pixels')
    for key in UPLOAD_EXTENSIONS:
        count = len(uploads.get(key, []))
        requirement = workflow['source_requirements'].get(key, {})
        if count > 1 or (count and not requirement) or (requirement.get('required') and count != 1):
            raise ValueError('Supply the required ' + key.replace('_', ' ') + ' for this workflow')
    if workflow['id'] == 'ltx2_v1':
        wants_last = '8750' in Path(config['lora']).name
        if bool(uploads.get('last_frame')) != wants_last:
            raise ValueError('The selected first-and-last adapter requires a last frame; the first-frame-only adapter does not accept one')
    prompt = raw.get('prompt', '')
    if not isinstance(prompt, str) or len(prompt) > 15000:
        raise ValueError('Prompt must be at most 15,000 characters')
    notes = prompt.strip()
    config['prompt_notes'] = notes
    if workflow['family'] == 'h3':
        from scripts.bfs_h3 import build_prompt
        config['prompt'] = build_prompt(notes)
    else:
        from scripts.bfs_ltx_wan import build_prompt
        config['prompt'] = build_prompt(workflow['id'], notes)
    if len(config['prompt']) > 16000:
        raise ValueError('Prompt including workflow instructions must be at most 16,000 characters')
    return config


class BFSJobManager(H3JobManager):
    def __init__(self, root=JOB_ROOT, **kwargs):
        kwargs.setdefault('worker', Path(kwargs.get('project', PROJECT_ROOT)) / 'scripts/bfs_video_worker.py')
        super().__init__(root, **kwargs)

    def inventory(self):
        components = discover_components(cache_roots(self.project))
        gpus = gpu_inventory()
        error = runtime_error(self.runtime, self.worker)
        workflows = []
        for workflow in WORKFLOWS:
            slots, missing = [], []
            for spec in workflow['slots']:
                candidates = [c['id'] for c in components if matches(c, spec, workflow)]
                slots.append({k: v for k, v in spec.items() if k != 'pattern'} | {'component_ids': candidates, 'default_id': candidates[0] if candidates else None})
                if spec['required'] and not candidates:
                    missing.append(spec['label'])
            specific_error = error or helper_error(workflow, self.runtime, self.project)
            workflows.append({**workflow, 'slots': slots, 'ready': not missing and not specific_error,
                              'runtime_error': specific_error,
                              'unavailable_reason': specific_error or ('Missing cached components: ' + ', '.join(missing) if missing else '')})
        return dict(workflows=workflows, components=components, gpus=gpus, runtime_ready=not error, batch_jobs=True,
                    runtime_error=error, defaults={'gpu': next((g['id'] for g in gpus if g.get('nvfp4')), gpus[0]['id'] if gpus else '')})

    def view(self, job_id, owner, **kwargs):
        result = super().view(job_id, owner, **kwargs)
        config = _read(self.directory(job_id) / 'manifest.json').get('config', {})
        result.update(workflow_id=config.get('workflow_id'), workflow_label=config.get('workflow_label'),
                      url=f'/api/video/bfs/jobs/{job_id}/video' if result['status'] == 'completed' else None)
        return result
