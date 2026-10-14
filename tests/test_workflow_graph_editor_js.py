"""Both workflow views mutate typed execution state, never presentation-only links."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_node(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    module = (ROOT / 'static/js/workflowGraphStore.js').as_uri()
    result = subprocess.run([node, '--input-type=module', '-e', f"import {{createWorkflowGraphStore, connectionIssue, graphOrder}} from {json.dumps(module)};\n" + body], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_connections_replace_input_preserve_multi_refs_and_undo():
    run_node(r'''
import assert from 'node:assert/strict';
const catalog={node_types:[
 {type:'input.video',outputs:[{id:'video',type:'VIDEO'}]},
 {type:'media.trim',inputs:[{id:'video',type:'VIDEO'}],outputs:[{id:'video',type:'VIDEO'}]},
 {type:'input.prompt',outputs:[{id:'text',type:'TEXT'}]},
 {type:'refs',inputs:[{id:'video',type:'VIDEO',multiple:true}]},
 {type:'output',inputs:[{id:'result',type:'RESULT|VIDEO'}]}
]};
const graph={format:'odysseus-workflow-graph',version:1,title:'Keep source',nodes:[{id:'a',type:'input.video',params:{name:'a.mp4'}},{id:'b',type:'input.video',params:{name:'b.mp4'}},{id:'trim',type:'media.trim',params:{start:3}},{id:'refs',type:'refs'},{id:'prompt',type:'input.prompt'},{id:'out',type:'output'}],edges:[]};
const s=createWorkflowGraphStore(graph,catalog);
s.connect({node:'a',port:'video'},{node:'trim',port:'video'});
s.connect({node:'b',port:'video'},{node:'trim',port:'video'});
assert.equal(s.document.edges.length,1);assert.equal(s.document.edges[0].source.node,'b');
s.undo();assert.equal(s.document.edges[0].source.node,'a');s.redo();assert.equal(s.document.edges[0].source.node,'b');
s.connect({node:'a',port:'video'},{node:'refs',port:'video'});
s.connect({node:'b',port:'video'},{node:'refs',port:'video'});
assert.equal(s.document.edges.filter(e=>e.target.node==='refs').length,2);
s.connect({node:'trim',port:'video'},{node:'out',port:'result'}); // union ports
assert.throws(()=>s.connect({node:'prompt',port:'text'},{node:'trim',port:'video'}),/TEXT cannot connect/);
assert.throws(()=>s.connect({node:'trim',port:'video'},{node:'trim',port:'video'}),/itself/);
const clone=s.duplicate('trim');assert.equal(s.document.nodes.find(n=>n.id===clone).params.start,3);
assert.equal(s.document.edges.find(e=>e.target.node===clone).source.node,'b');
s.parameter(clone,'start',8);assert.equal(s.document.nodes.find(n=>n.id==='trim').params.start,3);
s.remove('b');assert(!s.document.edges.some(e=>e.source.node==='b'));s.undo();assert(s.document.edges.some(e=>e.source.node==='b'));
assert.equal(graph.edges.length,0,'Store never mutates saved/queued source snapshots');
''')


def test_cycles_unknown_imported_state_and_graph_layout():
    run_node(r'''
import assert from 'node:assert/strict';
const catalog={node_types:[{type:'op',inputs:[{id:'i',type:'VIDEO'}],outputs:[{id:'o',type:'VIDEO'}]}]};
const original={format:'odysseus-workflow-graph',version:1,title:'Imported',metadata:{source:'ComfyUI'},nodes:[{id:'a',type:'op',params:{value:1}},{id:'b',type:'op',params:{}},{id:'c',type:'op',params:{}},{id:'unsupported',type:'comfy.CustomOperation',params:{nested:{keep:true}},state:'disabled',original:{widgets_values:[12,'keep']}}],edges:[]};
const s=createWorkflowGraphStore(original,catalog);
s.connect({node:'a',port:'o'},{node:'b',port:'i'});s.connect({node:'b',port:'o'},{node:'c',port:'i'});
assert.throws(()=>s.connect({node:'c',port:'o'},{node:'a',port:'i'}),/cycle/);
const edges=JSON.stringify(s.document.edges);s.layout();assert.equal(JSON.stringify(s.document.edges),edges);
assert.equal(s.document.nodes.find(n=>n.id==='b').position.x-s.document.nodes.find(n=>n.id==='a').position.x,288);
assert.deepEqual(s.document.nodes.find(n=>n.id==='unsupported'),{...original.nodes[3],position:s.document.nodes.find(n=>n.id==='unsupported').position});
s.patch('b',{id:'spoof',type:'evil',state:'bypassed'});assert.equal(s.document.nodes[1].id,'b');assert.equal(s.document.nodes[1].type,'op');
const exportSnapshot=JSON.parse(JSON.stringify(s.document));s.parameter('a','value',99);assert.equal(exportSnapshot.nodes[0].params.value,1);
s.replace(exportSnapshot);assert.equal(s.document.nodes[0].params.value,1);assert(!s.canUndo);assert.equal(s.document.metadata.source,'ComfyUI');
assert.equal(graphOrder(s.document).findIndex(n=>n.id==='a')<graphOrder(s.document).findIndex(n=>n.id==='c'),true);
''')


def test_dynamic_metadata_controls_wildcard_ports_and_archive_outputs():
    presentation = (ROOT / 'static/js/workflowGraphPresentation.js').as_uri()
    run_node(f"import {{graphFieldMultiline, graphOutputKind, graphPortHelp}} from {json.dumps(presentation)};\n" + r'''
import assert from 'node:assert/strict';
assert.equal(graphFieldMultiline({id:'filename_prefix',type:'text'},{execution:'comfy'}),false);
assert.equal(graphFieldMultiline({id:'text',type:'text',metadata:{multiline:true}},{execution:'comfy'}),true);
assert.equal(graphFieldMultiline({id:'prompt',type:'text',multiline:false},{execution:'comfy'}),false);
assert.equal(graphFieldMultiline({id:'prompt',type:'text'}),true);
assert.equal(graphOutputKind({name:'result.zip',url:'/api/workflow-graphs/runs/1/outputs/a'}),'archive');
assert.equal(graphOutputKind({name:'opaque',mime_type:'application/zip'}),'archive');
assert.equal(graphOutputKind({name:'render.mp4'}),'video');
assert.equal(graphOutputKind({name:'render.webp'}),'image');
assert.equal(graphOutputKind({name:'sound.wav'}),'audio');
assert.equal(graphOutputKind({name:'result.json'}),'download');
assert.match(graphPortHelp({required:false,force_input:true,is_list:true}),/optional.*connection-only.*list through one connection/);
assert.match(graphPortHelp({},true),/overrides the setting/);
const catalog={node_types:[
 {type:'source',outputs:[{id:'mixed',type:'IMAGE|MASK'},{id:'any',type:'*'}]},
 {type:'target',inputs:[{id:'image',type:'IMAGE'},{id:'any',type:'*'}]}
]};
const s=createWorkflowGraphStore({nodes:[{id:'s',type:'source'},{id:'t',type:'target'}],edges:[]},catalog);
s.connect({node:'s',port:'mixed'},{node:'t',port:'image'});
s.connect({node:'s',port:'any'},{node:'t',port:'image'});
s.connect({node:'s',port:'mixed'},{node:'t',port:'any'});
assert.equal(s.document.edges.length,2);
''')


def test_reference_attachments_are_real_multi_inputs_and_atomic_undo():
    run_node(r'''
import assert from 'node:assert/strict';
const catalog={node_types:[
 {type:'input.video',outputs:[{id:'video',type:'VIDEO'}]},
 {type:'input.image',outputs:[{id:'image',type:'IMAGE'}]},
 {type:'input.references',inputs:[{id:'reference_videos',type:'VIDEO',multiple:true},{id:'source_video',type:'VIDEO'},{id:'first_frame',type:'IMAGE'}]}
]};
const s=createWorkflowGraphStore({nodes:[{id:'refs',type:'input.references'}],edges:[]},catalog);
const target={node:'refs',port:'reference_videos'};
const ids=s.attachMedia(target,'video',['first.mp4','second.mp4']);
assert.equal(s.document.edges.length,2);
assert.deepEqual(s.document.nodes.filter(n=>n.type==='input.video').map(n=>n.params.name),['first.mp4','second.mp4']);
assert.deepEqual(s.document.edges.map(e=>e.source.node),ids);
s.attachMedia(target,'video',['third.mp4']);assert.equal(s.document.edges.length,3);
s.undo();assert.equal(s.document.edges.length,2,'One attach action is one undo');
s.redo();assert.equal(s.document.edges.length,3);
const source={node:'refs',port:'source_video'};
const [old]=s.attachMedia(source,'video',['old-source.mp4']);
const [next]=s.attachMedia(source,'video',['new-source.mp4']);
assert(!s.document.nodes.some(n=>n.id===old));assert(s.document.nodes.some(n=>n.id===next));
assert.equal(s.document.edges.filter(e=>e.target.port==='reference_videos').length,3);
s.undo();assert(s.document.nodes.some(n=>n.id===old));assert(!s.document.nodes.some(n=>n.id===next));
assert.throws(()=>s.attachMedia(source,'video',['one.mp4','two.mp4']),/one file/);
assert.throws(()=>s.attachMedia({node:'refs',port:'first_frame'},'video',['wrong.mp4']),/VIDEO cannot connect to IMAGE/);
s.connect({node:ids[0],port:'video'},source);
const reference=s.document.edges.find(e=>e.source.node===ids[0]&&e.target.port==='reference_videos');s.detachMedia(reference.id);
assert(s.document.nodes.some(n=>n.id===ids[0]),'Remove reference must preserve input used by another slot');
s.undo();assert(s.document.edges.some(e=>e.id===reference.id));
''')
