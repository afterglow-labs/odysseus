"""Import inspection must never apply a stale or unsupported conversion."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_conversion_cancels_stale_fetches_file_reads_and_closed_reports():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    module = (Path(__file__).resolve().parents[1] / 'static/js/workflowConversion.js').as_uri()
    source = f"import {{createWorkflowConversion}} from '{module}';\n" + r"""
import assert from 'node:assert/strict';
class Element {
 constructor(tag) { this.tagName=tag; this.children=[]; this.value=''; this.textContent=''; }
 setAttribute() {}
 append(...nodes) { this.children.push(...nodes); }
 replaceChildren(...nodes) { this.children=nodes; this.value=''; }
 add(node) { this.append(node); }
 click() { if(!this.disabled) this.onclick?.(); }
}
globalThis.document={createElement:tag=>new Element(tag)};
globalThis.Option=class extends Element { constructor(text,value){super('option');this.textContent=text;this.value=value;} };
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const tick=async()=>{await Promise.resolve();await Promise.resolve();};
const requests=[];
globalThis.fetch=(url,options)=>{const pending=deferred();requests.push({url,options,...pending});return pending.promise;};
const response=result=>({ok:true,json:async()=>result});
const valid={format:'odysseus-comfyui-conversion',version:1,family:'h3',convertible:true,config:{prompt:'new draft'},inputs:[],outputs:[],issues:[]};
const applied=[];
const ui=createWorkflowConversion({family:'h3',onApply:r=>applied.push(r)});
const descend=node=>[node,...node.children.flatMap(descend)];
const elements=descend(ui.panel);
const picker=elements.find(e=>e.type==='file');
const apply=elements.find(e=>e.textContent==='Apply to draft');
const close=elements.find(e=>e.textContent==='Close report');
const status=elements.find(e=>e.tagName==='p');
const choose=(name,text)=>{picker.files=[{name,size:100,text}];return picker.onchange();};

// Selecting a second file invalidates the first fetch before the new file is read.
const first=choose('first.json',async()=>'{}');await tick();
assert.equal(requests.length,1);
const secondRead=deferred(), second=choose('second.json',()=>secondRead.promise);
assert.equal(requests[0].options.signal.aborted,true);
requests[0].resolve(response(valid));await first;
assert.equal(apply.disabled,true);apply.onclick();assert.equal(applied.length,0);
secondRead.resolve('{}');await tick();assert.equal(requests.length,2);
close.onclick();requests[1].resolve(response(valid));await second;
assert.equal(ui.panel.hidden,true);apply.onclick();assert.equal(applied.length,0);

// Closing during a pending file read must not reopen the report or start a request.
const thirdRead=deferred(),third=choose('third.json',()=>thirdRead.promise);
close.onclick();thirdRead.resolve('{}');await third;
assert.equal(ui.panel.hidden,true);assert.equal(requests.length,2);

// A current matching report applies once, only on explicit Apply.
const fourth=choose('fourth.json',async()=>'{}');await tick();
requests[2].resolve(response(valid));await fourth;
assert.equal(ui.panel.hidden,false);assert.equal(apply.disabled,false);assert.equal(applied.length,0);
apply.onclick();apply.onclick();assert.equal(applied.length,1);assert.equal(applied[0],valid);

for(const result of [
 {...valid,convertible:false,config:null},
 {...valid,family:'bfs'},
 {...valid,config:{recipe:'seed_hunter'}},
 {...valid,config:null},
 {...valid,format:'unexpected'}
]) {
 const pending=choose('blocked.json',async()=>'{}');await tick();
 requests.at(-1).resolve(response(result));await pending;
 assert.equal(apply.disabled,true);apply.onclick();assert.equal(applied.length,1);
}
assert.match(status.textContent,/invalid workflow conversion report/);

// Destroy invalidates late reads and responses just like closing does.
const last=choose('last.json',async()=>'{}');await tick();
const current=requests.at(-1);ui.destroy();assert.equal(current.options.signal.aborted,true);
current.resolve(response(valid));await last;apply.onclick();assert.equal(applied.length,1);
"""
    result = subprocess.run([node, '--input-type=module', '-e', source], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
