"""Ordered H3 adapters survive every client path without silently dropping one."""
from test_h3_video_js import run_js
from test_video_workflow_js import run_js as run_workflow_js
from test_video_queue_js import run_js as run_queue_js


AGE_COMPONENT = r'''
const ageName='MM-H3 - Ref2VA - Age Slider v2 - EXPERIMENTAL.safetensors';
const age={id:'age',role:'lora',variant:'ref2va',name:ageName,strength_control:{kind:'age',label:'Age',min:-5,max:5,step:0.1,default:0,labels:{min:'Youngest',zero:'Neutral',max:'Oldest'}}};
components.push(age);
'''


def test_age_slider_uses_signed_strength_and_keeps_other_adapter_limits():
    run_js(AGE_COMPONENT + r'''
stored.set(STORAGE,JSON.stringify({...defaults,loras:[{id:'turbo',strength:0.8}]}));
showH3Video();await flush();await change('prompt','Same scene');
byText('Add LoRA').onclick();await change('lora-2','age');
const slider=byId('lora_age-2'),number=byId('lora_scale-2');
assert.equal(slider.type,'range');assert.equal(slider.min,-5);assert.equal(slider.max,5);
assert.equal(slider.step,0.1);assert.equal(slider.parentElement.hidden,false);
assert.equal(number.value,'0');assert.equal(slider.value,'0');
slider.value='-5';await slider.fire('input');
assert.equal(number.value,'-5');assert.deepEqual(batch.getSnapshot().config.loras,[{id:'turbo',strength:0.8},{id:'age',strength:-5}]);
slider.value='5';await slider.fire('change');
assert.equal(number.value,'5');assert.equal(generate().disabled,false);
assert.deepEqual(JSON.parse(stored.get(STORAGE)).loras,[{id:'turbo',strength:0.8},{id:'age',strength:5}]);
await change('lora_scale-2','-2.3');assert.equal(slider.value,'-2.3');
assert.equal(h3LoraIssue([{id:'age',strength:-5}],components),'');
assert.equal(h3LoraIssue([{id:'age',strength:5}],components),'');
assert.match(h3LoraIssue([{id:'age',strength:5.1}],components),/Age must be/);
assert.match(h3LoraIssue([{id:'turbo',strength:5}],components),/between/);
''')


def test_age_signed_values_restore_in_draft_job_edit_and_bulk_editor():
    run_js(AGE_COMPONENT + r'''
const initial=[{id:'turbo',strength:1},{id:'age',strength:-5}];
stored.set(STORAGE,JSON.stringify({...defaults,loras:initial}));
showH3Video();await flush();
assert.equal(byId('lora_scale-2').value,'-5');assert.equal(byId('lora_age-2').value,'-5');
await byText('Refresh components').onclick();assert.equal(byId('lora_age-2').value,'-5');
const draft=editorOptions.getDraft();editor.active={id:'queued',revision:1};
editorOptions.applyEdit({config:{...defaults,loras:[{id:'age',strength:5}],prompt:'Saved job'},inputs:{}});
assert.equal(byId('lora_age').value,'5');assert.equal(byId('lora_age').parentElement.hidden,false);
editor.active=null;editorOptions.restoreDraft(draft);
assert.deepEqual(editorOptions.getDraft().config.loras,initial);
const definition=queueOptions.getFields().find(item=>item.key==='loras');
const bulk=definition.create(()=>{});document.body.appendChild(bulk.node);
bulk.setValue([{id:'age',strength:-5},{id:'turbo',strength:1}]);
assert.deepEqual(bulk.getValue(),[{id:'age',strength:-5},{id:'turbo',strength:1}]);
const slider=document.getElementById('h3-queue-lora_age');assert.equal(slider.value,'-5');
slider.value='5';await slider.fire('input');assert.equal(bulk.getValue()[0].strength,5);
bulk.setDisabled(true);assert.equal(slider.disabled,true);
bulk.setDisabled(false);assert.equal(slider.disabled,false);
''')


def test_portable_age_strengths_accept_endpoints_and_reject_generic_five():
    run_workflow_js(r'''
const ageName='MM-H3 - Ref2VA - Age Slider v2 - EXPERIMENTAL.safetensors';
const components=[{id:'age',role:'lora',name:ageName},{id:'turbo',role:'lora',name:'turbo.safetensors'}];
const doc={format:'odysseus-video-workflow',version:1,family:'h3',config:{mode:'ref2va',lora_strengths:[-5,1]},components:{lora_0:{name:ageName},lora_1:{name:'turbo.safetensors'}}};
let result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components}});
assert.deepEqual(result.config.loras,[{id:'age',strength:-5},{id:'turbo',strength:1}]);
doc.config.lora_strengths=[5,1];result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components}});
assert.equal(result.config.loras[0].strength,5);
result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:components.slice(1)}});
assert.equal(result.config.loras[0].strength,5);assert.match(result.config.loras[0].id,/unavailable/);
assert.throws(()=>resolveVideoWorkflow({...doc,config:{mode:'ref2va',lora_strengths:[5,5]}},{family:'h3',inventory:{components}}),/Invalid/);
const legacy={...doc,config:{mode:'ref2va',lora_scale:-5},components:{lora:{name:ageName}}};
assert.equal(resolveVideoWorkflow(legacy,{family:'h3',inventory:{components}}).config.loras[0].strength,-5);
''')


def test_profile_actions_are_explicit_and_custom_strengths_survive_stacks():
    run_js(r'''
inventory.lora_library=true;
const turbo=components.find(item=>item.id==='turbo');
turbo.display_name='coolthor Turbo';
turbo.strength_control={kind:'custom',label:'Effect',min:-10,max:10,step:0.2,default:0.4,labels:{min:'Less',max:'More'}};
turbo.lora_profile={display_name:'coolthor Turbo',trigger_words:'special_effect:',trigger_position:'prefix',notes:'Use a reference video.',recommended_settings:{steps:4,sampler:'res_multistep',shift_video:8},strength_control:turbo.strength_control};
components.find(item=>item.id==='ref').display_name='coolthor H3';
showH3Video();await flush();await change('prompt','Keep this scene.');await change('steps','23');await change('lora','turbo');
assert.equal(byId('prompt').value,'Keep this scene.');assert.equal(byId('steps').value,'23');
assert.equal(byId('lora_scale').value,'0.4');assert.equal(byId('lora_age').min,-10);
assert.equal(byId('lora').options.find(item=>item.value==='turbo').textContent,'coolthor Turbo');
assert.equal(byId('model').options.find(item=>item.value==='ref').textContent,'coolthor H3');
byText('Insert trigger').onclick();assert.equal(byId('prompt').value,'special_effect:\nKeep this scene.');
byText('Insert trigger').onclick();assert.equal(byId('prompt').value,'special_effect:\nKeep this scene.');
byText('Apply recommended settings').onclick();assert.equal(byId('steps').value,'4');assert.equal(byId('sampler').value,'res_multistep');assert.equal(byId('shift_video').value,'8');
assert.equal(byId('shift_audio').value,'3');assert.equal(byId('mode').value,'ref2va');
await change('lora_scale','-8');assert.equal(h3LoraIssue([{id:'turbo',strength:-8}],components),'');
const draft=editorOptions.getDraft();editorOptions.restoreDraft(draft);assert.equal(byId('lora_scale').value,'-8');
byText('Manage LoRAs').onclick();assert.ok(loraLibraryOptions);
assert.equal(byId('prompt').value,'special_effect:\nKeep this scene.');
''')


def test_manual_trigger_preserves_surrounding_text_and_suffix_only_dedupes_boundary():
    run_js(r'''
assert.equal(insertH3LoraTrigger('first last','trigger:','manual',6),'first trigger:last');
assert.equal(insertH3LoraTrigger('scene','tag','prefix'),'tag\nscene');
assert.equal(insertH3LoraTrigger('tag\nscene','tag','prefix'),'tag\nscene');
assert.equal(insertH3LoraTrigger('scene','tag','suffix'),'scene\ntag');
assert.equal(insertH3LoraTrigger('scene\ntag','tag','suffix'),'scene\ntag');
assert.equal(insertH3LoraTrigger('tag\nscene','tag','suffix'),'tag\nscene\ntag');
assert.equal(insertH3LoraTrigger('scene','','prefix'),'scene');
''')


def test_recommendations_do_not_partially_apply_when_a_control_is_unavailable():
    run_js(r'''
const vfx=components.find(item=>item.id==='vfx');
vfx.lora_profile={recommended_settings:{steps:4,frames:124}};
showH3Video();await flush();await change('lora','vfx');await change('steps','27');
assert.equal(byId('frames').disabled,true);
byText('Apply recommended settings').onclick();
assert.equal(byId('steps').value,'27');assert.ok(elements.some(item=>item.textContent.includes('controlled by the current workflow')));
''')


def test_saved_job_profile_overlays_only_its_editor_and_rerun_controls():
    run_js(r'''
const turbo=components.find(item=>item.id==='turbo');
turbo.strength_control={kind:'custom',label:'Current effect',min:-2,max:2,step:0.1,default:1};
turbo.lora_profile={trigger_words:'new_trigger',trigger_position:'prefix'};
const frozen=[{...turbo,display_name:'Saved effect',strength_control:{kind:'custom',label:'Saved effect',min:-12,max:12,step:0.1,default:0},lora_profile:{trigger_words:'old_trigger',trigger_position:'suffix'}}];
stored.set(STORAGE,JSON.stringify({...defaults,loras:[{id:'turbo',strength:1}]}));
showH3Video();await flush();const draft=editorOptions.getDraft();
const saved={id:'old',config:{...defaults,loras:[{id:'turbo',strength:-9}],prompt:'Job'},inputs:{},lora_components:frozen};
editor.active={id:'old'};editorOptions.applyEdit(saved);
assert.equal(byId('lora_scale').min,-12);assert.equal(byId('lora_scale').value,'-9');assert.equal(generate().disabled,false);
byText('Insert trigger').onclick();assert.equal(byId('prompt').value,'Job\nold_trigger');
assert.equal(turbo.strength_control.min,-2);assert.equal(turbo.lora_profile.trigger_words,'new_trigger');
editor.active=null;editorOptions.restoreDraft(draft);assert.equal(byId('lora_scale').min,-2);assert.equal(byId('lora_scale').value,'1');
const definition=queueOptions.getFields(saved.config,saved).find(item=>item.key==='loras'),rerun=definition.create(()=>{});
document.body.appendChild(rerun.node);rerun.setValue(saved.config.loras);
assert.equal(document.getElementById('h3-queue-lora_scale').min,-12);assert.equal(rerun.getValue()[0].strength,-9);
// A formerly generic adapter must not inherit a newly-added library profile.
const generic=h3LoraComponents(components,[{id:'turbo',role:'lora',name:'turbo.safetensors',variant:'ref2va'}]).find(item=>item.id==='turbo');
assert.equal(generic.strength_control,undefined);assert.equal(generic.lora_profile,undefined);
''')


def test_moved_components_restore_saved_and_edited_ids_through_inventory_aliases():
    run_js(r'''
for (const item of components) item.aliases = ['old-' + item.id];
stored.set(STORAGE, JSON.stringify({...defaults, model:'old-ref', encoder:'old-encoder',
 video_vae:'old-video_vae',audio_vae:'old-audio_vae',loras:[{id:'old-turbo',strength:0.65}]}));
showH3Video();await flush();
for (const key of ['model','encoder','video_vae','audio_vae']) assert.equal(byId(key).value, defaults[key]);
assert.equal(byId('lora').value,'turbo');assert.equal(byId('lora_scale').value,'0.65');
assert.deepEqual(editorOptions.getDraft().config.loras,[{id:'turbo',strength:0.65}]);
assert.match(h3LoraIssue([{id:'turbo',strength:1},{id:'old-turbo',strength:1}],components), /only once/);
editorOptions.restoreDraft({config:{...defaults, model:'old-ref', encoder:'old-encoder',loras:[{id:'old-vfx',strength:0.4}]},uploads:{}});
assert.equal(byId('model').value,'ref');assert.equal(byId('encoder').value,'encoder');
assert.deepEqual(editorOptions.getDraft().config.loras,[{id:'vfx',strength:0.4}]);
''')


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
