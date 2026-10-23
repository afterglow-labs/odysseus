"""Resolve a shared refinement recipe independently for each source candidate."""
from copy import deepcopy


def resolve(raw, patch, inventory, inputs, *, scout=False):
    from src.h3_video import validate_config
    from src.h3_seed_hunter import FINAL_OPTIONS
    from scripts.h3_auto_refine_controls import REFINEMENT_FIELDS
    if not isinstance(patch, dict) or patch.keys() - REFINEMENT_FIELDS:
        raise ValueError('Each candidate keeps its own seed; unsupported refinement setting')
    options = patch.get('seed_hunter', {})
    if not isinstance(options, dict) or options.keys() - FINAL_OPTIONS:
        raise ValueError('Unsupported refinement option')
    if scout and any(k in patch and patch[k] != raw[k] for k in ('prompt', 'frames')):
        raise ValueError('Scout continuation keeps each candidate’s saved prompt and duration')
    result = deepcopy(raw)
    result.update(width=raw['width'] * 2, height=raw['height'] * 2,
                  steps=2, sampler='er_sde', scheduler='linear_quadratic')
    result.update({k: v for k, v in patch.items() if k not in {'seed_hunter', 'denoise'}})
    result['seed_hunter'].update(options)
    result['seed_hunter'].update(stage='final', preview_count=1)
    result['seed_hunter'].pop('auto_refine', None)
    if 'denoise' in patch:
        result['seed_hunter']['denoise'] = patch['denoise']
    if 'lora' in patch and 'loras' not in patch:
        result.pop('loras', None)
    resolved = validate_config(result, inventory['components'], inventory['gpus'], inputs)
    if resolved['width'] < raw['width'] or resolved['height'] < raw['height']:
        raise ValueError('Refinement dimensions must be at least each source preview’s dimensions')
    return resolved
