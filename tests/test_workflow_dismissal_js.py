"""Workflow drafts survive selection drags, backdrop clicks and Escape.

Exercise the shipped event handlers with the real dismissal registry. Browser
selection can dispatch its final click at the common ancestor (the backdrop),
so neither that click nor Escape may destroy the draft's files/batch state.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def handlers(kind):
    source = (ROOT / "static/js" / ("bfsVideo.js" if kind == "bfs" else "h3Video.js")).read_text()
    if kind == "export":
        return "  const close = bindMenuDismiss" + source.split("  const close = bindMenuDismiss", 1)[1].split("  form.addEventListener('submit'", 1)[0]
    tail = "  const onKey = event => {" + source.rsplit("  const onKey = event => {", 1)[1]
    end = "  function applyEditorDraft" if kind == "h3" else "  for (const state of files.values()) state.wrap.hidden = true;"
    return tail.split(end, 1)[0]


@pytest.mark.parametrize("kind", ["h3", "bfs", "export"])
def test_workflow_only_closes_via_explicit_button(kind):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for workflow dismissal checks")
    registry = (ROOT / "static/js/escMenuStack.js").read_text()
    script = r"""
import assert from 'node:assert/strict';
class Events {
  constructor() { this.handlers = new Map(); }
  addEventListener(type, fn, capture) { const list = this.handlers.get(type) || []; list.push({fn,capture}); this.handlers.set(type,list); }
  removeEventListener(type, fn) { this.handlers.set(type,(this.handlers.get(type)||[]).filter(item=>item.fn!==fn)); }
  dispatch(type, event) { for(const {fn} of [...this.handlers.get(type)||[]]) { fn(event); if(event.stopped)break; } }
}
globalThis.window = new Events(); globalThis.document = new Events();
document.querySelector = () => null;
let parentEscapes = 0;
document.addEventListener('keydown', event => { if(event.key==='Escape') { parentEscapes++; dismissTopMenu(); } },true);
let closed = false, destroyed = 0, exporting = false, aborted = 0, controller = null;
const timer = 0, pollTimer = 0;
const batchQueue = {destroy(){destroyed++;}}, queueControls = {destroy(){}};
const enhancementController = null, gpuController = null, files = new Map(), jobNodes = new Map();
const overlay = {connected:true,remove(){this.connected=false;}}, anchor = {isConnected:true,focus(){}};
const dialog = {querySelectorAll(){return [];}};
const closeButton = {focus(){}}, cancel = {focus(){}};
function event(values={}) { return {preventDefault(){this.defaultPrevented=true;},stopImmediatePropagation(){this.stopped=true;},...values}; }
function fire(type, values) { const ev=event(values); window.dispatch(type,ev); if(!ev.stopped)document.dispatch(type,ev); return ev; }
""" + registry + handlers(kind) + r"""
await new Promise(resolve=>setTimeout(resolve,0));
assert.equal(_openMenuCount(),1);
// Plain clicks, a drag starting in a text field and ending on the backdrop,
// and the inverse must all leave this live draft connected.
for(const downTarget of [overlay,dialog]) {
  fire('pointerdown',{target:downTarget,button:0,pointerId:1});
  fire('pointerup',{target:overlay,button:0,pointerId:1});
  fire('click',{target:overlay,button:0,detail:1});
  assert.equal(overlay.connected,true);
  assert.equal(closed,false);assert.equal(destroyed,0);
}
const escape = fire('keydown',{key:'Escape',target:dialog});
assert.equal(escape.defaultPrevented,true);assert.equal(escape.stopped,true);
assert.equal(parentEscapes,0);assert.equal(overlay.connected,true);assert.equal(_openMenuCount(),1);
assert.ok(window.handlers.get('keydown').every(handler=>handler.capture===true));
""" + ("cancel.onclick();" if kind == "export" else "closeButton.onclick();") + r"""
assert.equal(overlay.connected,false);assert.equal(closed,true);assert.equal(_openMenuCount(),0);
assert.equal(window.handlers.get('keydown').length,0);
assert.equal(document.handlers.get('click').length,0);
close();assert.equal(_openMenuCount(),0); // Explicit teardown remains idempotent.
"""
    result = subprocess.run([node, "--input-type=module"], input=script,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
