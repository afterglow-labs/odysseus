"""Local preview uploads have explicit owners, cancellation, retries and disposal."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    harness = r'''
const sent=[],deleted=[];
globalThis.fetch=async(url,options)=>{deleted.push({url,...options});return {ok:true}};
class XHR {
 constructor(){this.upload={};sent.push(this)}
 open(method,url){this.method=method;this.url=url}
 send(body){this.body=body}
 abort(){this.aborted=true;this.onabort?.()}
 respond(status,data){this.status=status;this.responseText=JSON.stringify(data);this.onload()}
}
globalThis.XMLHttpRequest=XHR;
const receipt=(id='a'.repeat(32))=>({id,metadata:{duration_seconds:600},preview_url:`/api/video/h3/local-previews/${id}/video`,expires_at:Date.now()/1000+7200});
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
'''
    source = "import assert from 'node:assert/strict';\n" + f"import {{acquireLocalVideoPreview}} from '{(ROOT / 'static/js/videoLocalPreview.js').as_uri()}';\n" + harness + script
    result = subprocess.run([node, '--input-type=module', '-e', source], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_upload_error_cancel_retry_and_progress_do_not_replace_original_file():
    run_js(r'''
const file=new File(['original bytes'],'source.mkv'),lease=acquireLocalVideoPreview(file),progress=[];
assert.equal(sent.length,0);
let result=lease.load(value=>progress.push(value));
assert.equal(sent.length,1);assert.equal(sent[0].method,'POST');
assert.deepEqual([...sent[0].body.keys()],['video']);assert.equal(await sent[0].body.get('video').text(),'original bytes');
sent[0].upload.onprogress({lengthComputable:true,loaded:5,total:14});
assert.deepEqual(progress.at(-1),{phase:'uploading',loaded:5,total:14});
sent[0].respond(507,{detail:'Not enough preview disk space.'});
await assert.rejects(result,/Not enough preview disk space/);
result=lease.load();lease.cancel();await assert.rejects(result,{name:'AbortError'});
assert.equal(sent[1].aborted,true);
result=lease.load();sent[2].upload.onload();sent[2].respond(201,receipt());
assert.equal((await result).id,'a'.repeat(32));
assert.equal(await file.text(),'original bytes');
lease.release();await sleep(300);assert.equal(deleted.length,1);
''')


def test_pending_upload_and_complete_token_reused_by_same_file_without_cross_file_leaks():
    run_js(r'''
const file=new File(['same'],'same.mkv');
let first=acquireLocalVideoPreview(file),result=first.load();first.release();
let second=acquireLocalVideoPreview(file),joined=second.load();assert.equal(sent.length,1);
sent[0].respond(201,receipt());assert.equal((await result).id,(await joined).id);
second.release();let third=acquireLocalVideoPreview(file);assert.equal((await third.load()).id,'a'.repeat(32));
await sleep(300);assert.equal(deleted.length,0);assert.equal(sent.length,1);
const other=acquireLocalVideoPreview(new File(['different'],'same.mkv')),otherResult=other.load();
assert.equal(sent.length,2);sent[1].respond(201,receipt('b'.repeat(32)));await otherResult;
third.release();other.release();await sleep(300);assert.equal(deleted.length,2);
''')


def test_closed_upload_aborts_ignores_late_response_and_deletes_orphan():
    run_js(r'''
const lease=acquireLocalVideoPreview(new File(['original'],'source.mkv'));
const result=lease.load();const failure=assert.rejects(result,{name:'AbortError'});lease.release();
await sleep(300);await failure;assert.equal(sent[0].aborted,true);
// Simulate a late successful server response despite the transport abort.
sent[0].respond(201,receipt());await sleep(0);
assert.equal(deleted.length,1);assert.equal(deleted[0].method,'DELETE');
await assert.rejects(lease.load(),{name:'AbortError'});
''')


def test_expired_invalid_and_explicitly_invalidated_receipts_retry_with_fresh_source():
    run_js(r'''
const lease=acquireLocalVideoPreview(new File(['original'],'source.mkv'));
let result=lease.load();const bad=receipt();bad.preview_url='https://other.test/video';sent[0].respond(201,bad);
await assert.rejects(result,/valid compatible preview/);assert.equal(deleted.length,1);
result=lease.load();const expired=receipt('b'.repeat(32));expired.expires_at=1;sent[1].respond(201,expired);await result;
result=lease.load();assert.equal(sent.length,3);assert.equal(deleted.length,2);sent[2].respond(201,receipt('c'.repeat(32)));await result;
lease.invalidate('incorrect');assert.equal((await lease.load()).id,'c'.repeat(32));assert.equal(sent.length,3);
lease.invalidate('c'.repeat(32));result=lease.load();assert.equal(sent.length,4);sent[3].respond(201,receipt('d'.repeat(32)));await result;
lease.release();await sleep(300);assert.equal(deleted.length,4);
''')
