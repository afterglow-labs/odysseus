"""Bulk uploads keep ordinary multi-reference jobs and retry identities separate."""
from pathlib import Path
import shutil
import re
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for batch queue checks')
    source = (ROOT / 'static/js/videoBatch.js').read_text()
    source = (ROOT / 'static/js/videoInputs.js').read_text() + '\n' + source
    source = (ROOT / 'static/js/referenceVideoRange.js').read_text() + '\n' + source
    source = re.sub(r'^import .*;\n', '', source, flags=re.MULTILINE)
    subprocess.run([node, '--input-type=module', '-e',
                    "import assert from 'node:assert/strict';\n" + source + '\n' + script],
                   check=True, capture_output=True, text=True)


def test_server_batch_keeps_drive_identity_and_never_uploads_server_video_bytes():
    run_js(r'''
const remote=(root,path)=>serverVideoInput({root,path,size:123456,mtime_ns:'1780000000000000001'});
const e=remote('e','folder/clip.mp4'),f=remote('f','folder/clip.mp4');
const phone=new File(['phone-video'],'local.mp4'),reference=remote('f','refs/reference.mp4');
const bodies=[];
const queue=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{prompt:'same'},videoField:'source_video',autoVideoLength:true,uploads:{reference_videos:[phone,reference]}}),submit:async(family,item)=>{bodies.push(videoBatchFormData(item));return{id:item.id}}});
assert.equal(queue.add([e,f,e]).added,2);await queue.run();
assert.equal(bodies.length,2);
for(const body of bodies){
 assert.equal(body.getAll('source_video').length,0);
 assert.equal(body.getAll('reference_videos').length,1);
 assert.equal(await body.get('reference_videos').text(),'phone-video');
 assert.equal(body.get('auto_video_length'),'true');
 const map=JSON.parse(body.get('server_inputs'));
 assert.equal(map.source_video[0].index,0);
 assert.equal(map.reference_videos[0].index,1);
 assert.equal(map.reference_videos[0].root,'f');
 assert.equal(map.reference_videos[0].mtime_ns,'1780000000000000001');
 assert.equal(body.getAll('server_inputs').length,1);
}
assert.deepEqual(bodies.map(body=>JSON.parse(body.get('server_inputs')).source_video[0].root),['e','f']);
assert.ok(Object.isFrozen(e.serverFile));
''')


def test_each_video_has_its_own_audio_and_images_without_creating_extra_jobs():
    run_js(r'''
const remote=(root,path)=>serverVideoInput({root,path,size:1234,mtime_ns:'1780000000000000001'});
const videos=[remote('e','clips/one.mp4'),remote('f','clips/two.mp4')];
const shared=new File(['shared'],'shared.wav'),audio1=new File(['one'],'one.wav'),audio2=remote('f','audio/two.flac');
const image1=remote('e','images/one.png'),image2=new File(['two-image'],'two.png');
const bodies=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',prompt:'same',steps:20},uploads:{reference_audio:[shared]},videoField:'reference_videos',autoVideoLength:true}),submit:async(f,item)=>{bodies.push(videoBatchFormData(item));return{id:item.id}}});
q.add(videos);q.addInputs(q.items[0].id,'reference_audio',[audio1]);q.addInputs(q.items[1].id,'reference_audio',[audio2]);
q.addInputs(q.items[0].id,'reference_images',[image1]);q.addInputs(q.items[1].id,'reference_images',[image2]);
await q.run();assert.equal(bodies.length,2);assert.equal(q.items.length,2);
assert.deepEqual(bodies[0].getAll('reference_audio'),[audio1]);assert.deepEqual(bodies[1].getAll('reference_audio'),[]);
assert.equal(JSON.parse(bodies[0].get('server_inputs')).reference_images[0].path,'images/one.png');
assert.equal(JSON.parse(bodies[1].get('server_inputs')).reference_audio[0].path,'audio/two.flac');
assert.deepEqual(bodies[1].getAll('reference_images'),[image2]);
assert.deepEqual(bodies.map(body=>JSON.parse(body.get('server_inputs')).reference_videos[0].path),['clips/one.mp4','clips/two.mp4']);
assert.ok(bodies.every(body=>body.get('auto_video_length')==='true'));
assert.equal(q.items[0].snapshot.config.prompt,'same');
assert.throws(()=>q.addInputs(q.items[0].id,'reference_audio',[shared]),/already saved/);
''')


def test_per_job_inputs_and_ranges_are_frozen_across_pause_and_ambiguous_retry():
    run_js(r'''
const primary=new File(['primary'],'one.mp4'),extra=new File(['extra'],'extra.mp4');
const audio=new File(['original'],'original.wav');setReferenceVideoRange(extra,{start_seconds:20,duration_seconds:3});
let attempt=0;const bodies=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',prompt:'original'},videoField:'reference_videos',autoVideoLength:true}),submit:async(f,item)=>{bodies.push(videoBatchFormData(item));if(attempt++===0)throw Error('Lost response');return{id:item.id}}});
q.add([primary]);const item=q.items[0];q.addInputs(item.id,'reference_videos',[extra]);q.addInputs(item.id,'reference_audio',[audio]);
await q.run();assert.equal(item.status,'error');const savedId=item.id;
item.uploads.reference_audio=[];item.uploads.reference_videos=[];setReferenceVideoRange(extra,{start_seconds:100,duration_seconds:2});
await q.run({retry:true});assert.equal(item.id,savedId);assert.equal(bodies.length,2);
for(const body of bodies){assert.deepEqual(body.getAll('reference_videos'),[primary,extra]);assert.deepEqual(body.getAll('reference_audio'),[audio]);assert.deepEqual(JSON.parse(body.get('config')).reference_video_ranges,[{start_seconds:0,duration_seconds:15},{start_seconds:20,duration_seconds:3}]);}
''')


def test_per_job_attachment_validation_and_removing_audio_restores_shared_input():
    run_js(r'''
const audio=new File(['voice'],'voice.wav');let submitted=0;
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',prompt:'same'},videoField:'reference_videos',uploads:{reference_audio:[audio]}}),submit:async(f,item)=>{submitted++;return{id:item.id}}});
q.add([new File(['v'],'one.mp4'),new File(['v'],'two.mp4')]);
const first=q.items[0];q.addInputs(first.id,'reference_audio',[audio]);q.removeInput(first.id,'reference_audio',0);
assert.throws(()=>q.addInputs(first.id,'reference_audio',[new File(['bad'],'notes.txt')]),/supported audio/);
assert.throws(()=>q.addInputs(first.id,'reference_videos',[1,2,3].map(n=>new File(['v'],`${n}.mp4`))),/At most 2/);
assert.equal(first.uploads.reference_audio,undefined);await q.run();assert.equal(submitted,2);
assert.deepEqual(videoBatchFormData(first).getAll('reference_audio'),[audio]);
assert.deepEqual(videoBatchFormData(q.items[1]).getAll('reference_audio'),[audio]);
''')


def test_mka_is_audio_for_primary_batches_and_individual_companions():
    run_js(r'''
const local=new File(['matroska-audio'],'voice.MKA'),remote=serverVideoInput({root:'f',path:'voice.mka',size:1234,mtime_ns:'1780000000000000001'});
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{recipe:'seed_hunter',mode:'ref2va',prompt:'same',seed_hunter:{stage:'dialogue'}},videoField:'reference_audio'}),submit:async(f,item)=>{const body=videoBatchFormData(item);assert.deepEqual(body.getAll('reference_audio'),[local]);return{id:item.id}}});
q.inputField='reference_audio';assert.equal(q.add([local]).added,1);await q.run();
const videoQ=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'ref2va',prompt:'same'},videoField:'reference_videos'}),submit:async(f,item)=>{assert.equal(JSON.parse(videoBatchFormData(item).get('server_inputs')).reference_audio[0].path,'voice.mka');return{id:item.id}}});
videoQ.add([new File(['v'],'clip.mp4')]);videoQ.addInputs(videoQ.items[0].id,'reference_audio',[remote]);await videoQ.run();
''')


def test_required_audio_and_guides_are_checked_for_all_rows_before_any_submission():
    run_js(r'''
let sent=0;
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{recipe:'seed_hunter',mode:'ref2va',prompt:'same',seed_hunter:{stage:'preview',force_audio:true,first_frame_guide:true}},videoField:'reference_videos'}),submit:async(f,item)=>{sent++;return{id:item.id}}});
q.add([new File(['v'],'one.mp4'),new File(['v'],'two.mp4')]);
const audio=new File(['audio'],'one.wav'),image=new File(['img'],'one.png');
q.addInputs(q.items[0].id,'reference_audio',[audio]);q.addInputs(q.items[0].id,'reference_images',[image]);
await assert.rejects(q.run(),/two.mp4: Preserve supplied audio/);assert.equal(sent,0);assert.ok(q.items.every(item=>!item.snapshot));
q.addInputs(q.items[1].id,'reference_audio',[new File(['audio2'],'two.wav')]);
await assert.rejects(q.run(),/two.mp4: the first-frame guide/);assert.equal(sent,0);
q.addInputs(q.items[1].id,'reference_images',[new File(['img2'],'two.png')]);
await q.run();assert.equal(sent,2);
''')


def test_mixed_reference_order_and_nanosecond_precision_survive_multipart():
    run_js(r'''
const make=path=>serverVideoInput({root:'f',path,size:100,mtime_ns:'1780000000123456789'});
const local=new File(['local'],'local.mp4');const body=new FormData();
appendVideoInputs(body,{reference_videos:[make('first.mp4'),local,make('third.mp4')],reference_images:[new File(['img'],'img.png')]});
assert.deepEqual(JSON.parse(body.get('server_inputs')).reference_videos.map(item=>item.index),[0,2]);
assert.deepEqual(body.getAll('reference_videos'),[local]);
assert.equal(body.get('reference_images').name,'img.png');
assert.throws(()=>serverVideoInput({root:'f',path:'bad.mp4',size:2,mtime_ns:1780000000123456789}),/invalid video selection/);
''')


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


def test_seed_hunter_media_batches_keep_each_role_settings_and_no_auto_flag():
    run_js(r'''
for(const [videoField,name] of [['source_video','source.mp4'],['reference_audio','voice.flac'],['reference_images','face.png'],['first_frame','first.jpg']]){
 const base={recipe:'seed_hunter',mode:'ref2va',seed_hunter:{stage:videoField==='reference_audio'?'dialogue':videoField==='source_video'?'continuation':'preview',preview_count:3},prompt:'same'};
 const shared=new File(['shared'],'shared.png'),bodies=[];
 const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:base,videoField,autoVideoLength:false,uploads:{reference_images:[shared]}}),submit:async(f,item)=>{bodies.push(videoBatchFormData(item));return{id:item.id};}});
 q.inputField=videoField;q.add([new File(['media'],name)]);await q.run();
 assert.equal(bodies.length,1);assert.equal(bodies[0].getAll(videoField).length,1);assert.equal(bodies[0].has('auto_video_length'),false);
 assert.deepEqual(JSON.parse(bodies[0].get('config')),base);
 if(videoField!=='reference_images')assert.deepEqual(bodies[0].getAll('reference_images'),[shared]);
}
''')


def test_draft_copies_preserve_shared_inputs_and_independent_submission_ids():
    run_js(r'''
const bodies=[],image=new File(['face'],'face.png');
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{mode:'fl2va',prompt:'same',seed:42},videoField:'copies',uploads:{first_frame:[image]}}),submit:async(f,item)=>{bodies.push({id:item.id,body:videoBatchFormData(item)});return{id:item.id};}});
q.addCopies(3);await q.run();assert.equal(new Set(bodies.map(row=>row.id)).size,3);
for(const {body} of bodies){assert.deepEqual(body.getAll('first_frame'),[image]);assert.equal(body.has('copies'),false);assert.equal(body.has('auto_video_length'),false);}
assert.throws(()=>q.addCopies(0),/1–1000/);
''')


def test_automatic_source_uses_server_detection_but_explicit_manual_window_is_preserved():
    run_js(r'''
const automatic=new File(['v'],'auto.mp4'),manual=new File(['v'],'manual.mp4'),bodies=[];
setReferenceVideoRange(manual,{start_seconds:80,duration_seconds:7});
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{recipe:'seed_hunter',seed_hunter:{stage:'preview',preview_count:3},reference_video_ranges:[{start_seconds:999,duration_seconds:2}]},videoField:'reference_videos',autoVideoLength:true}),submit:async(f,item)=>{bodies.push(videoBatchFormData(item));return{id:item.id};}});
q.add([manual,automatic]);await q.run();
assert.equal(JSON.parse(bodies[0].get('config')).reference_video_ranges,undefined);
assert.deepEqual(JSON.parse(bodies[1].get('config')).reference_video_ranges,[{start_seconds:80,duration_seconds:7}]);
assert.ok(bodies.every(body=>body.get('auto_video_length')==='true'));
''')


def test_second_batch_uses_new_settings_while_prior_server_jobs_still_run():
    run_js(r'''
let draft={prompt:'First batch',steps:8,seed:42,loras:[{id:'first',strength:1}]};
const first=new File(['first'],'first.mp4'),jobs=[];
const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:draft,videoField:'reference_videos',autoVideoLength:true}),submit:async(f,item)=>{const job={id:item.id,status:jobs.length?'queued':'running',config:JSON.parse(JSON.stringify(item.snapshot.config))};jobs.push(job);return job;}});
q.add([first,new File(['second'],'second.mp4')]);await q.run();
assert.equal(q.running,false);assert.deepEqual(jobs.map(j=>j.status),['running','queued']);
const firstIds=jobs.map(j=>j.id);q.clearAccepted();
draft.prompt='Different batch';draft.steps=4;draft.seed=99;draft.loras[0].strength=.5;
assert.equal(q.add([first]).added,1);await q.run();
assert.equal(jobs.length,3);assert.equal(jobs[0].status,'running');assert.equal(jobs[1].status,'queued');assert.equal(jobs[2].config.prompt,'Different batch');
assert.equal(jobs[2].config.steps,4);assert.equal(jobs[2].config.seed,99);assert.equal(jobs[2].config.loras[0].strength,.5);
for(const job of jobs.slice(0,2)){assert.equal(job.config.prompt,'First batch');assert.equal(job.config.steps,8);assert.equal(job.config.seed,42);assert.equal(job.config.loras[0].strength,1);}
assert.ok(!firstIds.includes(jobs[2].id));
''')


def test_new_batch_cleanup_preserves_failed_and_paused_submission_snapshots():
    run_js(r'''
let count=0;const q=new VideoBatchQueue({family:'h3',getSnapshot:()=>({config:{prompt:'First'},videoField:'reference_videos'}),submit:async(f,item)=>{if(++count===2)throw Error('Response lost');return{id:item.id,status:'running'};}});
q.add([1,2,3].map(n=>new File(['clip'],`${n}.mp4`)));await q.run();
const unresolved=q.items.slice(1),ids=unresolved.map(row=>row.id),snapshots=unresolved.map(row=>row.snapshot);
q.clearAccepted();assert.deepEqual(q.items.map(row=>row.status),['error','pending']);assert.deepEqual(q.items.map(row=>row.id),ids);assert.deepEqual(q.items.map(row=>row.snapshot),snapshots);
q.add([new File(['next'],'4.mp4')]);assert.equal(q.items.length,3);
''')
