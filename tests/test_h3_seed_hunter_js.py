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
assert.equal(seedHunterFrames(4),107);
for(let frames=73;frames<=362;frames+=17)assert.equal(seedHunterFrames(Number((frames/24).toFixed(3))),frames);
assert.equal(seedHunterFrames(6), 158);
assert.equal(seedHunterFrames(15), 362);
assert.deepEqual(seedHunterResolution(.4,9,16), {width:480,height:864});
assert.deepEqual(seedHunterResolution(2,480,864), {width:1088,height:1952});
assert.throws(()=>seedHunterResolution(0,9,16));
""")


def test_preview_count_is_capped_in_draft_restore_and_serialization():
    run_js("""
for(const [value,wanted] of [[94,3],[4,3],[3,3],[2,2],[1,1],[0,1],[-5,1],[2.8,3],[undefined,3]]){
 assert.equal(seedHunterPreviewCount(value),wanted);
 assert.equal(seedHunterConfig({mode:'ref2va'},{stage:'preview',preview_count:value},[]).seed_hunter.preview_count,wanted);
}
assert.equal(seedHunterConfig({mode:'ref2va'},{stage:'scout',scout_count:32,preview_count:1},[]).seed_hunter.scout_count,32);
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


def test_selected_preview_refinement_inherits_its_denoise_and_preserves_remembered_edits():
    run_js("""
const source={seed_hunter:{denoise:.65}},fallback={width:1088,height:1952,denoise:.4,steps:2};
const selected=seedHunterRefinementDefaults(source,fallback);
assert.equal(selected.denoise,.65);assert.equal(selected.steps,2);assert.equal(selected.width,1088);
assert.equal(seedHunterRefinementDefaults(source,fallback,{denoise:.25}).denoise,.25);
assert.equal(seedHunterRefinementDefaults({seed_hunter:{denoise:0}},fallback).denoise,0);
assert.equal(seedHunterRefinementDefaults({},fallback).denoise,.4);
assert.equal(seedHunterRefinementDefaults({},{}).denoise,.4);
assert.equal(source.seed_hunter.denoise,.65);assert.equal(fallback.denoise,.4);
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


def test_queued_save_uses_registered_patch_route_and_keeps_multipart_body():
    # Check the actual backend registration too: the /edit URL is GET-only.
    route_source = (ROOT / 'routes/video_job_edit_routes.py').read_text()
    assert "@router.patch('/jobs/{job_id}')" in route_source
    assert "@router.get('/jobs/{job_id}/edit')" in route_source
    run_js("""
const calls=[],body=new FormData();body.append('revision','7');body.append('config','{"steps":3}');
const result=await saveSeedHunterQueuedJob(async(path,options)=>{calls.push({path,options});return {id:'job',revision:8};},'queued-job',body);
assert.equal(calls.length,1);assert.equal(calls[0].path,'/jobs/queued-job');assert.equal(calls[0].options.method,'PATCH');
assert.equal(calls[0].options.body,body);assert.equal(calls[0].options.body.get('revision'),'7');assert.equal(result.revision,8);
""")


def test_enhancer_sends_active_guide_roles_and_blocks_missing_retained_image_bytes():
    run_js("""
const first=new File(['first'],'first.png'),last=new File(['last'],'last.png'),reference=new File(['ref'],'ref.png');
const states={first_frame:{files:[first],retained:[]},last_frame:{files:[last],retained:[]},reference_images:{files:[reference],retained:[]},source_video:{files:[{name:'source.mp4'}]}};
const active=seedHunterEnhancerInputs(states,{stage:'continuation',first_frame_guide:true,last_frame_guide:false});
assert.deepEqual(active.images,{reference_images:[reference],first_frame:[first],last_frame:[]});
assert.equal(active.counts.first_frame,1);assert.equal(active.counts.last_frame,0);assert.equal(active.counts.source_video,1);assert.equal(active.issue,'');
states.first_frame={files:[],retained:[{index:0,name:'saved.png'}]};
assert.match(seedHunterEnhancerInputs(states,{first_frame_guide:true}).issue,/Reattach/);
assert.equal(seedHunterEnhancerInputs(states,{first_frame_guide:false}).issue,'');
""")


def test_each_seed_hunter_input_stage_has_batch_choices_except_derived_final():
    run_js("""
assert.deepEqual(seedHunterBatchChoices('preview').map(row=>row.field),['reference_videos','reference_images','reference_audio','copies']);
assert.deepEqual(seedHunterBatchChoices('single'),seedHunterBatchChoices('preview'));
assert.deepEqual(seedHunterBatchChoices('continuation').map(row=>row.field),['source_video']);
assert.deepEqual(seedHunterBatchChoices('dialogue').map(row=>row.field),['reference_audio','copies']);
assert.deepEqual(seedHunterBatchChoices('final'),[]);
""")


def test_automatic_video_batch_normalizes_a_short_prior_continuation_without_mutating_draft():
    run_js("""
for(const stage of ['preview','single']){
 const draft={recipe:'seed_hunter',frames:22,prompt:'Preserve me',seed_hunter:{stage,preview_count:3}};
 const automatic=seedHunterBatchConfig(draft,true);
 assert.equal(automatic.frames,73);assert.equal(draft.frames,22);assert.equal(automatic.prompt,draft.prompt);assert.deepEqual(automatic.seed_hunter,draft.seed_hunter);
 assert.equal(seedHunterBatchConfig(draft,false).frames,22);
 assert.equal(seedHunterBatchConfig({...draft,frames:158},true).frames,158);
}
assert.equal(seedHunterBatchConfig({frames:22,seed_hunter:{stage:'continuation'}},true).frames,22);
""")
