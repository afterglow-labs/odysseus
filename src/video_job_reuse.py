"""Owner-scoped references to full saved inputs, without a client round trip."""
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from src.h3_video import DEFAULTS, _locked, _read
from src.video_job_edit import _draft, _inputs, revision
from src.video_job_inputs import VIDEO_FIELDS, _owned_manifest
from src.video_server_files import MediaRoot, _open_selection, _selection
from src.video_workflow import specification

JOB_INPUT_ROOT = 'job-input'
JOB_OUTPUT_ROOT = 'job-output'


def _completed_output(manager, owner, job_id, version=None):
    directory, manifest = _owned_manifest(manager, job_id, owner, version)
    state = _read(directory / 'state.json')
    if state.get('status') != 'completed':
        raise HTTPException(409, 'Choose a completed video to extend')
    path = Path(manifest.get('output_path', ''))
    if path.is_symlink() or path.parent != directory.resolve() or not path.is_file():
        raise HTTPException(404, 'The completed video is unavailable')
    return directory, manifest, path


def open_reused_output(selection, *, manager, owner, extensions):
    parts = _selection(selection, extensions)
    if manager is None or not owner or len(parts) != 3:
        raise HTTPException(404, 'Saved video output is unavailable')
    job_id, version, name = parts
    if not version.isdecimal() or len(version) > 16:
        raise HTTPException(400, 'Invalid saved video output')
    directory, _ = _owned_manifest(manager, job_id, owner, int(version))
    with _locked(directory / 'job.lock'):
        directory, _, path = _completed_output(manager, owner, job_id, int(version))
        if path.name != name:
            raise HTTPException(409, 'The completed video changed. Select it again.')
        return _open_selection({**selection, 'root': 'saved', 'path': name},
                               [MediaRoot('saved', 'Saved video output', directory)], extensions)


def extend_video_draft(manager, owner, job_id):
    """Prepare continuation without creating a job or transferring media to the client."""
    directory, _ = _owned_manifest(manager, job_id, owner)
    inventory = manager.submission_inventory()
    with _locked(directory / 'job.lock'):
        directory, manifest, output = _completed_output(manager, owner, job_id)
        source = manifest['config']
        if source.get('workflow_id'):
            raise HTTPException(400, 'Open this video in the H3 workflow to continue it')
        raw = _draft(source, inventory, 'h3')
        # A two-step refinement schedule is not a generation schedule. Restore
        # the source candidate's generation count when extending a refinement.
        if source.get('seed_hunter', {}).get('stage') == 'final':
            metadata = manifest.get('seed_hunter_input', {}).get('metadata')
            path = Path(metadata) if isinstance(metadata, str) else None
            original = (_read(path).get('config', {}) if path and not path.is_symlink()
                        and path.parent == directory.resolve() and path.suffix == '.json' else {})
            defaults = inventory.get('seed_hunter', {}).get('defaults', {})
            raw['steps'] = source['seed_hunter'].get('full_length_steps') or original.get('steps') or defaults.get('steps', DEFAULTS['steps'])
            for key in ('sampler', 'scheduler'):
                raw[key] = original.get(key, defaults.get(key, raw[key]))
        if raw.get('mode') != 'ref2va':
            model = next((c for c in inventory['components'] if c.get('role') == 'model' and c.get('variant') == 'ref2va'), None)
            if model is None:
                raise HTTPException(400, 'Install a Reference-to-video H3 model to continue this video')
            raw['model'] = model['id']
        raw.update(recipe='seed_hunter', mode='ref2va')
        for key in ('director', 'h3md', 'model_fl2va', 'reference_video_ranges'):
            raw.pop(key, None)
        options = deepcopy(raw.get('seed_hunter', {}))
        options.update(stage='continuation', preview_count=1, source_start_seconds=0, source_end_seconds=None,
                       first_frame_guide=False, last_frame_guide=False, force_audio=False,
                       audio_start_seconds=0, audio_end_seconds=None, refmod_strengths=[])
        for key in ('auto_refine', 'final_frames', 'full_length_steps'):
            options.pop(key, None)
        raw['seed_hunter'] = options
        info = output.stat()
        selected = {'root': JOB_OUTPUT_ROOT, 'path': f'{job_id}/{revision(manifest)}/{output.name}',
                    'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}
        from src.h3_loras import frozen_components
        return {'family': 'h3', 'config': raw, 'uploads': {'source_video': [selected]},
                'lora_components': frozen_components(source, inventory),
                'notice': 'Completed video attached in full. Continue a video is selected. Review the prompt and extension length, then queue when ready.'}


def _saved(manager, owner, job_id, version, field, index):
    directory, manifest = _owned_manifest(manager, job_id, owner, version)
    if directory.is_symlink():
        raise HTTPException(404, 'Saved job input is unavailable')
    family = 'bfs' if manifest.get('config', {}).get('workflow_id') else 'h3'
    try:
        _, _, extensions = specification(family, manifest['config'])
        if field not in extensions:
            raise ValueError('Unknown input field')
        items = _inputs(directory, manifest, {field: extensions[field]})[field]
        if not 0 <= index < len(items):
            raise ValueError('Unknown input index')
        return directory, manifest, family, items[index]
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise HTTPException(404, 'The original job input is no longer available') from exc


def open_reused_input(selection, *, manager, owner, extensions):
    parts = _selection(selection, extensions)
    if manager is None or not isinstance(owner, str) or not owner or len(parts) != 5:
        raise HTTPException(404, 'Saved job input is unavailable')
    job_id, version, field, index, name = parts
    if not version.isdecimal() or not index.isdecimal() or len(version) > 16 or len(index) > 2:
        raise HTTPException(400, 'Invalid saved job input')
    # Ownership is checked before acquiring a lock or opening media.
    directory, _ = _owned_manifest(manager, job_id, owner, int(version))
    with _locked(directory / 'job.lock'):
        directory, _, _, item = _saved(manager, owner, job_id, int(version), field, int(index))
        if item['name'] != name:
            raise HTTPException(409, 'The original job input changed. Select it again.')
        path = Path(item['path'])
        upload = _open_selection({**selection, 'root': 'saved', 'path': path.name},
                                 [MediaRoot('saved', 'Saved job input', directory)], extensions)
        upload.filename = item['name']
        return upload


def reuse_video_draft(manager, owner, job_id, field, index, expected_revision):
    if field not in VIDEO_FIELDS or type(index) is not int or not 0 <= index < 17:
        raise HTTPException(404, 'Original video not found')
    directory, _ = _owned_manifest(manager, job_id, owner, expected_revision)
    inventory = manager.submission_inventory()
    with _locked(directory / 'job.lock'):
        directory, manifest, family, item = _saved(manager, owner, job_id, expected_revision, field, index)
        config = deepcopy(manifest['config'])
        if config.get('recipe') == 'seed_hunter' and config.get('seed_hunter', {}).get('stage') == 'final':
            # A fresh draft must not inherit a refinement's saved latent or
            # final-resolution sampling stage. Prefer its frozen preview config.
            metadata = manifest.get('seed_hunter_input', {}).get('metadata')
            path = Path(metadata) if isinstance(metadata, str) else None
            original = (_read(path).get('config', {}) if path and not path.is_symlink()
                        and path.parent == directory.absolute() and path.suffix == '.json' else {})
            if original.get('recipe') == 'seed_hunter' and original.get('seed_hunter', {}).get('stage') != 'final':
                config = deepcopy(original)
            else:
                config['seed_hunter']['stage'] = 'preview'
                config['seed_hunter']['preview_count'] = 3
                config['seed_hunter']['final_frames'] = config.get('frames', 362)
                config['frames'] = manifest.get('seed_hunter_source', {}).get('preview_frames', 107)
        if config.get('recipe') == 'director' and config.get('director', {}).get('stage') == 'refine':
            config['director']['stage'] = 'generation'
        config.pop('reference_video_ranges', None)
        if isinstance(config.get('seed_hunter'), dict):
            config['seed_hunter'].pop('auto_refine', None)
            config['seed_hunter'].update(source_start_seconds=0, source_end_seconds=None)
            config['seed_hunter']['refmod_strengths'] = []
            if config['seed_hunter'].get('stage') == 'scout' and config['seed_hunter'].get('scout_mode') == 'final':
                config['seed_hunter']['scout_mode'] = 'interactive'
        draft = _draft(config, inventory, family)
        info = Path(item['path']).stat()
        selected = {'root': JOB_INPUT_ROOT,
                    'path': f"{job_id}/{revision(manifest)}/{field}/{index}/{item['name']}",
                    'size': info.st_size, 'mtime_ns': str(info.st_mtime_ns)}
        result = {'family': family, 'config': draft, 'uploads': {field: [selected]},
                  'notice': 'Full original video attached. Choose a section and review the settings before submitting.'}
        if family == 'h3':
            from src.h3_loras import frozen_components
            result['lora_components'] = frozen_components(config, inventory)
        return result
