"""Prepare a single-change workflow from an owned, completed video result."""
from copy import deepcopy
from pathlib import Path
import subprocess

from src.h3_video import _read, _write, _locked, validate_config

PROMPT = """Edit <Video 1>. It is the sole source of truth for this shot.

Make only this requested change:
{change}

Preserve everything not explicitly named in that request: character identities, faces, expressions, body proportions, clothing outside the requested change, objects, positions, actions, choreography, scene order, camera motion, framing, lighting, colors, backgrounds, timing, duration, and visual style. Match the source shot moment by moment. Do not add a scene, cut, transition, camera move, dialogue, music, sound effect, or unrelated detail. Do not reinterpret or improve the story. The result must be the same shot with only the specified change.
{audio}"""


def prompt(change, preserve_audio=True):
    if not isinstance(change, str) or not change.strip() or len(change) > 12000:
        raise ValueError('Describe the single change in 1–12,000 characters')
    return PROMPT.format(change=change.strip(), audio=('Keep the original soundtrack and its exact timing.' if preserve_audio
                       else 'Change audio only if explicitly requested; otherwise match the original soundtrack and timing.'))


def _source(manager, identity, owner):
    manager._state(identity, owner)
    directory = manager.directory(identity)
    state, manifest = _read(directory / 'state.json'), _read(directory / 'manifest.json')
    if state.get('owner') != owner:
        raise FileNotFoundError('Video job not found')
    if state.get('status') != 'completed':
        raise ValueError('Choose a completed video result')
    source = Path(manifest.get('output_path', ''))
    if source.is_symlink() or source.parent != directory.resolve() or not source.is_file():
        raise FileNotFoundError('The completed video is unavailable')
    return directory, manifest, source


def probe(path):
    # Headers only: no frame decoding, GPU work or thumbnail generation.
    import av
    with av.open(str(path)) as container:
        video = container.streams.video[0]
        return {'width': video.width, 'height': video.height,
                'fps': float(video.average_rate or 24), 'has_audio': bool(container.streams.audio)}


def detail(manager, identity, owner):
    from src.video_job_edit import _draft
    from src.h3_loras import frozen_inventory
    directory, manifest, source = _source(manager, identity, owner)
    inventory = frozen_inventory(manifest['config'], manager.submission_inventory())
    raw = _draft(manifest['config'], inventory, 'h3')
    info = probe(source)
    if raw.get('mode') != 'ref2va':
        model = next((item for item in inventory['components'] if item.get('role') == 'model' and item.get('variant') == 'ref2va'), None)
        if not model:
            raise ValueError('Install a Ref2VA H3 model to edit a completed video')
        raw['model'] = model['id']
    raw.update(recipe='seed_hunter', mode='ref2va', width=info['width'], height=info['height'],
               steps=max(20, raw['steps']), sampler='euler', scheduler='simple')
    raw.pop('latent_upscaler', None)
    raw.pop('reference_video_ranges', None)
    options = raw.setdefault('seed_hunter', {})
    options.update(stage='single', preview_count=1, denoise=1., final_frames=None, scout_upscale=False,
                   first_frame_guide=False, last_frame_guide=False, force_audio=False,
                   source_start_seconds=0, source_end_seconds=None, audio_start_seconds=0,
                   audio_end_seconds=None, refmod_strengths=[], output_fps=48 if info['fps'] > 36 else 24)
    options.pop('auto_refine', None)
    # A fast preview/distillation or effect adapter is not a preservation preset.
    # Use the same base model with a full schedule; the user can add adapters later.
    raw.update(loras=[])
    raw.pop('lora', None); raw.pop('lora_scale', None)
    return {'config': raw, 'source_name': manifest.get('source_name') or source.name,
            'has_audio': info['has_audio'], 'prompt_template': PROMPT,
            'summary': f"{info['width']} × {info['height']} · {raw['frames'] / 24:.2f}s · {options['output_fps']} fps",
            'note': 'Uses the completed video as Video 1, the same base model, a full sampling schedule, and no LoRAs. Original audio is copied unless you turn it off.'}


def prepare(manager, identity, owner, body, destination, submission_id):
    from scripts.h3_seed_hunter import ffmpeg_binary
    from src.video_queue_bulk import _clone_file
    from src.h3_loras import frozen_inventory
    from src.h3_seed_hunter import preflight
    if not isinstance(body, dict) or body.keys() - {'change', 'preserve_audio', 'steps', 'request_id'}:
        raise ValueError('Invalid video edit request')
    preserve = body.get('preserve_audio', True)
    if type(preserve) is not bool:
        raise ValueError('Preserve audio must be true or false')
    instructions = prompt(body.get('change'), preserve)
    data = detail(manager, identity, owner)
    source_dir, manifest, source = _source(manager, identity, owner)
    raw = deepcopy(data['config']); raw['prompt'] = instructions
    if 'steps' in body:
        raw['steps'] = body['steps']
    destination = Path(destination)
    video = destination / 'edit-source.mp4'
    with _locked(source_dir / 'job.lock'):
        state = _read(source_dir / 'state.json')
        if state.get('owner') != owner or state.get('status') != 'completed' or not source.is_file() or source.is_symlink():
            raise ValueError('The completed result changed; refresh the job before editing')
        _clone_file(source, video)
    inputs = {'reference_videos': [str(video.resolve())]}
    names = {'reference_videos': [data['source_name']]}
    if preserve and data['has_audio']:
        audio = destination / 'edit-source-audio.flac'
        try:
            result = subprocess.run([ffmpeg_binary(), '-nostdin', '-v', 'error', '-i', str(video), '-map', '0:a:0',
                                     '-vn', '-c:a', 'flac', str(audio)], capture_output=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('Extracting the original soundtrack timed out; retry this edit') from exc
        if result.returncode:
            raise ValueError('Could not extract the original soundtrack for this edit')
        inputs['reference_audio'] = [str(audio.resolve())]
        names['reference_audio'] = ['Original soundtrack.flac']
        raw['seed_hunter']['force_audio'] = True
    inventory = frozen_inventory(manifest['config'], manager.submission_inventory())
    resolved = validate_config(raw, inventory['components'], inventory['gpus'], inputs)
    preflight(resolved, inventory, inputs)
    inputs['result_edit'] = {'source_job_id': identity, 'change': body['change'].strip(), 'preserve_audio': preserve,
                             'workflow_stage': manifest.get('result_edit', {}).get('workflow_stage') or manifest['config'].get('seed_hunter', {}).get('stage')}
    parent = manifest.get('seed_hunter_source', {})
    if parent.get('source_job_id'):
        inputs['seed_hunter_source'] = dict(parent)
    inputs['job_group_parent'] = parent.get('source_job_id') or identity
    _write(destination / 'submission.json', {'submission_id': submission_id,
           'input_names': names, 'source_name': data['source_name']})
    return resolved, inputs
