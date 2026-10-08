"""Bulk video controls preserve per-job media, explicit patches and retry identity."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for queue UI checks')
    source = (ROOT / 'static/js/videoQueue.js').read_text()
    result = subprocess.run([node, '--input-type=module', '-e',
                             "import assert from 'node:assert/strict';\n" + source + '\n' + script],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


DOM = r'''
const elements=[];
class Element {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attributes={};this.listeners={};this.classList={add(){},toggle(){}};this.hidden=false;this.disabled=false;this.checked=false;this.value='';this.type='';elements.push(this);}
 get options(){return this.children;}
 querySelector(){return null;}
 setAttribute(key,value){this.attributes[key]=value;}
 removeAttribute(key){delete this.attributes[key];}
 append(...items){this.children.push(...items);}
 appendChild(item){this.append(item);return item;}
 replaceChildren(...items){this.children=[...items];}
 add(option){this.append(option);if(this.children.length===1)this.value=option.value;}
 addEventListener(name,handler){this.listeners[name]=handler;}
 cloneNode(){const copy=new Element(this.tagName);copy.value=this.value;copy.type=this.type;copy.checked=this.checked;copy.children=[...this.children];return copy;}
 reportValidity(){return true;}
 focus(){}
 scrollIntoView(){}
}
globalThis.document={createElement:tag=>new Element(tag)};
globalThis.Option=function(label,value){return {label,value}};
const find=(name)=>elements.find(node=>node.dataset.queueAction===name);
const field=(key)=>elements.find(node=>node.dataset.queueField===key);
const value=(key)=>elements.find(node=>node.dataset.queueValue===key);
const approval=()=>elements.find(node=>node.className==='video-queue-approval').children[0];
const editor=()=>elements.find(node=>node.className==='video-queue-editor');
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
const steps=new Element('input');steps.type='number';steps.value='10';
const prompt=new Element('textarea');prompt.value='Current draft';
const vae=new Element('select');vae.add(new Option('VAE','vae-1'));
const queue={paused:false,queued:200,running:1,total_queued:210,total_running:1};
const calls=[];let reloads=0;
let handler=async(path,options)=>{
 if(path.startsWith('/queue/jobs'))return {jobs:[{id:'a',revision:3,status:'queued',source_name:'a.mp4'},{id:'b',revision:5,status:'queued',source_name:'b.mp4'}],queue};
 return {succeeded:2,queue};
};
const controls=createVideoQueueControls({container:new Element('div'),family:'h3',
 request:async(path,options)=>{calls.push({path,body:options?.body?JSON.parse(options.body):null});return handler(path,options)},
 getFields:()=>[videoQueueField('steps','Steps',steps,true),videoQueueField('prompt','Prompt',prompt),videoQueueField('components.vae','Video VAE',vae),videoQueueField('mode','Mode',prompt)],
 reload:async()=>{reloads++}});
controls.update(queue);
const submit=async()=>{editor().onsubmit({preventDefault(){}});await flush()};
'''


def test_sparse_patch_preserves_unchecked_and_structural_fields():
    run_js(r'''
const rows=[
 {key:'steps',numeric:true,check:{checked:true},control:{value:'25'}},
 {key:'prompt',check:{checked:false},control:{value:'Do not replace'}},
 {key:'components.vae',check:{checked:true},control:{value:'new-vae'}},
 {key:'lora',check:{checked:true},control:{value:''}},
 {key:'enabled',check:{checked:true},control:{type:'checkbox',checked:false}},
];
assert.deepEqual(videoQueuePatch(rows),{steps:25,components:{vae:'new-vae'},lora:'',enabled:false});
assert.throws(()=>videoQueuePatch([{key:'mode',check:{checked:true},control:{value:'ref2va'}}]),/cannot replace/);
assert.throws(()=>videoQueuePatch([{key:'steps',numeric:true,check:{checked:true},control:{value:''}}]),/Enter a number/);
''')


def test_edit_loads_full_selection_and_sends_only_checked_values_with_revisions():
    run_js(DOM + r'''
find('edit').onclick();await flush();
assert.equal(calls[0].path,'/queue/jobs?status=queued');assert.equal(field('mode'),undefined);
assert.equal(find('apply').disabled,true);assert.equal(value('steps').disabled,true);
field('steps').checked=true;field('steps').onchange();value('steps').value='25';value('steps').listeners.input();
approval().checked=true;approval().onchange();assert.equal(find('apply').disabled,false);
await submit();
assert.deepEqual(calls[1],{path:'/queue/edit',body:{patch:{steps:25},jobs:[{id:'a',revision:3},{id:'b',revision:5}]}});
assert.equal(steps.value,'10');assert.equal(prompt.value,'Current draft');assert.equal(editor().hidden,true);assert.equal(reloads,1);
''')


def test_rerun_defaults_finished_and_retries_partial_receipt_with_same_request_id():
    run_js(DOM + r'''
let posts=0;const originalHandler=handler;
handler=async(path,options)=>path==='/queue/rerun'?(++posts===1?{succeeded:1,failed:1,errors:[{id:'b',error:'Disk busy'}],queue}:{succeeded:2,failed:0,queue}):originalHandler(path,options);
find('rerun').onclick();await flush();assert.equal(calls[0].path,'/queue/jobs?status=finished');
field('steps').checked=true;field('steps').onchange();value('steps').value='4';value('steps').listeners.input();approval().checked=true;approval().onchange();
await submit();assert.equal(editor().hidden,false);assert.equal(value('steps').disabled,true);assert.match(find('apply').textContent,/Retry/);
await submit();const first=calls.find(item=>item.path==='/queue/rerun');const last=calls.at(-1);
assert.match(first.body.request_id,/^[0-9a-f-]{36}$/);assert.deepEqual(last.body,first.body);assert.equal(editor().hidden,true);
assert.equal(first.body.patch.steps,4);assert.equal(first.body.jobs.length,2);
''')


def test_network_retry_keeps_request_identity_and_conflict_keeps_edits_visible():
    run_js(DOM + r'''
let fail=true;const originalHandler=handler;
handler=async(path,options)=>{if(path==='/queue/rerun'&&fail){fail=false;throw Error('Network interrupted')}return originalHandler(path,options)};
find('rerun').onclick();await flush();approval().checked=true;approval().onchange();await submit();
assert.equal(editor().hidden,false);const first=calls.at(-1).body;await submit();assert.deepEqual(calls.at(-1).body,first);
handler=async(path,options)=>{if(path==='/queue/edit')throw Object.assign(Error('One job started'),{status:409});return originalHandler(path,options)};
find('edit').onclick();await flush();field('steps').checked=true;field('steps').onchange();
// New editor controls replace the old DOM; use the rows now present in the field grid.
const grid=elements.find(node=>node.className==='video-queue-fields');const row=grid.children[0];row.children[0].children[0].checked=true;row.children[0].children[0].onchange();row.children[1].value='19';
approval().checked=true;approval().onchange();await submit();assert.equal(editor().hidden,false);assert.equal(row.children[1].value,'19');
assert.ok(elements.some(node=>node.textContent==='One job started'));
''')


def test_pause_delete_scope_confirmation_and_partial_failure_reporting():
    run_js(DOM + r'''
handler=async(path,options)=>path==='/queue/pause'?{queue:{...queue,paused:true}}:{succeeded:199,failed:1,errors:[{id:'b',error:'Already running'}],queue:{...queue,queued:0}};
await find('pause').onclick();assert.deepEqual(calls[0],{path:'/queue/pause',body:{paused:true}});assert.equal(find('pause').textContent,'Resume queue');
const scope=elements.find(node=>node.attributes['aria-label']==='Queued job scope');scope.value='all';scope.onchange();
find('delete').onclick();assert.equal(calls.length,1);assert.match(find('confirm').textContent,/permanently/);
await find('confirm').onclick();assert.deepEqual(calls[1],{path:'/queue/delete',body:{scope:'all'}});
assert.ok(elements.some(node=>String(node.textContent).includes('199 jobs deleted')));assert.ok(elements.some(node=>String(node.textContent).includes('Already running')));
''')


def test_blocked_controls_do_not_start_simultaneous_mutations():
    run_js(DOM + r'''
controls.setBlocked(true);assert.equal(find('pause').disabled,true);await find('pause').onclick();assert.equal(calls.length,0);
controls.setBlocked(false);let release;handler=()=>new Promise(resolve=>{release=resolve});
const active=find('pause').onclick();assert.equal(controls.busy,true);await find('pause').onclick();assert.equal(calls.length,1);
release({queue:{...queue,paused:true}});await active;assert.equal(controls.busy,false);
''')


def test_single_job_parameter_editor_prefills_saved_values_and_sends_only_changed_fields():
    run_js(DOM + r'''
handler=async()=>({jobs:[{id:'original',revision:4,status:'completed',source_name:'source.mp4',config:{steps:25,prompt:'Saved job prompt',vae_gpu:'other'}}],queue});
let received=null;
await controls.openRerun('original',async(job,patch)=>{received={job,patch};return{result:{succeeded:1,failed:0},pending:false}});
assert.equal(controls.editing,true);assert.equal(value('steps').value,'25');assert.equal(value('prompt').value,'Saved job prompt');
assert.equal(value('steps').disabled,false);assert.equal(field('steps').hidden,true);assert.equal(value('components.vae'),undefined);
assert.equal(find('apply').disabled,true);assert.equal(find('apply').textContent,'Queue edited copy');
await submit();assert.equal(received,null); // Opening the editor cannot silently queue an unchanged copy.
value('steps').value='6';value('steps').listeners.input();value('prompt').value='Edited prompt';value('prompt').listeners.input();
await submit();assert.deepEqual(received.patch,{steps:6,prompt:'Edited prompt'});assert.equal(received.job.id,'original');assert.equal(received.job.revision,4);
assert.equal(steps.value,'10');assert.equal(prompt.value,'Current draft');assert.equal(controls.editing,false);
''')


def test_single_editor_freezes_on_uncertain_response_but_validation_errors_stay_editable():
    run_js(DOM + r'''
handler=async()=>({jobs:[{id:'original',revision:4,status:'failed',config:{steps:25,prompt:'Saved prompt'}}],queue});
let count=0;
await controls.openRerun('original',async()=>++count===1?{pending:false,error:'Invalid sampling settings'}:count===2?{pending:true,error:'Lost response'}:{result:{succeeded:1,failed:0}});
value('steps').value='600';value('steps').listeners.input();await submit();assert.equal(value('steps').disabled,false);
value('steps').value='6';value('steps').listeners.input();await submit();assert.equal(value('steps').disabled,true);assert.equal(find('apply').textContent,'Retry request');
await submit();assert.equal(controls.editing,false);
''')


def test_single_editor_reports_blocked_and_failed_open_without_starting_a_job():
    run_js(DOM + r'''
controls.setBlocked(true);assert.equal(controls.blocked,true);
const blocked=await controls.openRerun('original',async()=>{});assert.match(blocked.error,/Finish the current/);assert.equal(calls.length,0);
controls.setBlocked(false);assert.equal(controls.blocked,false);
handler=async()=>{throw Error('Saved parameters temporarily unavailable')};
const failed=await controls.openRerun('original',async()=>{});assert.equal(failed.error,'Saved parameters temporarily unavailable');
assert.equal(controls.editing,false);assert.equal(controls.busy,false);assert.equal(calls.length,1);assert.equal(calls[0].path,'/queue/jobs?status=finished');
handler=async()=>({jobs:[{id:'original',revision:4,status:'failed',config:{steps:25,prompt:'Saved'}}],queue});
assert.deepEqual(await controls.openRerun('original',async()=>{}),{opened:true});
assert.equal(find('apply').disabled,true);
''')
