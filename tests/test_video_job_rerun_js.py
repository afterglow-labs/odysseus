"""Retry/reprocess copies one saved job and recovers ambiguous responses safely."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for job rerun checks')
    source = (ROOT / 'static/js/videoJobRerun.js').read_text()
    prelude = r'''
import assert from 'node:assert/strict';
const stored=new Map();globalThis.sessionStorage={getItem:k=>stored.get(k)||null,setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)};
const source={id:'original',revision:7,status:'failed',prompt:'Saved prompt'};
let calls=[],refreshes=0,changes=0;
const request=async(path,options)=>{calls.push({path,body:options?.body?JSON.parse(options.body):null});return path.startsWith('/queue/jobs')?{jobs:[source]}:{jobs:[{id:'a'.repeat(32),source_id:'original',status:'queued'}],succeeded:1,failed:0,request_id:JSON.parse(options.body).request_id.replaceAll('-','')}};
const make=options=>createVideoJobReruns({family:'h3',request,onChange:()=>changes++,onQueued:async()=>refreshes++,...options});
'''
    result = subprocess.run([node, '--input-type=module'], input=prelude+source+script,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_single_saved_job_empty_patch_and_labels_without_changing_draft():
    run_js(r'''
const draft={prompt:'Unrelated current draft',steps:99,files:[new File(['image'],'new.png')]};
const control=make();
assert.equal(control.state(source).label,'Retry');assert.equal(control.state({...source,status:'stopped'}).label,'Retry');
assert.equal(control.state({...source,status:'completed'}).label,'Reprocess');
for(const status of ['queued','running']){assert.equal(control.state({...source,status}).hidden,true);await control.run({...source,status});}
assert.equal(calls.length,0);await control.run(source);
assert.equal(calls.length,1);assert.equal(calls[0].path,'/queue/rerun');
assert.deepEqual(calls[0].body.jobs,[{id:'original',revision:7}]);assert.deepEqual(calls[0].body.patch,{});
assert.match(calls[0].body.request_id,/^[0-9a-f-]{36}$/);assert.match(control.state(source).message,/New job.*saved inputs/);
assert.equal(draft.prompt,'Unrelated current draft');assert.equal(draft.steps,99);assert.equal(draft.files[0].name,'new.png');
assert.equal(stored.size,0);assert.equal(refreshes,1);
const first=calls[0].body.request_id;await control.run(source);assert.notEqual(calls[1].body.request_id,first);
''')


def test_pending_identity_survives_lost_response_reopen_and_page_refresh():
    run_js(r'''
let first=true;const handler=async(path,options)=>{const result=await request(path,options);if(first){first=false;throw Error('Response lost')}return result};
let control=make({request:handler});await control.run(source);
assert.equal(control.state(source).label,'Retry request');assert.equal(stored.size,1);const initial=calls[0].body;
control=make({request:handler});assert.equal(control.state(source).label,'Retry request');
rerunSessions.clear();control=make({request:handler}); // Simulate reloading the JS while sessionStorage remains.
await control.run({...source,revision:8});assert.deepEqual(calls[1].body,initial);
assert.equal(control.state(source).label,'Retry');assert.equal(stored.size,0);assert.equal(refreshes,1);
''')


def test_partial_or_invalid_receipt_retries_same_payload_and_no_duplicate_inflight():
    run_js(r'''
let resolve;const gate=new Promise(r=>resolve=r);let first=true;
const control=make({request:async(path,options)=>{const result=await request(path,options);if(first){first=false;await gate;return{failed:1,errors:[{error:'Storage busy'}],jobs:[]}}return result}});
const running=control.run(source);assert.equal(control.state(source).disabled,true);assert.equal(control.state(source).label,'Queuing…');
await control.run(source);assert.equal(calls.length,1);resolve();await running;
assert.equal(control.state(source).error,'Storage busy');assert.equal(control.state(source).label,'Retry request');
await control.run(source);assert.deepEqual(calls[1].body,calls[0].body);assert.equal(refreshes,1);
''')


def test_old_list_revision_fallback_validates_source_and_does_not_rerun_another_job():
    run_js(r'''
const control=make();await control.run({...source,revision:undefined});
assert.equal(calls[0].path,'/queue/jobs?status=finished');assert.deepEqual(calls[1].body.jobs,[{id:'original',revision:7}]);
calls=[];const missing=make({request:async(path)=>{calls.push({path});return{jobs:[{id:'other',revision:2,status:'completed'}]}}});
await missing.run({id:'missing',status:'failed'});assert.equal(calls.length,1);assert.match(missing.state({id:'missing',status:'failed'}).error,/no longer ready/);
''')


def test_close_after_submission_retains_receipt_and_refresh_failure_is_not_rerun_failure():
    run_js(r'''
let closed=false;const control=make({isClosed:()=>closed,request:async(path,options)=>{closed=true;return request(path,options)}});
await control.run(source);assert.equal(stored.size,0);assert.equal(refreshes,0);
closed=false;const retry=make({onQueued:async()=>{throw Error('Refresh failed')}});await retry.run(source);
assert.equal(stored.size,0);assert.equal(retry.state(source).error,'');assert.match(retry.state(source).message,/Refresh jobs/);
''')


def test_authoritative_revision_conflict_allows_fresh_selection_but_unknown_conflict_keeps_identity():
    run_js(r'''
let message='A selected job was edited elsewhere. Refresh to load its latest settings.';
const control=make({request:async(path,options)=>{await request(path,options);throw Object.assign(Error(message),{status:409})}});
await control.run(source);assert.equal(stored.size,0);const first=calls[0].body.request_id;
message='A rerun staging directory is already in use';await control.run({...source,revision:8});assert.notEqual(calls[1].body.request_id,first);
assert.equal(stored.size,1);await control.run(source);assert.deepEqual(calls[2].body,calls[1].body);
''')


def test_edited_patch_freezes_on_ambiguous_response_and_validation_rejection_can_be_corrected():
    run_js(r'''
let fail='network';const control=make({request:async(path,options)=>{const value=await request(path,options);if(fail==='network')throw Error('Lost');if(fail==='validation')throw Object.assign(Error('Invalid LoRA'),{status:400});return value}});
const patch={prompt:'Change one thing',steps:6,loras:[{id:'turbo',strength:0.5}]};
await control.run(source,patch);patch.loras[0].strength=1;patch.steps=40;
fail='';await control.run(source,patch);assert.deepEqual(calls[1].body,calls[0].body);assert.equal(calls[1].body.patch.steps,6);assert.equal(calls[1].body.patch.loras[0].strength,0.5);
fail='validation';await control.run(source,{steps:500});assert.equal(control.state(source).pending,false);
const invalid=calls.at(-1).body;fail='';await control.run(source,{steps:10});assert.notEqual(calls.at(-1).body.request_id,invalid.request_id);assert.equal(calls.at(-1).body.patch.steps,10);
''')


def test_untrusted_receipt_never_clears_pending_or_reports_success():
    run_js(r'''
for(const change of [result=>result.request_id='b'.repeat(32),result=>result.succeeded=2,result=>result.jobs.push({...result.jobs[0]}),result=>result.jobs[0].source_id='other',result=>result.jobs[0].id='invalid']){
 const id='original';rerunSessions.clear();stored.clear();calls=[];
 const control=make({request:async(path,options)=>{const result=await request(path,options);change(result);return result}});
 const result=await control.run(source);assert.equal(result,null);assert.equal(control.state(source).pending,true);assert.equal(control.state(source).label,'Retry request');
}
''')
