"""Provider reconnect actions retain the failed route and never guess an account."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run_node(script):
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "--input-type=module"],
        input=script, text=True, capture_output=True, cwd=ROOT, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_reconnect_button_preserves_failed_account_and_excludes_permission_errors():
    run_node(r"""
import assert from 'node:assert/strict';
import { createTerminalStreamError } from './static/js/chatStreamErrors.js';
import { appendProviderReconnectButton, resolveReconnectEndpoint } from './static/js/providerReconnect.js';
const url = 'https://chatgpt.com/backend-api/codex';
const selected = { endpoint_url: url, endpoint_id: 'account-a' };
const error = createTerminalStreamError({ authentication_required: true,
  provider: 'chatgpt-subscription', endpoint_url: url + '/responses', endpoint_id:'account-a', status: 401, text: 'Expired' }, selected);
selected.endpoint_id = 'account-b'; selected.endpoint_url = 'https://api.openai.com/v1';
assert.equal(error.providerReconnect.endpoint_id, 'account-a');
for (const payload of [
  {status: 401}, {status: 403}, {status: 429, authentication_required: true},
  {status: 400, code: 'unsupported_access_program', authentication_required: true},
  {status: 403, code: 'access_program_not_enabled', authentication_required: true},
]) assert.equal(createTerminalStreamError({...payload, endpoint_url: url}, selected).providerReconnect, null);
const fallback = createTerminalStreamError({authentication_required: true, endpoint_url: url}, selected);
assert.equal(fallback.providerReconnect.endpoint_id, '', 'Do not apply the requested account ID to a fallback provider');
const sameUrlFallback = createTerminalStreamError({authentication_required:true, endpoint_url:url}, {endpoint_id:'account-a',endpoint_url:url});
assert.equal(sameUrlFallback.providerReconnect.endpoint_id, '', 'The same URL can belong to a different fallback account');
const endpoints = ['account-a', 'account-b'].map(id => ({id, base_url:url}));
assert.equal(resolveReconnectEndpoint(error.providerReconnect, endpoints).id, 'account-a');
assert.throws(() => resolveReconnectEndpoint({endpoint_url:url}, endpoints), /More than one/);
assert.throws(() => resolveReconnectEndpoint(error.providerReconnect, endpoints.slice(1)), /changed or was removed/);
assert.throws(() => resolveReconnectEndpoint(error.providerReconnect, [{id:'account-a',base_url:'https://api.openai.com/v1'}]), /changed or was removed/);
class Element {
  constructor() { this.children=[]; this.style={}; this.events={}; }
  querySelector(name) {return this.children.find(child => '.' + child.className.split(' ').at(-1) === name) || null;}
  addEventListener(name, fn) {this.events[name]=fn;}
  appendChild(child) {this.children.push(child);}
}
globalThis.document = {createElement:() => new Element()};
const body = new Element(); let opened;
const button = appendProviderReconnectButton(body, error.providerReconnect, {open: async target => {opened=target;}});
assert.equal(button.type, 'button'); assert.equal(button.textContent, 'Reconnect provider');
assert.equal(opened, undefined, 'Rendering cannot initiate sign-in');
await button.events.click();
assert.equal(opened.endpoint_id, 'account-a');
assert.equal(button.disabled, false);
assert.equal(appendProviderReconnectButton(body, error.providerReconnect), null);
""")


def test_real_connection_form_targets_exact_endpoint_for_key_and_device_reconnect():
    run_node(r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
const elements = new Map(), calls = [], deviceCalls = [], opened = [];
class Element {
  constructor() {this.events={};this.style={};this.dataset={};this.options=[];this.value='';this.children=[];this.classList={add(){},remove(){},toggle(){},contains(){return false;}};}
  addEventListener(name, fn) {(this.events[name] ||= []).push(fn);}
  async dispatchEvent(event) {for (const fn of this.events[event.type] || []) await fn(event);}
  querySelector(){return null;} querySelectorAll(){return [];} closest(){return null;}
  appendChild(child){this.children.push(child);} setAttribute(){} focus(){this.focused=true;}
  get selectedOptions(){return this.options.filter(option => option.value === this.value);}
}
const document = {getElementById(id){if(!elements.has(id)) elements.set(id,new Element());return elements.get(id);},
  createElement(){return new Element();},addEventListener(){},querySelectorAll(){return [];},body:new Element()};
const provider = document.getElementById('adm-epProvider');
provider.options = [{value:'',dataset:{},textContent:'Provider'},
  {value:'chatgpt-subscription',dataset:{authFlow:'chatgpt-subscription'},textContent:'ChatGPT Subscription'},
  {value:'https://api.openai.com/v1',dataset:{},textContent:'OpenAI'}];
const endpoints = [
  {id:'key-a',name:'First API account',base_url:'https://api.openai.com/v1',is_enabled:true,models:[]},
  {id:'key-b',name:'Second API account',base_url:'https://api.openai.com/v1',is_enabled:true,models:[]},
  {id:'sub-a',name:'Subscription A',base_url:'https://chatgpt.com/backend-api/codex',is_enabled:true,models:[]},
];
let delayedEndpoints;
const fetch = async (url, options={}) => {
  calls.push({url,...options});
  if (url==='/api/model-endpoints' && delayedEndpoints) {const wait=delayedEndpoints;delayedEndpoints=null;return wait;}
  return {ok:true,json:async()=>url==='/api/model-endpoints'?endpoints:{}};
};
const window = {modelsModule:{refreshModels:async()=>{}},sessionModule:{},dispatchEvent(){}};
const context = vm.createContext({document,window,fetch,FormData,URL,AbortController,console:{error(){},warn(){}},
  Event:class {constructor(type){this.type=type;}},CustomEvent:class{},setTimeout(){},clearTimeout(){},setInterval(){},clearInterval(){},
  navigator:{},localStorage:{getItem(){return null;},setItem(){}}});
const root = path.resolve('static/js'), modules = new Map();
const stubs = {
  'ui.js': {default:{esc:value=>String(value ?? '')}},
  'settings.js': {default:{open:tab=>opened.push(tab),refreshAiModelEndpoints(){},initIntegrations(){}}},
  'providers.js': {providerLogo:()=>'',providerLogoFromUrl:()=>''},
  'modelSort.js': {sortModelObjects:values=>values},
  'appConfig.js': {getSettings:async()=>({}),getTools:async()=>({}),invalidateSettings(){},invalidateTools(){}},
  'providerDeviceFlow.js': {PROVIDER_DEVICE_FLOWS:{'chatgpt-subscription':{label:'ChatGPT Subscription'}},
    formatDeviceFlowError:error=>error.message,runProviderDeviceFlow:async(provider, options)=>{deviceCalls.push({provider,options});return {status:'failed',error:'fixture'};}},
};
async function load(file){
  if(modules.has(file)) return modules.get(file);
  const name=path.basename(file); let module;
  if(stubs[name]) {const values=stubs[name];module=new vm.SyntheticModule(Object.keys(values),function(){for(const [key,value] of Object.entries(values))this.setExport(key,value);},{context,identifier:file});}
  else module=new vm.SourceTextModule(fs.readFileSync(file,'utf8'),{context,identifier:file});
  modules.set(file,module);await module.link((specifier, parent)=>load(path.resolve(path.dirname(parent.identifier),specifier)));return module;
}
const admin=await load(path.join(root,'admin.js'));await admin.evaluate();
await admin.namespace.openProviderReconnect({provider:'openai',endpoint_id:'key-b',endpoint_url:'https://api.openai.com/v1/chat/completions'});
assert.equal(opened.at(-1),'services');
assert.equal(document.getElementById('adm-epAddBtn').textContent,'Save key');
assert.equal(calls.some(call=>call.method==='PATCH'||call.method==='POST'),false, 'Opening settings must not mutate credentials');
document.getElementById('adm-epApiKey').value='fixture-new-key';
await document.getElementById('adm-epAddBtn').dispatchEvent({type:'click'});
const saved=calls.filter(call=>call.method==='PATCH');
assert.equal(saved.length,1);assert.equal(saved[0].url,'/api/model-endpoints/key-b');
assert.deepEqual(JSON.parse(saved[0].body),{api_key:'fixture-new-key'});
assert.equal(document.getElementById('adm-epApiKey').value,'');
await admin.namespace.openProviderReconnect({provider:'chatgpt-subscription',endpoint_id:'sub-a',endpoint_url:'https://chatgpt.com/backend-api/codex/responses'});
assert.equal(provider.value,'chatgpt-subscription');
assert.equal(document.getElementById('adm-epAddBtn').textContent,'Reconnect');
assert.equal(deviceCalls.length,0);
await document.getElementById('adm-epAddBtn').dispatchEvent({type:'click'});
assert.equal(deviceCalls.length,1);assert.equal(deviceCalls[0].options.formData.get('endpoint_id'),'sub-a');
await assert.rejects(admin.namespace.openProviderReconnect({endpoint_url:'https://api.openai.com/v1'}),/More than one/);
assert.equal(opened.at(-1),'added-models');
let releaseOld;
delayedEndpoints=new Promise(resolve=>{releaseOld=resolve;});
const oldNavigation=admin.namespace.openProviderReconnect({endpoint_id:'key-a',endpoint_url:'https://api.openai.com/v1'});
await admin.namespace.openProviderReconnect({endpoint_id:'key-b',endpoint_url:'https://api.openai.com/v1'});
releaseOld({ok:true,json:async()=>endpoints});await oldNavigation;
assert.match(document.getElementById('adm-epApiMsg').textContent,/Second API account/,
  'An older metadata response must not replace the account opened by the latest click');
""")
