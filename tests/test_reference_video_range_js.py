"""Selections retain video identity and never leak a different batch file's trim."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    source = "import assert from 'node:assert/strict';\n" + f"import * as ranges from '{(ROOT / 'static/js/referenceVideoRange.js').as_uri()}';\n" + script
    result = subprocess.run([node, '--input-type=module', '-e', source], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_ranges_follow_input_identity_removal_reorder_and_draft_restore():
    run_js("""
const first={name:'first.mp4'},second={name:'second.mp4'},third={name:'third.mp4'};
const state={retained:[first],files:[second,third]};
ranges.restoreReferenceVideoRanges(state,[{start_seconds:80,duration_seconds:5},{start_seconds:120,duration_seconds:6},{start_seconds:240,duration_seconds:4}]);
state.retained=[];state.files.reverse();
assert.deepEqual(ranges.referenceVideoRanges(state),[{start_seconds:240,duration_seconds:4},{start_seconds:120,duration_seconds:6}]);
const saved=ranges.referenceVideoRanges(state);ranges.setReferenceVideoRange(second,{start_seconds:999,duration_seconds:3});
ranges.restoreReferenceVideoRanges(state,saved);assert.equal(ranges.referenceVideoRange(second).start_seconds,120);
assert.equal(ranges.referenceVideoRange(first).start_seconds,80);
assert.deepEqual(ranges.referenceVideoRange({name:'second.mp4'}),{start_seconds:0,duration_seconds:15});
""")


def test_each_batch_item_snapshots_only_its_own_section_and_retry_keeps_it():
    run_js(f"""
import {{ VideoBatchQueue, videoBatchFormData }} from '{(ROOT / 'static/js/videoBatch.js').as_uri()}';
const a=new File(['a'],'a.mp4'),b=new File(['b'],'b.mp4');
ranges.setReferenceVideoRange(a,{{start_seconds:45,duration_seconds:15}});ranges.setReferenceVideoRange(b,{{start_seconds:95,duration_seconds:10}});
let fail=true;const sent=[];
const queue=new VideoBatchQueue({{family:'h3',getSnapshot:()=>({{videoField:'reference_videos',autoVideoLength:true,config:{{prompt:'same',reference_video_ranges:[{{start_seconds:999,duration_seconds:2}}]}}}}),submit:async(family,item)=>{{sent.push(JSON.parse(videoBatchFormData(item).get('config')));if(fail){{fail=false;throw new Error('lost reply');}}return{{id:item.id}};}}}});
queue.add([b,a]);await queue.run();ranges.setReferenceVideoRange(a,{{start_seconds:500,duration_seconds:2}});await queue.run({{retry:true}});await queue.run();
assert.deepEqual(sent.map(c=>c.reference_video_ranges),[[{{start_seconds:45,duration_seconds:15}}],[{{start_seconds:45,duration_seconds:15}}],[{{start_seconds:95,duration_seconds:10}}]]);
""")


def test_portable_ranges_validate_and_copy_without_host_metadata():
    run_js(f"""
import {{ resolveVideoWorkflow }} from '{(ROOT / 'static/js/videoWorkflow.js').as_uri()}';
const doc={{format:'odysseus-video-workflow',version:1,family:'h3',components:{{}},config:{{mode:'ref2va',reference_video_ranges:[{{start_seconds:120.5,duration_seconds:15,secret:'ignored'}}]}}}};
const result=resolveVideoWorkflow(doc,{{family:'h3',inventory:{{components:[]}}}});
assert.deepEqual(result.config.reference_video_ranges,[{{start_seconds:120.5,duration_seconds:15}}]);
doc.config.reference_video_ranges[0].start_seconds=-1;assert.throws(()=>resolveVideoWorkflow(doc,{{family:'h3',inventory:{{components:[]}}}}),/start time/);
""")
