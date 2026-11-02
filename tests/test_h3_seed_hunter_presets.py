"""Preset recipes must survive real config resolution and stage boundaries."""
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest
import torch

from scripts import h3_director
from scripts.h3_seed_hunter_controls import validate_options
from scripts.h3_seed_hunter_presets import (CHARACTER_SWAP, CHARACTER_TURBO, LIGHTVAE,
    PIKA, PRISM8, TURBO4, TURBO8, candidate_stride, validate_sampling)
from src.h3_seed_hunter_presets import catalog, final_defaults
from src.h3_video import discover_components, validate_config
from tests.test_h3_video import weights, config, inventory


@pytest.fixture
def presets(tmp_path, inventory):
    for name in ("Minimax_h3_ref2va_pruned_int8.safetensors",
                 "Minimax-h3_Singularity_ref2va_v2_pruned_int8.safetensors", PRISM8,
                 LIGHTVAE, "minimax_h3_video_vae_fp16.safetensors", "minimax_h3_latent_upscaler.safetensors"):
        weights(tmp_path / name)
    for name in (TURBO4, TURBO8, PIKA, CHARACTER_SWAP, CHARACTER_TURBO):
        weights(tmp_path / name, lora=True)
    inventory['components'] = discover_components([tmp_path])
    return inventory, {row['id']: row for row in catalog(inventory['components'])}


@pytest.mark.parametrize('key,steps,sampler', [('quality',20,'er_sde'),('turbo4',4,'euler'),
    ('turbo8',8,'euler'),('pika',6,'lcm'),('lightvae',20,'er_sde'),
    ('director',16,'euler'),('prism8',8,'euler'),('character_swap',8,'er_sde')])
def test_catalog_resolves_installed_recipes_and_distinct_final_schedules(presets,key,steps,sampler):
    inventory, rows = presets
    row = rows[key]
    assert row['ready'], row['missing']
    raw = {**config(inventory, 'ref2va'), 'recipe':'seed_hunter', 'frames':73, **deepcopy(row['config'])}
    resolved = validate_config(raw, inventory['components'], inventory['gpus'], {})
    validate_sampling(resolved)
    assert resolved['steps'] == steps and resolved['sampler'] == sampler
    final = final_defaults(resolved, inventory['components'])
    assert final['steps'] == (20 if key == 'director' else 8 if key in {'character_swap','prism8'} else 2)
    assert final['seed_hunter']['sampling_schedule'] == 'standard'
    if key in {'turbo4','turbo8','pika'}:
        assert final['loras'] == []  # Do not keep a full-generation accelerator in the classic 2-step finish.
    if key == 'prism8':
        assert resolved['loras'] == []  # Turbo is already merged.
    if key == 'character_swap':
        assert [Path(v['path']).name for v in resolved['loras']] == [CHARACTER_SWAP, CHARACTER_TURBO]


def test_character_cross_task_exception_requires_the_character_adapter(presets):
    inventory, rows = presets
    raw = {**config(inventory, 'ref2va'), 'recipe':'seed_hunter', 'frames':73, **deepcopy(rows['character_swap']['config'])}
    raw['loras'] = raw['loras'][1:]
    with pytest.raises(ValueError, match='FL2VA LoRA'):
        validate_config(raw, inventory['components'], inventory['gpus'], {})


def test_pika_adapter_cannot_run_with_an_ordinary_hunter_schedule(presets):
    inventory, rows = presets
    raw = {**config(inventory, 'ref2va'), 'recipe':'seed_hunter', 'frames':73, **deepcopy(rows['pika']['config'])}
    raw['seed_hunter']['sampling_schedule'] = 'standard'
    with pytest.raises(ValueError, match='manual schedule'):
        validate_config(raw, inventory['components'], inventory['gpus'], {})


def test_director_timeline_scales_to_final_length_and_preserves_explicit_text():
    options = validate_options(dict(preset='director',preview_count=4,director_shots='2 | <Subject 1> walks.\n1 | Close-up.',director_music='No music.'))
    raw = dict(prompt='Use the provided references.',frames=144,seed_hunter=options)
    prepared = {'ref_images': {'ref_image_1': object()}, 'ref_videos': {'ref_video_1': object()}}
    text = h3_director.prompt(raw,prepared)
    assert '[Shot 2] At 00:04.000, Close-up.' in text
    assert '<Picture 1>' in text and '<Video 1>' in text
    assert 'No music.' in text and 'Use the provided references.' in text
    assert '00:08.000' in h3_director.prompt({**raw,'frames':288},prepared)
    assert candidate_stride(options) == 1_000_003
    with pytest.raises(ValueError): validate_options(dict(preview_count=4))
    with pytest.raises(ValueError): validate_options(dict(preset='director',director_shots='nan | Shot'))


def test_pixel_refinement_resizes_and_encodes_both_tracks_but_returns_original_audio(monkeypatch):
    audio = {'waveform': torch.tensor([[[.25,-.25]]]),'sample_rate':32000}
    calls = []
    module = ModuleType('comfy_extras.nodes_lt')
    module.LTXVConcatAVLatent = SimpleNamespace(execute=lambda video,sound: ({'video':video,'audio':sound},))
    monkeypatch.setitem(sys.modules, 'comfy_extras.nodes_lt',module)
    runtime = SimpleNamespace(decode=lambda *a: (torch.ones(73,16,16,3),audio),
        utils=SimpleNamespace(common_upscale=lambda frames,w,h,*a: calls.append((w,h)) or frames),
        audio=SimpleNamespace(VAEEncodeAudio=SimpleNamespace(execute=lambda vae,sound: (sound,))))
    latent, passthrough = h3_director.pixel_refinement(runtime,dict(width=960,height=544,frames=73),{},
        SimpleNamespace(encode=lambda frames:frames),'audio_vae',lambda *a:None)
    assert calls == [(960,544)]
    assert latent['audio'] is audio and passthrough is audio
    assert latent['video']['samples'].shape[0] == 73


def test_pika_schedule_cannot_silently_run_another_sampler():
    raw=dict(steps=6,sampler='lcm',seed_hunter=dict(sampling_schedule='pika',shift_enabled=False))
    validate_sampling(raw)
    with pytest.raises(ValueError): validate_sampling({**raw,'sampler':'euler'})
    with pytest.raises(ValueError): validate_sampling({**raw,'steps':20})
    with pytest.raises(ValueError, match='Seed Scout'): validate_sampling({**raw,'seed_hunter':dict(sampling_schedule='pika',stage='scout')})
    with pytest.raises(ValueError): validate_sampling({**raw,'seed_hunter':dict(sampling_schedule='pika',shift_enabled=True)})
