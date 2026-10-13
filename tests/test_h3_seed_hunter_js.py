"""Seed Hunter draft sizing, branch isolation and visible prompt composition."""
from pathlib import Path
import re
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    source = (ROOT / 'static/js/h3SeedHunter.js').read_text()
    source = re.sub(r'^import .*;\n', '', source, flags=re.MULTILINE)
    result = subprocess.run([node, '--input-type=module', '-e', "import assert from 'node:assert/strict';\n" + source + script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_author_workflow_rounds_duration_up_and_preserves_selected_aspect():
    run_js("""
assert.equal(seedHunterFrames(3), 73);
assert.equal(seedHunterFrames(6), 158);
assert.equal(seedHunterFrames(15), 362);
assert.deepEqual(seedHunterResolution(.4,9,16), {width:480,height:864});
assert.deepEqual(seedHunterResolution(2,480,864), {width:1088,height:1952});
assert.throws(()=>seedHunterResolution(0,9,16));
""")


def test_stage_count_and_lora_snapshots_do_not_mutate_ordinary_draft():
    run_js("""
const source={mode:'ref2va',width:480,height:864,frames:73,steps:6,seed:42,prompt:'Keep this visible'};
const options={stage:'preview',preview_count:3};const loras=[{id:'age',strength:-5}];
const preview=seedHunterConfig(source,options,loras);
const continued=seedHunterConfig(source,{...options,stage:'continuation'},loras);
assert.equal(preview.recipe,'seed_hunter');assert.equal(preview.seed_hunter.preview_count,3);
assert.equal(continued.seed_hunter.preview_count,1);assert.equal(options.preview_count,3);
continued.loras[0].strength=3;assert.equal(loras[0].strength,-5);
assert.equal(source.recipe,undefined);assert.equal(preview.prompt,source.prompt);
""")


def test_dialogue_build_is_explicit_text_substitution():
    run_js("""
assert.equal(seedHunterDialoguePrompt('Hello','Warmly','Before {{dialogue}} / {{tone}} after'),'Before Hello / Warmly after');
assert.equal(seedHunterDialoguePrompt('<literal>','', '{{dialogue}}'),'<literal>');
""")


def test_latent_upscaler_is_preserved_for_source_exports_and_refinement():
    run_js("""
const fields={mode:'ref2va',latent_upscaler:'upscaler',prompt:'Saved input'};
const preview=seedHunterConfig(fields,{stage:'preview'},[]);
const final=seedHunterConfig(fields,{stage:'final',refmod_strengths:[.5],sparse_keep_percent:25},[]);
assert.equal(preview.latent_upscaler,'upscaler');assert.equal(final.latent_upscaler,'upscaler');
assert.deepEqual(final.seed_hunter.refmod_strengths,[.5]);assert.equal(final.seed_hunter.sparse_keep_percent,25);
""")
