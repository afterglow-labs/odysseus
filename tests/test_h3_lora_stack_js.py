"""Ordered H3 adapters survive every client path without silently dropping one."""
from test_h3_video_js import run_js
from test_video_workflow_js import run_js as run_workflow_js
from test_video_queue_js import run_js as run_queue_js


def test_saved_stack_keeps_order_strengths_and_explicit_empty_over_new_preset():
    run_js(r'''
inventory.installed_preset={revision:'new-vfx',name:'VFX',config:{...defaults,lora:'vfx'}};
const stack=[{id:'turbo',strength:0.45},{id:'vfx',strength:1.2}];
stored.set(STORAGE,JSON.stringify({...defaults,loras:stack,steps:7}));
const ui=showH3Video();await flush();
assert.equal(byId('lora').value,'turbo');assert.equal(byId('lora-2').value,'vfx');
assert.equal(byId('lora_scale').value,'0.45');assert.equal(byId('lora_scale-2').value,'1.2');assert.equal(byId('steps').value,'7');
await change('prompt','Only this change');assert.deepEqual(batch.getSnapshot().config.loras,stack);
ui.close();stored.set(STORAGE,JSON.stringify({...defaults,lora:'vfx',loras:[],steps:33}));
showH3Video();await flush();assert.equal(byId('lora'),undefined);assert.equal(byId('steps').value,'33');
assert.deepEqual(editorOptions.getDraft().config.loras,[]);
''')


def test_refresh_keeps_missing_selection_visible_and_restores_when_drive_returns():
    run_js(r'''
stored.set(STORAGE,JSON.stringify({...defaults,loras:[{id:'turbo',strength:0.6},{id:'vfx',strength:1}]}));
showH3Video();await flush();await change('prompt','Glow');await attach('reference_videos',source());
const original=inventory.components;inventory.components=original.filter(item=>item.id!=='turbo');
await byText('Refresh components').onclick();assert.equal(byId('lora').value,'turbo');
assert.ok(byId('lora').options.some(option=>option.value==='turbo'&&option.textContent.includes('Unavailable')));
assert.equal(generate().disabled,true);assert.match(batch.issue,/unavailable/);
inventory.components=original;await byText('Refresh components').onclick();
assert.equal(generate().disabled,false);assert.equal(byId('lora_scale').value,'0.6');assert.equal(byId('lora-2').value,'vfx');
''')


def test_second_vfx_row_controls_source_recipe_only_when_active_and_checks_every_mode():
    run_js(r'''
stored.set(STORAGE,JSON.stringify({...defaults,loras:[{id:'turbo',strength:1},{id:'vfx',strength:0}]}));
showH3Video();await flush();assert.equal(byId('frames').disabled,false);
await change('lora_scale-2','0.8');assert.equal(byId('frames').disabled,true);
await change('lora_scale-2','0');assert.equal(byId('frames').disabled,false);
await change('lora-2','turbo');assert.equal(generate().disabled,true);assert.match(batch.issue,/only once/);
await change('lora-2','vfx');await change('mode','fl2va');assert.equal(generate().disabled,true);assert.ok(elements.some(item=>item.textContent.includes('requires REF2VA')));
await change('lora_scale','0');assert.equal(generate().disabled,false);
await change('lora_scale','4.1');assert.equal(generate().disabled,true);
''')


def test_queue_edit_cancel_and_batch_snapshot_preserve_stack_independently():
    run_js(r'''
const initial=[{id:'turbo',strength:0.7},{id:'vfx',strength:1.1}];
stored.set(STORAGE,JSON.stringify({...defaults,loras:initial}));
showH3Video();await flush();await change('prompt','Preserve me');await attach('reference_videos',source());
const draft=editorOptions.getDraft(),snapshot=batch.getSnapshot();
editor.active={id:'queued',revision:1};
editorOptions.applyEdit({config:{...defaults,loras:[{id:'turbo',strength:0.2}],prompt:'Job'},inputs:{reference_videos:[{index:0,name:'saved.mp4',size:7}]}});
assert.equal(byId('lora_scale').value,'0.2');assert.equal(byId('lora-2'),undefined);
editor.active=null;editorOptions.restoreDraft(draft);
assert.deepEqual(editorOptions.getDraft().config.loras,initial);assert.equal(byId('prompt').value,'Preserve me');
await change('lora_scale','0.4');assert.deepEqual(snapshot.config.loras,initial);
const remove=elements.find(item=>item.attributes['aria-label']==='Remove LoRA 2'&&connected(item));remove.onclick();
assert.deepEqual(editorOptions.getDraft().config.loras,[{id:'turbo',strength:0.4}]);
''')


def test_older_server_blocks_multiple_adapters_and_preset_explicitly_replaces_stack():
    run_js(r'''
inventory.lora_stack=false;stored.set(STORAGE,JSON.stringify({...defaults,loras:[{id:'turbo',strength:1},{id:'vfx',strength:1}]}));
inventory.installed_preset={revision:'vfx',name:'VFX',config:{...defaults,lora:'vfx'}};
showH3Video();await flush();assert.equal(generate().disabled,true);assert.match(batch.issue,/updated Odysseus server/);
byText('Use installed preset').onclick();assert.deepEqual(editorOptions.getDraft().config.loras,[{id:'vfx',strength:1}]);
assert.equal(generate().disabled,false);
''')


def test_portable_stack_maps_all_rows_and_marks_missing_adapter_without_dropping():
    run_workflow_js(r'''
const components=[{id:'new-vfx',role:'lora',name:'vfx.safetensors'},{id:'new-turbo',role:'lora',name:'turbo.safetensors'}];
const doc={format:'odysseus-video-workflow',version:1,family:'h3',config:{mode:'ref2va',lora_strengths:[0.5,1.2]},components:{lora_0:{name:'turbo.safetensors'},lora_1:{name:'vfx.safetensors'}}};
let result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components}});
assert.deepEqual(result.config.loras,[{id:'new-turbo',strength:0.5},{id:'new-vfx',strength:1.2}]);
assert.equal(result.config.lora_0,undefined);assert.equal(result.config.lora,'new-turbo');
result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:components.slice(0,1)}});
assert.deepEqual(result.config.loras,[{id:'unavailable:lora_0:turbo.safetensors',strength:0.5},{id:'new-vfx',strength:1.2}]);
assert.equal(result.warnings.length,1);
result=resolveVideoWorkflow({...doc,config:{mode:'ref2va',lora_strengths:[]},components:{lora:{name:'vfx.safetensors'}}},{family:'h3',inventory:{components}});
assert.deepEqual(result.config.loras,[]);
for(const strengths of ['wrong',[5],[NaN],Array(9).fill(1)])assert.throws(()=>resolveVideoWorkflow({...doc,config:{mode:'ref2va',lora_strengths:strengths}},{family:'h3',inventory:{components}}),/Invalid/);
assert.throws(()=>resolveVideoWorkflow({...doc,components:{lora_0:{name:'turbo.safetensors'}}},{family:'h3',inventory:{components}}),/Missing/);
''')


def test_bulk_patch_replaces_only_checked_stack_and_allows_explicit_removal():
    run_queue_js(r'''
const stack=[{id:'turbo',strength:0.65},{id:'vfx',strength:1.25}];
const rows=[{key:'loras',check:{checked:true},custom:{getValue:()=>stack}},{key:'steps',check:{checked:false},control:{value:7}}];
assert.deepEqual(videoQueuePatch(rows),{loras:stack});
rows[0].custom.getValue=()=>[];assert.deepEqual(videoQueuePatch(rows),{loras:[]});
rows[0].check.checked=false;assert.deepEqual(videoQueuePatch(rows),{});
''')
