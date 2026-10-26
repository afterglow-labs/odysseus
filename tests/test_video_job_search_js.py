"""All-page job selection preserves the server's acknowledged search scope."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    source = (ROOT / 'static/js/h3AutoRefine.js').read_text()
    result = subprocess.run([node, '--input-type=module'],
                            input="import assert from 'node:assert/strict';\n" + source + '\n' + script,
                            text=True, capture_output=True, cwd=ROOT / 'static/js')
    assert result.returncode == 0, result.stderr


def test_all_matching_pages_share_query_anchor_and_parent_grouping():
    run_js(r'''
const calls=[];
const parent=id=>({id,status:'completed',seed_hunter:{stage:'preview'}});
const rows=await allPreviewJobs(async path=>{
 const query=new URL(path,'https://fixture.test').searchParams;calls.push(query);
 const page=Number(query.get('page'));
 return {search_query:'Harbor & Mix',jobs:page===1?[parent('one'),{id:'running',status:'running',seed_hunter:{stage:'preview'}}]:[parent('two'),{id:'final',status:'completed',seed_hunter:{stage:'final'}}],pagination:{page,total_pages:2,before:54321}};
},false,'  Harbor & Mix  ');
assert.deepEqual(rows.map(job=>job.id),['one','two']);
assert.equal(calls.length,2);
for(const query of calls){assert.equal(query.get('q'),'Harbor & Mix');assert.equal(query.get('stage'),'not_scout');assert.equal(query.get('group_refinements'),'true');assert.equal(query.get('page_size'),'50');}
assert.equal(calls[0].has('before'),false);assert.equal(calls[1].get('before'),'54321');
const scoutCalls=[];
const scout=await allPreviewJobs(async path=>{scoutCalls.push(new URL(path,'https://fixture.test').searchParams);return {search_query:'coast',jobs:[{id:'scout',status:'stopped',seed_hunter:{stage:'scout'},scout:{mode:'scout'}},{id:'child',status:'completed',seed_hunter:{stage:'scout',source_job_id:'parent'}}]};},true,'coast');
assert.deepEqual(scout.map(job=>job.id),['scout']);assert.equal(scoutCalls[0].get('stage'),'scout');assert.equal(scoutCalls[0].has('group_refinements'),false);
''')


def test_old_server_cannot_silently_select_unfiltered_jobs():
    run_js(r'''
const all={jobs:[{id:'unrelated',status:'completed',seed_hunter:{stage:'preview'}}]};
await assert.rejects(()=>allPreviewJobs(async()=>all,false,'harbor'),/Update the Odysseus server/);
await assert.rejects(()=>allPreviewJobs(async()=>({...all,search_query:'coast'}),false,'harbor'),/Update the Odysseus server/);
assert.equal(checkedJobSearchResponse(all,''),all);
assert.deepEqual((await allPreviewJobs(async()=>all,false)).map(job=>job.id),['unrelated']);
''')


def test_aborted_selection_never_collects_more_pages_or_returns_stale_rows():
    run_js(r'''
const controller=new AbortController();let calls=0;
await assert.rejects(()=>allPreviewJobs(async(path,options)=>{
 calls++;assert.equal(options.signal,controller.signal);controller.abort();
 return {search_query:'harbor',jobs:[{id:'old',status:'completed',seed_hunter:{stage:'preview'}}],pagination:{page:1,total_pages:10,before:54321}};
},false,'harbor',{signal:controller.signal}),{name:'AbortError'});
assert.equal(calls,1);
''')
