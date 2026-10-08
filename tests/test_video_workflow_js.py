"""Portable recipes must remap local assets without silently selecting another model."""
from pathlib import Path
import base64
import io
import zipfile
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def save_fixture():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as bundle:
        bundle.writestr('workflow.json', '{"prompt":"Café 🌊"}')
    return r'''
globalThis.window={location:{origin:'https://odysseus.test'},isSecureContext:true,addEventListener(){},removeEventListener(){}};
const bytes=new Uint8Array(Buffer.from(''' + repr(base64.b64encode(archive.getvalue()).decode()) + r''','base64'));
const calls=[], events=[], written=[];
let committed=false,rolledBack=false;
const handle={name:'chosen.zip',async createWritable(){return new WritableStream({write(chunk){written.push(...chunk)},close(){committed=true},abort(){rolledBack=true}})}};
window.showSaveFilePicker=options=>{events.push('picker');assert.equal(options.suggestedName,'h3.odysseus-workflow.zip');return Promise.resolve(handle)};
const response=()=>new Response(new ReadableStream({start(c){for(let i=0;i<bytes.length;i+=7)c.enqueue(bytes.slice(i,i+7));c.close()}}),{headers:{'Content-Type':'application/zip','Content-Length':String(bytes.length)}});
globalThis.fetch=async(url,options)=>{calls.push({url,options});events.push(options?.method==='POST'?'prepare':'download');return options?.method==='POST'?new Response(JSON.stringify({download_url:'/api/video/h3/workflow/exports/ticket',filename:'server.zip',total_bytes:bytes.length-20})):response()};
'''


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for workflow transport checks')
    source = (ROOT / 'static/js/videoWorkflow.js').read_text()
    subprocess.run([node, '--input-type=module', '-e',
                    "import assert from 'node:assert/strict';\n" + source + '\n' + script],
                   check=True, capture_output=True, text=True)


def test_portable_component_resolution_preserves_exact_variant_and_rejects_ambiguous_matches():
    run_js(r'''
const component=(id,name,repo,role='model',variant='fl2va')=>({id,name,role,variant,path:`/new/cache/models--${repo.replace('/','--')}/snapshots/rev/${name}`});
const model=component('new-model','MiniMax_H3_FL2VA_nvfp4.safetensors','author/H3');
const wrong=component('wrong','MiniMax_H3_FL2VA_nvfp4.safetensors','other/H3');
const encoder=component('new-encoder','qwen_h3.safetensors','author/Qwen','encoder','shared');
const doc={format:'odysseus-video-workflow',version:1,family:'h3',config:{mode:'t2va',prompt:'Café 🌊',steps:10,gpu:'old-GPU',vae_gpu:'other-old-GPU',worker_path:'/tmp/execute'},components:{model:{name:model.name,repository:'author/H3'},encoder:{name:encoder.name}}};
let result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:[wrong,model,encoder]}});
assert.equal(result.config.model,'new-model');assert.equal(result.config.encoder,'new-encoder');
assert.equal(result.config.prompt,'Café 🌊');assert.equal(result.config.gpu,undefined);assert.equal(result.config.vae_gpu,undefined);assert.equal(result.config.worker_path,undefined);
assert.deepEqual(result.warnings,[]);
result=resolveVideoWorkflow(doc,{family:'h3',inventory:{components:[wrong,encoder]}});
assert.equal(result.config.model,'');assert.match(result.warnings.join(' '),/not installed/);
result=resolveVideoWorkflow({...doc,components:{model:{name:model.name}}},{family:'h3',inventory:{components:[wrong,model]}});
assert.equal(result.config.model,'');assert.match(result.warnings.join(' '),/more than one/);
result=resolveVideoWorkflow({...doc,components:{model:{name:model.name}}},{family:'h3',inventory:{components:[wrong,model]},resolvedComponents:{model:model.id}});
assert.equal(result.config.model,model.id);
assert.throws(()=>resolveVideoWorkflow({...doc,version:99},{family:'h3',inventory:{}}),/supported/);
assert.throws(()=>resolveVideoWorkflow({...doc,components:{model:{name:'../../secret'}}},{family:'h3',inventory:{}}),/Invalid/);
assert.throws(()=>resolveVideoWorkflow({...doc,config:{mode:'bad'}},{family:'h3',inventory:{}}),/Unknown/);
''')


def test_bfs_recipe_restores_only_available_matching_slots():
    run_js(r'''
const inventory={components:[{id:'a',name:'face.safetensors'},{id:'bad',name:'wrong.safetensors'}],workflows:[{id:'ltx25_v1',controls:[{key:'steps'}],slots:[{key:'lora',component_ids:['a']}]}]};
const doc={format:'odysseus-video-workflow',version:1,family:'bfs',config:{workflow_id:'ltx25_v1',prompt:'Head swap',steps:8,gpu:'remote',arbitrary:true},components:{lora:{name:'face.safetensors'}}};
let result=resolveVideoWorkflow(doc,{family:'bfs',inventory});
assert.deepEqual(result.config,{workflow_id:'ltx25_v1',prompt:'Head swap',steps:8,components:{lora:'a'}});
result=resolveVideoWorkflow({...doc,components:{lora:{name:'wrong.safetensors'}}},{family:'bfs',inventory,resolvedComponents:{lora:'bad'}});
assert.equal(result.config.components.lora,'');assert.equal(result.warnings.length,1);
assert.throws(()=>resolveVideoWorkflow(doc,{family:'h3',inventory}),/BFS/);
assert.throws(()=>resolveVideoWorkflow({...doc,config:{workflow_id:'unknown'}},{family:'bfs',inventory}),/unavailable/);
''')


def test_export_download_avoids_weight_response_blob_and_import_streams_file():
    run_js(r'''
const links=[],calls=[];
globalThis.window={location:{origin:'https://odysseus.test'}};
globalThis.document={body:{appendChild(){}},createElement(){const a={click(){links.push({href:a.href,download:a.download})},remove(){}};return a;}};
globalThis.fetch=async(url,options)=>{calls.push({url,options});return{ok:true,json:async()=>({download_url:'/api/video/h3/workflow/exports/ticket',filename:'recipe.zip'}),blob(){throw Error('must not buffer weights')}}};
const input=new File(['pixels'],'frame.png',{type:'image/png'});
await exportVideoWorkflow({family:'h3',config:{prompt:'one'},uploads:{first_frame:[input]},includeWeights:true,includeAttachments:false});
assert.equal(calls.length,1);assert.equal(calls[0].options.body.get('first_frame'),null);
assert.deepEqual(JSON.parse(calls[0].options.body.get('options')),{include_weights:true,include_attachments:false});
assert.equal(links[0].href,'https://odysseus.test/api/video/h3/workflow/exports/ticket');
await exportVideoWorkflow({family:'h3',config:{},uploads:{first_frame:[input]},includeWeights:false,includeAttachments:true});
assert.equal(calls[1].options.body.get('first_frame').name,'frame.png');
await downloadJobWorkflow('bfs','a'.repeat(32),{includeWeights:true,includeAttachments:true});
assert.match(links[2].href,/include_weights=true&include_attachments=true/);
const archive=new File(['zipdata'],'recipe.zip');
globalThis.fetch=async(url,options)=>{assert.equal(options.body,archive);assert.equal(options.headers['Content-Type'],'application/octet-stream');return{ok:true,json:async()=>({workflow:{format:'odysseus-video-workflow',version:1,family:'h3',config:{mode:'t2va',prompt:'hello'},components:{}},inventory:{components:[]},attachments:[]})}};
const result=await importVideoWorkflow(archive,{family:'h3',inventory:{}});assert.equal(result.config.prompt,'hello');
globalThis.fetch=async()=>({ok:true,json:async()=>({download_url:'https://attacker.test/weights.zip'})});
await assert.rejects(exportVideoWorkflow({family:'h3',config:{}}),/invalid workflow download address/);
''')


def test_save_picker_precedes_network_and_streams_selected_file_until_committed():
    run_js(save_fixture() + r'''
const progress=[];
const pending=exportVideoWorkflow({family:'h3',config:{},includeWeights:true,onProgress:p=>{progress.push(p);if(p.phase==='saved')assert.equal(committed,true)}});
assert.deepEqual(events,['picker']);assert.equal(calls.length,0);
const result=await pending;
assert.deepEqual(events,['picker','prepare','download']);
assert.deepEqual(new Uint8Array(written),bytes);assert.equal(rolledBack,false);
assert.deepEqual(result.status,'saved');assert.equal(result.filename,'chosen.zip');assert.equal(result.bytes,bytes.length);
assert.ok(progress.some(p=>p.phase==='saving'&&p.loaded>0));assert.equal(progress.at(-1).phase,'saved');
window.showSaveFilePicker=()=>Promise.resolve(handle);
await downloadJobWorkflow('bfs','a'.repeat(32),{includeWeights:true});
assert.match(calls.at(-1).url,/bfs\/jobs\/a+\/workflow\?include_weights=true/);
''')


def test_cancelled_picker_or_failed_preparation_does_not_start_download():
    run_js(save_fixture() + r'''
window.showSaveFilePicker=()=>Promise.reject(new DOMException('Cancelled','AbortError'));
await assert.rejects(exportVideoWorkflow({family:'h3',config:{}}),{name:'AbortError'});
assert.equal(calls.length,0);assert.equal(committed,false);
window.showSaveFilePicker=()=>Promise.resolve(handle);
globalThis.fetch=async()=>{calls.push('prepare');return new Response(JSON.stringify({detail:'Model missing'}),{status:400})};
await assert.rejects(exportVideoWorkflow({family:'h3',config:{}}),/Model missing/);
assert.equal(calls.length,1);assert.equal(written.length,0);
window.isSecureContext=false;
await assert.rejects(exportVideoWorkflow({family:'h3',config:{},destination:'file'}),/cannot choose/);
assert.equal(calls.length,1);
''')


def test_failed_or_truncated_stream_aborts_destination_without_reporting_saved():
    run_js(save_fixture() + r'''
const originalFetch=fetch;
for(const failure of ['truncated','network','length','disk']) {
 committed=false;rolledBack=false;
 globalThis.fetch=async(url,options)=>{
  if(options?.method==='POST')return originalFetch(url,options);
  if(failure==='network')return new Response(new ReadableStream({start(c){c.enqueue(bytes.slice(0,20))},pull(c){c.error(new Error('Network lost'))}}));
  if(failure==='truncated')return new Response(bytes.slice(0,-1));
  if(failure==='length')return new Response(bytes,{headers:{'Content-Length':String(bytes.length+1)}});
  return response();
 };
 if(failure==='disk')handle.createWritable=async()=>new WritableStream({write(){throw new Error('Disk full')}});
 const progress=[];
 await assert.rejects(exportVideoWorkflow({family:'h3',config:{},onProgress:p=>progress.push(p)}),/incomplete|Network lost|Disk full/);
 assert.equal(committed,false);assert.ok(!progress.some(p=>p.phase==='saved'));
 if(failure!=='disk')assert.equal(rolledBack,true);
}
''')


def test_abort_mid_download_cancels_server_stream_and_destination():
    run_js(save_fixture() + r'''
const controller=new AbortController(),originalFetch=fetch;let cancelled=false;
globalThis.fetch=async(url,options)=>options?.method==='POST'?originalFetch(url,options):new Response(new ReadableStream({start(c){c.enqueue(bytes.slice(0,50))},cancel(){cancelled=true}}));
await assert.rejects(exportVideoWorkflow({family:'h3',config:{},signal:controller.signal,onProgress:p=>{if(p.phase==='saving'&&p.loaded>0)controller.abort()}}),{name:'AbortError'});
await new Promise(r=>setTimeout(r,0));
assert.equal(committed,false);assert.equal(rolledBack,true);assert.equal(cancelled,true);
''')
