"""Bulk uploads keep ordinary multi-reference jobs and retry identities separate."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for batch queue checks')
    source = (ROOT / 'static/js/videoBatch.js').read_text()
    subprocess.run([node, '--input-type=module', '-e',
                    "import assert from 'node:assert/strict';\n" + source + '\n' + script],
                   check=True, capture_output=True, text=True)


def test_200_jobs_capture_one_snapshot_and_one_video_each_with_shared_images_audio():
    run_js(r'''
const frame=new File(['image'],'face.png'),audio=new File(['sound'],'voice.wav');
const ordinary=[new File(['ref1'],'ordinary1.mp4'),new File(['ref2'],'ordinary2.mp4')];
const draft={config:{mode:'ref2va',prompt:'Same prompt',seed:42,nested:{lora:'same'}},uploads:{reference_videos:ordinary,reference_images:[frame],reference_audio:[audio]},videoField:'reference_videos'};
let snapshots=0;const submitted=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>{snapshots++;return draft},submit:async(f,item)=>{
 const body=videoBatchFormData(item);submitted.push({id:item.id,config:JSON.parse(body.get('config')),videos:body.getAll('reference_videos'),images:body.getAll('reference_images'),audio:body.getAll('reference_audio')});
 draft.config.prompt='User editing later';draft.config.nested.lora='changed';draft.uploads.reference_images=[];
 return{id:item.id,status:'queued'};
}});
const videos=Array.from({length:200},(_,i)=>new File(['video'+i],`clip${200-i}.mp4`,{lastModified:123}));
assert.equal(q.add(videos).added,200);await q.run();
assert.equal(snapshots,1);assert.equal(submitted.length,200);assert.equal(new Set(submitted.map(x=>x.id)).size,200);
for(let i=0;i<200;i++){
 const row=submitted[i];assert.equal(row.config.prompt,'Same prompt');assert.equal(row.config.nested.lora,'same');assert.equal(row.config.seed,42);
 assert.equal(row.videos.length,1);assert.equal(row.videos[0].name,`clip${i+1}.mp4`);
 assert.equal(row.images[0].name,frame.name);assert.equal(await row.images[0].text(),'image');
 assert.equal(row.audio[0].name,audio.name);assert.equal(await row.audio[0].text(),'sound');
}
assert.equal(ordinary.length,2);assert.ok(q.items.every(x=>x.status==='queued'));
await q.run();assert.equal(submitted.length,200);
''')


def test_bfs_replaces_only_target_and_keeps_common_identity_mask_and_last_frame():
    run_js(r'''
const identityImage=new File(['face'],'identity.png'),mask=new File(['mask'],'mask.mp4'),last=new File(['last'],'last.png'),old=new File(['old'],'old.mp4');
const snapshot=snapshotVideoBatch({config:{workflow_id:'wan22_head_swap',prompt:'Swap'},uploads:{identity_image:[identityImage],source_video:[old],mask_video:[mask],last_frame:[last]},videoField:'source_video'},'bfs');
const file=new File(['new'],'target.mp4');const body=videoBatchFormData({file,snapshot});
assert.deepEqual(body.getAll('source_video'),[file]);assert.deepEqual(body.getAll('identity_image'),[identityImage]);
assert.deepEqual(body.getAll('mask_video'),[mask]);assert.deepEqual(body.getAll('last_frame'),[last]);
assert.throws(()=>snapshotVideoBatch({config:{},videoField:'reference_videos'},'bfs'),/Choose a video workflow/);
''')


def test_pause_finishes_current_upload_and_resume_keeps_original_snapshot():
    run_js(r'''
let release;const gate=new Promise(r=>release=r);let prompt='original';const sent=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{prompt},videoField:'reference_videos'}),submit:async(f,item)=>{sent.push(item);if(sent.length===1)await gate;return{id:item.id,status:'queued'}}});
q.add([1,2,3].map(i=>new File(['v'],`${i}.mp4`)));
const running=q.run();assert.equal(q.running,true);q.pause();prompt='changed';release();await running;
assert.equal(sent.length,1);assert.equal(q.items[0].status,'queued');assert.equal(q.items[1].status,'pending');
await q.run();assert.equal(sent.length,3);assert.ok(sent.every(x=>x.snapshot.config.prompt==='original'));
q.add([new File(['v'],'4.mp4')]);await q.run();assert.equal(sent[3].snapshot.config.prompt,'changed');
''')


def test_ambiguous_response_retry_reuses_id_snapshot_and_never_resends_confirmed_jobs():
    run_js(r'''
const receipts=new Map(),attempts=[];let loseResponse=true,prompt='original';
const q=new VideoBatchQueue({family:'bfs',getSnapshot:()=>({config:{prompt},videoField:'source_video'}),onJob:()=>{throw Error('Unrelated UI failure')},submit:async(f,item)=>{
 attempts.push(item.id);if(!receipts.has(item.id))receipts.set(item.id,{id:item.id,status:'queued'});
 if(loseResponse){loseResponse=false;throw Object.assign(Error('Response lost'),{status:0})}
 return receipts.get(item.id);
}});
q.add([new File(['a'],'a.mp4'),new File(['b'],'b.mp4')]);await q.run();
assert.equal(q.items[0].status,'error');assert.equal(q.items[1].status,'pending');assert.equal(receipts.size,1);
prompt='edited';await q.run({retry:true});assert.equal(attempts[0],attempts[1]);assert.equal(receipts.size,1);assert.equal(q.items[0].snapshot.config.prompt,'original');
await q.run();assert.equal(receipts.size,2);assert.ok(q.items.every(x=>x.status==='queued'));
await q.run({retry:true});assert.equal(attempts.length,3);
''')


def test_file_selection_deduplicates_exact_files_and_skips_empty_or_unsupported():
    run_js(r'''
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>{throw Error('Invalid draft')}});
const a=new File(['x'],'clip2.MKV',{lastModified:1}),b=new File(['x'],'clip2.MKV',{lastModified:2});
const result=q.add([a,a,b,new File(['bad'],'notes.txt'),new File([],'empty.mp4')]);
assert.equal(result.added,2);assert.equal(result.duplicates,1);assert.equal(result.rejected.length,2);
assert.equal(q.add([a]).duplicates,1);await assert.rejects(q.run(),/Invalid draft/);assert.equal(q.running,false);assert.ok(q.items.every(x=>!x.snapshot));
q.remove(q.items[0].id);assert.equal(q.items.length,1);q.clear();assert.equal(q.items.length,0);
''')


def test_xhr_transport_sends_stable_header_and_reports_upload_then_server_confirmation():
    run_js(r'''
let sent;const progress=[];
globalThis.XMLHttpRequest=class{constructor(){this.upload={};this.headers={}}open(method,url){assert.equal(method,'POST');assert.equal(url,'/api/video/h3/jobs')}setRequestHeader(k,v){this.headers[k]=v}send(body){sent={body,headers:this.headers};this.upload.onprogress({loaded:10,total:20,lengthComputable:true});this.upload.onload();this.status=201;this.responseText=JSON.stringify({id:'accepted',status:'queued'});this.onload()}};
const item={id:'stable-key',file:new File(['v'],'one.mp4'),snapshot:{config:{prompt:'Hi'},uploads:{},videoField:'reference_videos'}};
const job=await submitVideoBatchJob('h3',item,p=>progress.push(p));assert.equal(job.id,'accepted');assert.equal(sent.headers['X-Odysseus-Submission-Id'],'stable-key');assert.equal(sent.body.getAll('reference_videos').length,1);assert.equal(progress[0].loaded,10);assert.equal(progress.at(-1).waiting,true);
''')
