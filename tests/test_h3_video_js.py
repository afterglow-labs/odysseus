"""The H3 editor selects VFX behavior without losing draft or queued media."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for H3 UI checks")
    source = (ROOT / "static/js/h3Loras.js").read_text() + '\n' + (ROOT / "static/js/h3Video.js").read_text()
    source = re.sub(r"^import .*;\n", "", source, flags=re.MULTILINE)
    result = subprocess.run(
        [node, "--input-type=module", "-e", "import assert from 'node:assert/strict';\n" + source + DOM + script],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


DOM = r"""
const elements=[];
class Element {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.style={};this.dataset={};this.attributes={};this.listeners={};this.classList={add(){},remove(){},toggle(){}};this.hidden=false;this.disabled=false;this.checked=false;this._value='';this.type='';this.textContent='';elements.push(this);}
 set value(value){this._value=String(value);}
 get value(){return this._value;}
 get options(){return this.children;}
 setAttribute(key,value){this.attributes[key]=value;}
 removeAttribute(key){delete this.attributes[key];}
 append(...items){for(const item of items){item.parentElement=this;this.children.push(item);}}
 appendChild(item){this.append(item);return item;}
 replaceChildren(...items){this.children=[];this.append(...items);this._value='';}
 add(option){this.append(option);if(this.children.length===1)this.value=option.value;}
 addEventListener(name,handler){(this.listeners[name]??=[]).push(handler);}
 async fire(name,bubble=true){const event={preventDefault(){}};for(const fn of this.listeners[name]||[])await fn(event);if(bubble&&this.parentElement)await this.parentElement.fire(name);}
 reportValidity(){return true;}
 checkValidity(){return true;}
 focus(){}
 scrollIntoView(){}
 remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(item=>item!==this);}
}
const connected=node=>node===document.body||node===document.head||!!(node.parentElement?.children.includes(node)&&connected(node.parentElement));
globalThis.document={createElement:tag=>new Element(tag),head:new Element('head'),body:new Element('body'),querySelectorAll:()=>[],getElementById:id=>elements.find(node=>node.id===id&&connected(node))};
globalThis.window={location:{host:'localhost'},addEventListener(){},removeEventListener(){}};
globalThis.Option=function(label,value){return {textContent:label,label,value,disabled:false,hidden:false}};
globalThis.setInterval=()=>1;globalThis.clearInterval=()=>{};
const stored=new Map();globalThis.localStorage={getItem:key=>stored.get(key)||null,setItem:(key,value)=>stored.set(key,value),removeItem:key=>stored.delete(key)};
const topPortalZ=()=>1,bindMenuDismiss=(node,close)=>close,dismissOrRemove=()=>{},dismissTopMenu=()=>{};
const videoWorkflow={};
let batch,editor,editorOptions;
const createVideoBatchQueue=options=>(batch={...options,active:false,busy:false,setAvailable(){},setEnabled(enabled,issue){this.enabled=enabled;this.issue=issue;},destroy(){}});
const createVideoJobEditor=options=>{editorOptions=options;return editor={active:null,loading:false,observe(){},finish(){}};};
const createVideoQueueControls=()=>({busy:false,editing:false,setBlocked(){},update(){},destroy(){}});
const videoQueueField=()=>{};
const videoJobEditFormData=()=>new FormData();
const components=[
 {id:'ref',role:'model',variant:'ref2va',name:'Ref2VA'},
 {id:'fl',role:'model',variant:'fl2va',name:'FL2VA'},
 ...['encoder','video_vae','audio_vae'].map(role=>({id:role,role,name:role})),
 {id:'vfx',role:'lora',variant:'ref2va',recipe:'vfx_edit',name:'VFX Edit'},
 {id:'turbo',role:'lora',variant:'ref2va',name:'Turbo'},
];
const defaults={mode:'ref2va',model:'ref',encoder:'encoder',video_vae:'video_vae',audio_vae:'audio_vae',lora:'',gpu:'gpu',vae_gpu:'',width:960,height:544,frames:124,steps:20,seed:42,lora_scale:1,sampler:'euler',scheduler:'simple',shift_video:12,shift_audio:3,reference_size:'match'};
const inventory={components,gpus:[{id:'gpu',name:'GPU',nvfp4:true}],runtime_ready:true,batch_jobs:true,lora_stack:true,max_loras:8,defaults};
const calls=[];
globalThis.fetch=async(url,options={})=>{calls.push({url,...options});let data={};
 if(url.endsWith('/inventory'))data=inventory;
 else if(url.endsWith('/prompt-enhancer'))data={models:[{endpoint_id:'local',model:'Qwen'}],default:{endpoint_id:'local',model:'Qwen'}};
 else if(url.endsWith('/enhance-prompt'))data={prompt:'Add a blue glow around the hands.'};
 else if(url.endsWith('/jobs'))data={jobs:[]};
 return{ok:true,json:async()=>data};
};
const byId=id=>document.getElementById('h3-'+id);
const byText=text=>elements.find(node=>node.tagName==='BUTTON'&&node.textContent===text);
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
const change=async(key,value)=>{if(key==='lora'&&!byId(key))byText('Add LoRA').onclick();byId(key).value=value;await byId(key).fire('change');};
const attach=async(key,...files)=>{byId(key).files=files;await byId(key).fire('change');};
const source=()=>new File(['video'],'source.mp4');
const generate=()=>byText(editor.active?'Save changes':'Generate video');
const form=()=>elements.find(node=>node.tagName==='FORM');
"""


def test_vfx_controls_preserve_prompt_and_show_source_timing_without_changing_sampling():
    run_js(r"""
showH3Video();await flush();
await change('prompt','Keep this exact prompt');await change('steps','27');
await change('lora','vfx');
assert.equal(byId('prompt').value,'Keep this exact prompt');assert.equal(byId('steps').value,'27');
assert.equal(byId('reference_videos').parentElement.children[0].textContent,'Source video');
assert.equal(byId('reference_videos').multiple,false);
assert.equal(byId('reference_images').parentElement.hidden,true);assert.equal(byId('reference_audio').disabled,true);
assert.equal(byId('frames').disabled,true);assert.equal(byId('reference_size').disabled,true);
assert.equal(byId('frames').options.find(option=>option.value==='73').hidden,false);
assert.ok(elements.some(node=>!node.hidden&&node.textContent.startsWith('Matches source video.')));
await change('lora','turbo');
assert.equal(byId('reference_videos').multiple,true);assert.equal(byId('reference_images').disabled,false);
assert.equal(byId('frames').disabled,false);assert.equal(byId('frames').options.find(option=>option.value==='73').hidden,true);
assert.equal(byId('frames').value,'124');assert.equal(byId('prompt').value,'Keep this exact prompt');
""")


def test_stale_images_and_audio_remain_removable_and_block_all_vfx_submission_paths():
    run_js(r"""
showH3Video();await flush();await change('prompt','Glow');
const image=new File(['image'],'face.png'),audio=new File(['audio'],'voice.wav');
await attach('reference_images',image);await attach('reference_audio',audio);await attach('reference_videos',source());
await change('lora','vfx');
for(const key of ['reference_images','reference_audio']){assert.equal(byId(key).parentElement.hidden,false);assert.equal(byId(key).disabled,true);}
assert.equal(generate().disabled,true);assert.match(batch.issue,/Remove the preserved/);
assert.equal(byText('Enhance prompt').disabled,true);await form().fire('submit');
assert.equal(calls.filter(call=>call.method==='POST').length,0);
const imageRemove=byId('reference_images').parentElement.children[2].children[0].children[1];imageRemove.onclick();
assert.equal(byId('reference_images').parentElement.hidden,true);assert.equal(generate().disabled,true);
byId('reference_audio').parentElement.children[2].children[0].children[1].onclick();
assert.equal(generate().disabled,false);assert.equal(batch.enabled,true);
""")


def test_multiple_videos_are_preserved_and_batch_snapshot_has_one_video_per_job():
    run_js(r"""
showH3Video();await flush();await change('prompt','Glow');
await attach('reference_videos',source(),new File(['two'],'second.mp4'));await change('lora','vfx');
assert.equal(generate().disabled,true);assert.equal(byId('reference_videos').parentElement.children[2].children.length,2);
assert.ok(elements.some(node=>node.textContent.startsWith('VFX Edit accepts one source video')));
batch.active=true;batch.onChange();
assert.equal(batch.enabled,true);const snapshot=batch.getSnapshot();
assert.equal(snapshot.videoField,'reference_videos');assert.equal(snapshot.config.lora,'vfx');
assert.equal(snapshot.uploads.reference_videos,undefined);
assert.deepEqual(snapshot.uploads.reference_images,[]);assert.deepEqual(snapshot.uploads.reference_audio,[]);
assert.equal(byId('reference_videos').parentElement.children[2].children.length,2);
batch.active=false;batch.onChange();assert.equal(generate().disabled,true);
""")


def test_vfx_rejects_missing_source_and_enhances_using_recipe_and_retained_video_count():
    run_js(r"""
showH3Video();await flush();await change('prompt','Glow');await change('lora','vfx');
await form().fire('submit');assert.ok(elements.some(node=>node.textContent==='Choose a source video for VFX Edit.'));
assert.equal(calls.filter(call=>call.method==='POST').length,0);
editor.active={id:'queued',revision:2};
editorOptions.applyEdit({config:{...defaults,lora:'vfx',prompt:'Glow'},inputs:{reference_videos:[{index:0,name:'saved.mp4',size:7}]}});
assert.equal(byText('Enhance prompt').disabled,false);await byText('Enhance prompt').fire('click');
const payload=JSON.parse(calls.find(call=>call.url.endsWith('/enhance-prompt')).body);
assert.equal(payload.recipe,'vfx_edit');assert.equal(payload.reference_counts.reference_videos,1);
assert.equal(byId('prompt').value,'Add a blue glow around the hands.');
""")


def test_generic_installed_preset_applies_once_preserves_overrides_and_blocks_queued_edit():
    run_js(r"""
inventory.installed_preset={revision:'vfx-one',name:'VFX Edit',config:{...defaults,lora:'vfx'}};
stored.set(STORAGE,JSON.stringify({...defaults,steps:33}));
showH3Video();await flush();assert.equal(byId('lora').value,'vfx');assert.equal(byId('steps').value,'20');
assert.ok(byText('Use installed preset'));await change('steps','31');await change('lora','turbo');
await change('mode','fl2va');await change('mode','ref2va');
assert.equal(byId('steps').value,'31');assert.equal(byId('lora').value,'turbo');
await change('prompt','Keep me');editor.active={id:'queued',revision:1};byText('Use installed preset').onclick();
assert.equal(byId('steps').value,'31');assert.equal(byId('lora').value,'turbo');
editor.active=null;byText('Use installed preset').onclick();assert.equal(byId('lora').value,'vfx');
assert.equal(byId('prompt').value,'Keep me');assert.equal(byId('width').value,'960');
""")


def test_restored_short_vfx_job_preserves_frames_and_hidden_keyframes_until_removed():
    run_js(r"""
showH3Video();await flush();editor.active={id:'queued',revision:1};
editorOptions.applyEdit({config:{...defaults,lora:'vfx',prompt:'Glow',frames:73},inputs:{first_frame:[{index:0,name:'first.png',size:2}],reference_videos:[{index:0,name:'source.mp4',size:7}]}});
assert.equal(byId('frames').value,'73');assert.equal(byId('frames').disabled,true);
assert.equal(byId('first_frame').parentElement.hidden,false);assert.equal(byId('first_frame').disabled,true);
assert.equal(generate().disabled,true);
byId('first_frame').parentElement.children[2].children[0].children[1].onclick();assert.equal(generate().disabled,false);
await change('lora','turbo');assert.equal(byId('frames').value,'73');assert.equal(byId('frames').disabled,false);
assert.equal(generate().disabled,true);assert.match(batch.issue,/editing the queued job/);
assert.ok(elements.some(node=>node.textContent.includes('Choose a length of at least 124 frames')));
await change('frames','124');assert.equal(generate().disabled,false);
""")
