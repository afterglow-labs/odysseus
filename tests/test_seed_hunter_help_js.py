"""Shared desktop/native help covers the controls and describes selected choices."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = json.loads((ROOT / 'static/seed-hunter-help.json').read_text())


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    source = re.sub(r'^import .*;\n', '', (ROOT / 'static/js/seedHunterHelp.js').read_text(), flags=re.MULTILINE)
    result = subprocess.run([node, '--input-type=module'], input="import assert from 'node:assert/strict';\n" + source + '\nconst catalogue=' + json.dumps(CATALOGUE) + ';\n' + script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_catalogue_covers_all_seed_hunter_options_and_refinement_controls():
    from scripts.h3_seed_hunter_controls import DEFAULTS
    fields = CATALOGUE['fields']
    assert CATALOGUE['version'] == 1
    assert set(DEFAULTS) - {'refmod_strengths'} <= fields.keys()
    assert {'refmod_strength', 'model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler',
            'gpu', 'vae_gpu', 'prompt', 'width', 'height', 'frames', 'steps', 'seed',
            'sampler', 'scheduler', 'shift_video', 'shift_audio', 'reference_size',
            'final_width', 'final_height', 'final_steps', 'final_seed', 'final_denoise',
            'final_sampler', 'final_scheduler', 'final_loras', 'final_latent_upscaler',
            'batch_job', 'batch_input', 'auto_video_length', 'reference_start',
            'reference_duration', 'loras', 'lora_strength'} <= fields.keys()
    for entry in fields.values():
        assert entry['title'] and entry['body']
        for choice in entry.get('choices', {}).values():
            assert choice['title'] and choice['body']


def test_selected_sampler_and_scheduler_help_changes_without_losing_alternatives():
    run_js("""
const simple=seedHunterHelpText(catalogue,'scheduler','simple');
const normal=seedHunterHelpText(catalogue,'scheduler','normal');
assert.match(simple,/Selected: Simple\\n/);assert.match(normal,/Selected: Normal\\n/);
assert.match(simple,/model’s stored noise-level table/);assert.match(normal,/timestep range/);
assert.match(simple,/Linear quadratic/);assert.match(normal,/Beta/);
assert.match(seedHunterHelpText(catalogue,'sampler','euler'),/Selected: Euler/);
assert.match(seedHunterHelpText(catalogue,'final_sampler','res_multistep'),/previous denoising predictions/);
assert.equal(seedHunterHelpText(catalogue,'unknown',''), '');
""")


def test_dynamic_input_help_resolves_media_loras_batch_and_queue_fields():
    run_js("""
const field=(id='',label='',attributes=[])=>({id,getAttribute:k=>k==='aria-label'?label:null,hasAttribute:k=>attributes.includes(k)});
assert.equal(seedHunterHelpKey(field('seed-hunter-option-low_vram_chunks')),'low_vram_chunks');
assert.equal(seedHunterHelpKey(field('seed-hunter-final-scheduler')),'final_scheduler');
assert.equal(seedHunterHelpKey(field('seed-hunter-input-reference_audio')),'reference_audio');
assert.equal(seedHunterHelpKey(field('seed-hunter-final-lora_scale-2')),'lora_strength');
assert.equal(seedHunterHelpKey(field('seed-hunter-queue-lora')),'loras');
assert.equal(seedHunterHelpKey(field('','New sparse keep percent')),'sparse_keep_percent');
assert.equal(seedHunterHelpKey(field('','Start at (seconds) for clip.mp4')),'reference_start');
assert.equal(seedHunterHelpKey(field('','Start at (seconds or time) for clip.mp4')),'reference_start');
assert.equal(seedHunterHelpKey(field('','Choose a section for clip.mp4')),'reference_duration');
assert.equal(seedHunterHelpKey(field('','', ['data-batch-auto-length'])),'auto_video_length');
""")
