"""The Studio organizes the live H3 draft; it never submits a display copy."""
from test_h3_video_js import run_js


def test_studio_receives_live_controls_and_existing_form_ownership():
    run_js(r"""
showH3Video();await flush();
const sections=new Map(studioOptions.sections.map(section=>[section.id,section]));
assert.equal(studioOptions.form,form());
assert.equal(sections.get('inputs').parent,form());
assert.ok(sections.get('inputs').nodes.includes(byId('mode').parentElement));
assert.ok(sections.get('prepare').nodes.some(node=>node.children.includes(byId('prompt'))));
assert.ok(sections.get('setup').nodes.includes(byId('model').parentElement.parentElement));
assert.ok(sections.get('generate').nodes.includes(byId('steps').parentElement.parentElement));
assert.notEqual(sections.get('review').parent,form());
await change('lora','turbo');await change('lora_scale','0');
assert.deepEqual(studioOptions.getSummary().loras,[{name:'Turbo',strength:0,enabled:false}]);
await change('lora_scale','1.25');
assert.deepEqual(studioOptions.getSummary().loras,[{name:'Turbo',strength:1.25,enabled:true}]);
assert.equal(calls.filter(call=>call.method==='POST'&&call.url.endsWith('/jobs')).length,0);
""")


def test_converted_draft_preserves_device_and_clears_prior_media_without_queuing():
    run_js(r"""
showH3Video();await flush();
await change('prompt','Existing draft');await attach('reference_videos',source());
studioOptions.onApplyConverted({family:'h3',config:{...defaults,prompt:'Converted draft',gpu:'foreign-gpu',vae_gpu:'foreign-vae',steps:8,loras:[{id:'turbo',strength:.5}]}});
assert.equal(byId('prompt').value,'Converted draft');assert.equal(byId('steps').value,'8');
assert.equal(byId('gpu').value,'gpu');assert.equal(byId('vae_gpu').value,'');
assert.equal(studioOptions.getSummary().inputCount,0);
assert.equal(studioSelection,'setup');
assert.equal(calls.filter(call=>call.method==='POST'&&call.url.endsWith('/jobs')).length,0);
assert.throws(()=>studioOptions.onApplyConverted({family:'h3',config:{recipe:'seed_hunter'}}),/matching workflow panel/);
assert.equal(byId('prompt').value,'Converted draft');
editor.active={id:'editing',revision:2};
assert.throws(()=>studioOptions.onApplyConverted({family:'h3',config:{prompt:'Must not replace editing job'}}),/Finish the current edit/);
assert.equal(byId('prompt').value,'Converted draft');
""")


def test_queued_editor_reveals_prompt_stage_without_submitting_a_job():
    run_js(r"""
showH3Video();await flush();studioSelection='review';
editorOptions.applyEdit({config:{...defaults,prompt:'Queued prompt'},inputs:{}});
assert.equal(studioSelection,'prepare');assert.equal(byId('prompt').value,'Queued prompt');
assert.equal(calls.filter(call=>call.method==='POST'&&call.url.endsWith('/jobs')).length,0);
""")
