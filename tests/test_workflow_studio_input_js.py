"""Preview descriptors keep local, E/F and saved inputs on the same server."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_studio_preview_keeps_server_descriptors_and_never_fetches_until_play():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    module = (Path(__file__).resolve().parents[1] / 'static/js/workflowStudioInput.js').as_uri()
    source = f"import {{workflowStudioInputPreview as preview}} from '{module}';\n" + r"""
import assert from 'node:assert/strict';
globalThis.fetch=()=>{throw new Error('A preview descriptor must not open media');};
const origin='https://odysseus.example';
const file=new File(['image'],'source.png',{type:'image/png'});
assert.equal(preview([file],'h3',origin).file,file);
assert.equal(preview([file],'h3',origin).type,'image');
for(const family of ['h3','bfs'])for(const root of ['e','f']){
 const selection={root,path:'Videos/clip & one.mp4',size:1024,mtime_ns:'1234567890123456789'};
 const result=preview([{name:'clip & one.mp4',serverFile:selection}],family,origin);
 const url=new URL(result.url,origin);
 assert.equal(url.pathname,`/api/video/${family}/server-files/video`);
 assert.deepEqual(Object.fromEntries(url.searchParams),{...selection,size:'1024'});
 assert.equal(result.type,'video');
}
assert.equal(preview([{name:'retained.mp4',preview_url:'/api/video/h3/jobs/one/inputs/reference_videos/0'}],'h3',origin).url,origin+'/api/video/h3/jobs/one/inputs/reference_videos/0');
for(const url of ['https://other.example/api/video/h3/jobs/one/input','https://user:password@odysseus.example/api/video/h3/input','/api/video/bfs/other','javascript:alert(1)','/private/file'])
 assert.equal(preview([{name:'saved.mp4',preview_url:url}],'h3',origin),null);
assert.equal(preview([{name:'invalid.mp4',serverFile:{root:'e',path:'clip.mp4',size:-1,mtime_ns:'bad'}},file],'h3',origin).file,file);
assert.equal(preview([file],'unknown',origin),null);
"""
    result = subprocess.run([node, '--input-type=module', '-e', source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
