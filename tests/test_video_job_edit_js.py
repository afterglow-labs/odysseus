"""Queued edits retain remote media and preserve the unsent draft on conflicts."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for queued editor checks')
    source = (ROOT / 'static/js/videoJobEdit.js').read_text()
    subprocess.run([node, '--input-type=module', '-e',
                    "import assert from 'node:assert/strict';\n" + source + '\n' + script],
                   check=True, capture_output=True, text=True)


DOM = r'''
const elements=[];
globalThis.document={createElement(tag){
 const element={tag,children:[],classList:{toggle(){}},setAttribute(){},append(...items){this.children.push(...items)},scrollIntoView(){}};
 elements.push(element);return element;
}};
const form={prepend(){},querySelector(){return{focus(){}}}};
let draft={prompt:'Unsent draft',files:[new File(['local'],'draft.mp4')]},busy=false,closed=false,error='',applied=null,restored=null;
const original=draft;
const payload={id:'queued-one',revision:4,config:{prompt:'Original queued prompt'},inputs:{reference_videos:[{index:0,name:'one.mp4',size:3},{index:1,name:'two.mp4',size:4}]}};
const calls=[];
const make=(extra={})=>createVideoJobEditor({form,request:async path=>{calls.push(path);return payload},
 getDraft:()=>draft,applyEdit:value=>{applied=value;draft={prompt:value.config.prompt}},
 restoreDraft:value=>{restored=value;draft=value},onChange(){},onError:value=>{error=value},
 isBusy:()=>busy,isClosed:()=>closed,...extra});
'''


def test_edit_transport_keeps_remote_reference_order_without_reuploading_and_appends_new_files():
    run_js(r'''
const file=new File(['new-video'],'new.mp4');
const body=videoJobEditFormData({revision:7},{mode:'ref2va',prompt:'Changed'},
 {reference_videos:[file]}, {reference_videos:[{index:1,name:'second.mp4'},{index:0,name:'first.mp4'}],reference_audio:[]});
assert.equal(body.get('revision'),'7');
assert.deepEqual(JSON.parse(body.get('retain_inputs')),{reference_videos:[1,0],reference_audio:[]});
assert.equal(body.getAll('reference_videos').length,1);
assert.equal(body.get('reference_videos').name,'new.mp4');
assert.equal(body.get('reference_videos').size,9);
assert.equal(body.get('first_frame'),null);
assert.equal(JSON.parse(body.get('config')).prompt,'Changed');
''')


def test_cancel_restores_original_draft_with_local_files_and_does_not_submit():
    run_js(DOM + r'''
const editor=make();await editor.open('queued-one');
assert.deepEqual(calls,['/jobs/queued-one/edit']);assert.equal(applied,payload);
assert.equal(editor.active.revision,4);assert.equal(draft.prompt,'Original queued prompt');
draft.prompt='Unsaved job changes';
elements.find(item=>item.tag==='button').onclick();
assert.equal(editor.active,null);assert.equal(restored,original);
assert.equal(draft.files[0].name,'draft.mp4');assert.equal(calls.length,1);
''')


def test_conflict_keeps_edits_visible_and_original_draft_until_explicit_cancel():
    run_js(DOM + r'''
const editor=make();await editor.open('queued-one');draft.prompt='Keep my correction';
editor.failed(Object.assign(Error('Revision conflict'),{status:409}));
assert.match(editor.blocked,/started or changed/);assert.equal(editor.active.id,'queued-one');
assert.equal(draft.prompt,'Keep my correction');assert.equal(restored,null);assert.equal(error,'Revision conflict');
editor.observe([{id:'queued-one',status:'running',revision:4}]);assert.match(editor.blocked,/left the queue/);
assert.equal(draft.prompt,'Keep my correction');editor.finish();assert.equal(draft,original);
''')


def test_polling_accepts_older_snapshot_but_blocks_newer_revision_and_preserves_edits():
    run_js(DOM + r'''
const editor=make();await editor.open('queued-one');
editor.observe([{id:'queued-one',status:'queued',revision:3}]);assert.equal(editor.blocked,'');
editor.observe([{id:'queued-one',status:'queued',revision:4}]);assert.equal(editor.blocked,'');
editor.observe([{id:'queued-one',status:'queued',revision:5}]);assert.match(editor.blocked,/changed elsewhere/);
assert.equal(editor.active,payload);assert.equal(restored,null);
''')


def test_loading_prevents_other_editor_and_snapshots_draft_after_request_finishes():
    run_js(DOM + r'''
let release;const gate=new Promise(resolve=>{release=resolve});
const editor=make({request:async()=>{await gate;return payload}});
const loading=editor.open('queued-one');assert.equal(editor.loading,true);
await editor.open('second-job');draft={prompt:'Updated while loading',files:original.files};
release();await loading;assert.equal(editor.loading,false);
editor.finish();assert.equal(restored.prompt,'Updated while loading');assert.equal(restored.files,original.files);
''')


def test_failed_apply_rolls_back_original_draft_and_loading_state():
    run_js(DOM + r'''
const editor=make({applyEdit(){draft={prompt:'Partially changed'};throw Error('Missing workflow')}});
await editor.open('queued-one');assert.equal(editor.active,null);assert.equal(editor.loading,false);
assert.equal(draft,original);assert.match(error,/Missing workflow/);
''')
