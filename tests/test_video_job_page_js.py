"""Bounded job history, stable polling anchors, and on-demand media transport."""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def run_js(script):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    source = '\n'.join((ROOT / 'static/js' / name).read_text().replace("import { createVideoBatchHistory } from './videoBatchHistory.js';", '')
                       for name in ('videoBatchHistory.js', 'videoJobPage.js', 'videoJobMedia.js'))
    result = subprocess.run([node, '--input-type=module'],
                            input="import assert from 'node:assert/strict';\n" + source + '\n' + script,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


DOM = r'''
class Element {
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};this.listeners={};this.isConnected=true;this.paused=true;this.loads=0;}
 setAttribute(key,value){this.attrs[key]=value;}
 getAttribute(key){return this.attrs[key]??null;}
 removeAttribute(key){delete this.attrs[key];}
 set src(value){this.attrs.src=value;}
 get src(){return this.attrs.src||'';}
 append(...nodes){this.children.push(...nodes);}
 insertBefore(node,before){const i=this.children.indexOf(before);if(i<0)this.children.push(node);else this.children.splice(i,0,node);}
 addEventListener(name,callback){this.listeners[name]=callback;}
 querySelectorAll(selector){return this.children.flatMap(child=>[...(selector.includes(child.tag)?[child]:[]),...child.querySelectorAll(selector)]);}
 async play(){this.paused=false;}
 pause(){this.paused=true;}
 load(){this.loads++;}
 remove(){}
}
globalThis.document={createElement:tag=>new Element(tag),createTextNode:text=>({textContent:text})};
const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
'''


def test_first_page_anchor_navigation_and_refresh_use_bounded_queries():
    run_js(r'''
const state=createVideoJobPageState({recipe:'seed_hunter'});
assert.equal(state.query(),'/jobs?page=1&page_size=10&recipe=seed_hunter');
state.accept({jobs:[{id:'one'}],counts:{running:1,queued:77},pagination:{page:1,total:107,total_pages:11,before:1234}});
assert.match(state.query({page:2}),/page=2.*before=1234/);
state.accept({jobs:[],counts:{running:1,queued:77},pagination:{page:2,total:107,total_pages:11,before:1234}});
assert.match(state.query(),/page=2.*before=1234/);
assert.equal(state.label,'11–20 of 107 jobs · Page 2 of 11');
assert.equal(state.counts.queued,77);
assert.equal(state.query({reset:true}),'/jobs?page=1&page_size=10&recipe=seed_hunter');
''')


def test_server_clamped_page_and_legacy_filtered_array_keep_ten_visible():
    run_js(r'''
const state=createVideoJobPageState({recipe:'seed_hunter'});
const rows=Array.from({length:123},(_,id)=>({id,status:'completed',recipe:id%2?'other':'seed_hunter'}));
const page=state.accept({jobs:rows},{page:2});
assert.equal(page.jobs.length,10);assert.equal(page.jobs[0].id,20);assert.equal(state.total,62);
assert.equal(state.counts.completed,62);
state.accept({jobs:[],counts:{},pagination:{page:1,total:0,total_pages:1,before:321}});
assert.equal(state.label,'0 jobs');assert.match(state.query(),/page=1.*before=321/);
''')


def test_navigation_and_new_submission_are_not_lost_behind_a_poll():
    run_js(DOM + r'''
let finish;const calls=[],pages=[];
const pager=createVideoJobPager({container:new Element('div'),request:path=>{calls.push(path);return new Promise(resolve=>finish=resolve)},onData:data=>pages.push(data.jobs[0]?.id),onError:error=>{throw error}});
const initial=pager.load();await flush();
finish({jobs:[{id:'initial'}],pagination:{page:1,total:100,total_pages:10,before:10}});await initial;
const polling=pager.load();await flush();pager.load({page:2});
finish({jobs:[{id:'obsolete'}],pagination:{page:1,total:100,total_pages:10,before:10}});await flush();
assert.match(calls.at(-1),/page=2.*before=10/);
pager.load({reset:true});finish({jobs:[{id:'also-obsolete'}],pagination:{page:2,total:100,total_pages:10,before:10}});await flush();
assert.equal(calls.at(-1),'/jobs?page=1&page_size=10&group_batches=true');
finish({jobs:[{id:'new-job'}],pagination:{page:1,total:101,total_pages:11,before:20}});await polling;
assert.deepEqual(pages,['initial','new-job']);assert.equal(pager.total,101);
pager.destroy();await pager.load();assert.equal(calls.length,4);
''')


def test_top_and_bottom_controls_share_navigation_pending_state_and_page_clamping():
    run_js(DOM + r'''
const container=new Element('div'),jobs=new Element('section'),footer=new Element('footer');
container.append(jobs,footer);jobs.nextSibling=footer;
let finish;const calls=[],seen=[];
const pager=createVideoJobPager({container,beforeNode:jobs,
 request:path=>{calls.push(path);return new Promise(resolve=>finish=resolve)},
 onData:data=>seen.push(data.jobs),onError:error=>{throw error}});
assert.deepEqual(container.children,[pager.node,jobs,pager.bottomNode,footer]);
const top=pager.node.children,bottom=pager.bottomNode.children;
const respond=(page,total=21)=>finish({jobs:[{id:`page-${page}`}],pagination:{page,total,total_pages:Math.max(1,Math.ceil(total/10)),before:123}});
const initial=pager.load();await flush();respond(1);await initial;
assert.equal(top[1].textContent,bottom[1].textContent);
assert.equal(top[0].disabled,true);assert.equal(bottom[0].disabled,true);
const forward=bottom[2].onclick();
assert.ok([top[0],top[2],bottom[0],bottom[2]].every(button=>button.disabled));
assert.equal(pager.node.getAttribute('aria-busy'),'true');assert.equal(pager.bottomNode.getAttribute('aria-busy'),'true');
await flush();assert.match(calls.at(-1),/page=2&page_size=10.*before=123/);
respond(2);await forward;
assert.equal(top[1].textContent,'11–20 of 21 jobs · Page 2 of 3');
assert.equal(top[1].textContent,bottom[1].textContent);
assert.ok([top[0],top[2],bottom[0],bottom[2]].every(button=>!button.disabled));
const polling=pager.load();await flush();assert.match(calls.at(-1),/page=2.*before=123/);respond(2);await polling;
const previous=top[0].onclick();await flush();assert.match(calls.at(-1),/page=1.*before=123/);respond(1);await previous;
const last=pager.load({page:3});await flush();respond(3);await last;
assert.equal(top[2].disabled,true);assert.equal(bottom[2].disabled,true);
const afterDelete=pager.load();await flush();respond(1,4);await afterDelete;
assert.equal(top[1].textContent,'1–4 of 4 jobs · Page 1 of 1');assert.equal(top[1].textContent,bottom[1].textContent);
assert.ok([top[0],top[2],bottom[0],bottom[2]].every(button=>button.disabled));
assert.equal(calls.length,6);assert.equal(seen.length,6);
''')


def test_only_open_off_page_editor_fetches_detail_and_blocks_deleted_job():
    run_js(r'''
const seen=[],calls=[],editor={active:{id:'older'},observe:jobs=>seen.push(jobs)};
const request=async path=>{calls.push(path);return{id:'older',status:'running',revision:7}};
await observePagedVideoJobEditor(editor,[{id:'newer'}],request);
assert.deepEqual(calls,['/jobs/older']);assert.equal(seen.at(-1)[0].status,'running');
await observePagedVideoJobEditor(editor,[{id:'older',status:'queued'}],request);assert.equal(calls.length,1);
await observePagedVideoJobEditor(editor,[],async()=>{throw Object.assign(Error(),{status:404})});
assert.deepEqual(seen.at(-1),[{id:'older',status:'deleted'}]);
editor.active=null;await observePagedVideoJobEditor(editor,[],request);assert.equal(calls.length,1);
''')


def test_media_has_no_source_until_play_and_releases_decoder_on_page_leave():
    run_js(DOM + r'''
const player=createVideoJobMedia('/api/video/h3/jobs/one/video');
assert.equal(player.media.preload,'none');assert.equal(player.media.src,'');assert.equal(player.media.hidden,true);
await player.node.children[0].onclick();assert.equal(player.media.src,'/api/video/h3/jobs/one/video');assert.equal(player.media.paused,false);
releaseVideoJobMedia(player.node);assert.equal(player.media.src,'');assert.equal(player.media.paused,true);assert.equal(player.media.loads,1);
const audio=createVideoJobMedia('/audio',{type:'audio',label:'Play audio'});assert.equal(audio.media.src,'');
await audio.node.children[0].onclick();assert.equal(audio.media.tag,'audio');assert.equal(audio.media.paused,false);
''')
