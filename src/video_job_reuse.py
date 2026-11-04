"""Owner-scoped references to full saved inputs, without a client round trip."""
from copy import deepcopy
from pathlib import Path

from fastapi import HTTPException

from src.h3_video import _locked, _read
from src.video_job_edit import _draft, _inputs, revision
from src.video_job_inputs import VIDEO_FIELDS, _owned_manifest
from src.video_server_files import MediaRoot, _open_selection, _selection
from src.video_workflow import specification

JOB_INPUT_ROOT = 'job-input'


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
