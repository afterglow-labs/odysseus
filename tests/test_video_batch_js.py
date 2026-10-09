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


def test_vfx_200_sources_keep_shared_reference_videos_images_and_audio_in_every_job():
    run_js(r'''
const references=[new File(['reference1'],'reference1.mp4'),new File(['reference2'],'reference2.mp4')];
const image=new File(['image'],'clothing.png'),audio=new File(['sound'],'tone.wav');
const sent=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',prompt:'Use <Picture 1> only for the jacket',loras:[{id:'vfx',strength:1}]},videoField:'source_video',uploads:{source_video:[new File(['old'],'old.mp4')],reference_videos:references,reference_images:[image],reference_audio:[audio]}}),submit:async(family,item)=>{sent.push(videoBatchFormData(item));return {id:item.id,status:'queued'}}});
q.add(Array.from({length:200},(_,index)=>new File(['source'],`source${index+1}.mp4`)));await q.run();
assert.equal(sent.length,200);
for(let index=0;index<sent.length;index++){
 assert.equal(sent[index].getAll('source_video').length,1);assert.equal(sent[index].get('source_video').name,`source${index+1}.mp4`);
 assert.deepEqual(sent[index].getAll('reference_videos'),references);assert.deepEqual(sent[index].getAll('reference_images'),[image]);assert.deepEqual(sent[index].getAll('reference_audio'),[audio]);
 assert.deepEqual(JSON.parse(sent[index].get('config')).loras,[{id:'vfx',strength:1}]);
}
assert.equal(references.length,2);
''')


def test_automatic_length_is_submission_metadata_and_survives_retry_snapshot():
    run_js(r'''
let automatic=true,attempts=0;
const bodies=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',frames:124,prompt:'Keep the scene'},videoField:'reference_videos',autoVideoLength:automatic}),submit:async(f,item)=>{
 bodies.push(videoBatchFormData(item));
 if(attempts++===0)throw Object.assign(Error('Response lost'),{status:0});
 return{id:item.id,status:'queued',batch_video_length:{duration_seconds:8,frames:192,selected_seconds:8}};
}});
q.add([new File(['video'],'one.mp4')]);await q.run();automatic=false;
await q.run({retry:true});
assert.equal(bodies.length,2);
for(const body of bodies){
 assert.equal(body.get('auto_video_length'),'true');
 assert.deepEqual(JSON.parse(body.get('config')),{mode:'ref2va',frames:124,prompt:'Keep the scene'});
}
q.add([new File(['other'],'two.mp4')]);await q.run();
assert.equal(bodies.at(-1).has('auto_video_length'),false);
assert.equal(q.items[0].snapshot.config.frames,124);
''')


def test_auto_length_does_not_leak_to_bfs_or_change_reference_attachments():
    run_js(r'''
const video=new File(['video'],'one.mp4'),shared=new File(['ref'],'reference.mp4');
const value={config:{frames:124},autoVideoLength:true,videoField:'source_video',uploads:{reference_videos:[shared]}};
const h3=videoBatchFormData({file:video,snapshot:snapshotVideoBatch(value,'h3')});
assert.equal(h3.get('auto_video_length'),'true');assert.deepEqual(h3.getAll('reference_videos'),[shared]);
const bfs=videoBatchFormData({file:video,snapshot:snapshotVideoBatch(value,'bfs')});
assert.equal(bfs.has('auto_video_length'),false);
assert.equal(value.config.frames,124);assert.equal(value.uploads.reference_videos.length,1);
assert.equal(videoBatchLengthLabel({batch_video_length:{duration_seconds:8,selected_seconds:8.708333,frames:209}}),'8.00s source → 8.71s · 209 frames');
assert.match(videoBatchLengthLabel({batch_video_length:{duration_seconds:8,selected_seconds:8.708333,frames:209,preserve_source_duration:true}}),/preserves source length/);
assert.equal(videoBatchLengthLabel({}), '');
''')
