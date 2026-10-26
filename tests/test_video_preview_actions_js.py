"""One-click refinement skips accepted work and preserves idempotency on retry."""
from pathlib import Path
import subprocess
import shutil
import pytest


def test_default_refine_retries_only_unconfirmed_seed_and_recovers_deleted_result():
    node=shutil.which('node')
    if not node:pytest.skip('Node required')
    module=(Path(__file__).resolve().parents[1]/'static/js/videoPreviewActions.js').as_uri()
    script=r'''
import assert from 'node:assert/strict';
const {createPreviewActions}=await import(MODULE);
const storage=new Map();globalThis.sessionStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};
const posts=[];const accepted=new Map();let lose=true;
const job={id:'parent',seed_hunter:{stage:'preview',candidates:[1,2,3].map(index=>({index,seed:index*1000,can_refine:true}))},refinements:[]};
const request=async(path,options)=>{
 if(!options){
  if(path.endsWith('/seed-hunter'))return{config:{frames:98},refinement_defaults:{frames:362}};
  if(accepted.has(path.split('/').pop()))return{status:'queued'};
  throw Object.assign(new Error('Deleted'),{status:404});
 }
 const body=JSON.parse(options.body);posts.push(body);
 const id='candidate-'+body.candidate;
 accepted.set(id,body);
 if(body.candidate===2&&lose){lose=false;throw new Error('Lost response');}
 return{id};
};
const helper=createPreviewActions({request,defaults:(data,c)=>({seed:c.seed,frames:data.refinement_defaults.frames}),onChange(){},onQueued(){},isClosed:()=>false});
await helper.refine(job);
assert.equal(posts.length,2);assert.match(helper.state(job.id).error,/Lost response/);
await helper.refine(job);
assert.deepEqual(posts.map(p=>p.candidate),[1,2,2,3]);
assert.equal(posts[1].request_id,posts[2].request_id);
assert.deepEqual([...accepted.values()].map(p=>p.config.seed),[1000,2000,3000]);
assert.ok(posts.every(p=>p.config.frames===362));
await helper.refine(job);assert.equal(posts.length,4);
accepted.delete('candidate-1');await helper.refine(job,1);
assert.equal(posts.length,5);assert.notEqual(posts[0].request_id,posts[4].request_id);
const scout={id:'scout',seed_hunter:{stage:'scout'},scout:{seeds:[20,30,40],previews:[{seed:40},{seed:20}]},refinements:[]};
const sent=[];const scoutHelper=createPreviewActions({request:async(path,opt)=>{sent.push(JSON.parse(opt.body));return{id:'saved-'+sent.length};},defaults(){throw new Error('Scout uses its saved settings');},onChange(){},onQueued(){},isClosed:()=>false});
await scoutHelper.refine(scout,3);assert.equal(sent.length,1);assert.equal(sent[0].seed,40);
'''.replace('MODULE',repr(module))
    result=subprocess.run([node,'--input-type=module'],input=script,text=True,capture_output=True)
    assert result.returncode==0,result.stderr
