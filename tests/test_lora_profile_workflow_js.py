"""Portable LoRA profiles control strength validation without rewriting drafts."""
from test_video_workflow_js import run_js


def test_custom_profile_ranges_roundtrip_and_local_conflicts_are_visible():
    run_js(r'''
const profile={display_name:'My movement',trigger_words:'movement_style',trigger_position:'prefix',notes:'',variant:'ref2va',strength_control:{kind:'custom',label:'Movement',min:-8,max:8,step:.25,default:1},recommended_settings:{steps:4}};
const component={id:'installed',name:'movement.safetensors',role:'lora',variant:'ref2va',display_name:profile.display_name,lora_profile:profile,strength_control:profile.strength_control};
const doc={format:'odysseus-video-workflow',version:1,family:'h3',config:{mode:'ref2va',prompt:'Exactly my prompt',steps:20,lora_strengths:[-8]},components:{lora_0:{name:component.name,lora_profile:profile}}};
let result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:[component]}});
assert.deepEqual(result.config.loras,[{id:'installed',strength:-8}]);
assert.equal(result.config.prompt,'Exactly my prompt');assert.equal(result.config.steps,20);
result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:[]}});
assert.equal(result.config.loras[0].strength,-8);assert.match(result.config.loras[0].id,/unavailable/);
const local={...component,strength_control:{...profile.strength_control,min:-2,max:2}};
assert.throws(()=>resolveVideoWorkflow(doc,{family:'h3',inventory:{components:[local]}}),/local LoRA profile range/);
const invalid={...doc,config:{...doc.config,lora_strengths:[8.1]}};
assert.throws(()=>resolveVideoWorkflow(invalid,{family:'h3',inventory:{components:[component]}}),/Invalid/);
const legacy={...doc,config:{mode:'ref2va',lora_scale:8},components:{lora:doc.components.lora_0}};
assert.equal(resolveVideoWorkflow(legacy,{family:'h3',inventory:{components:[component]}}).config.loras[0].strength,8);
''')
