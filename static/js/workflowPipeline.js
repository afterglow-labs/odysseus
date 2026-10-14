import { createVideoJobMedia, releaseVideoJobMedia } from './videoJobMedia.js';
import { workflowStudioInputPreview } from './workflowStudioInput.js';
import { bindMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
const make = (tag, cls = '', text) => { const n = document.createElement(tag); n.className = cls; if (text !== undefined) n.textContent = text; return n; };
const button = (label, action) => { const n = make('button', 'memory-toolbar-btn', label); n.type = 'button'; n.onclick = action; return n; };
let nextId = 0;
function styles() {
  if (document.getElementById('workflow-pipeline-styles')) return;
  const css = make('link'); css.id = 'workflow-pipeline-styles'; css.rel = 'stylesheet'; css.href = '/static/workflow-pipeline.css'; document.head.append(css);
}
export function showWorkflowPipeline({} = {}, anchor = document.activeElement) {
  styles(); document.querySelectorAll('.workflow-pipeline-chooser-overlay').forEach(dismissOrRemove);
  const overlay = make('div', 'cookbook-edit-overlay workflow-pipeline-chooser-overlay'); overlay.style.zIndex = String(topPortalZ());
  const dialog = make('section', 'cookbook-edit-modal workflow-pipeline-chooser'); dialog.setAttribute('role','dialog'); dialog.setAttribute('aria-modal','true'); dialog.setAttribute('aria-label','Build Pipeline');
  const header = make('header', 'h3-video-header'); const closeButton = button('Close', () => close()); header.append(make('h2','','Build Pipeline'),closeButton);
  const body = make('div','workflow-pipeline-recipes'); body.append(make('p','','Choose a workflow. Edit its real settings, attach media, and review results in the browser. Classic workflows remain available in Cookbook.'));
  const recipes = [
    ['MiniMax H3 Video','Generate from text, frames, or multiple references.','h3Video.js','showH3Video'],
    ['H3 Seed Hunter','Compare completed previews and refine a selected latent.','h3SeedHunter.js','showH3SeedHunter'],
    ['H3 Seed Scout','Compare partial previews, choose a seed, and continue it.','h3SeedHunter.js','showH3SeedScout'],
    ['BFS Video','Choose an identity, target video, and compatible workflow.','bfsVideo.js','showBfsVideo'],
  ];
  for (const [name,description,module,entry] of recipes) {
    const item=button('',async()=>{item.disabled=true;try{const fn=(await import('./'+module))[entry];close();fn({layout:'pipeline'},anchor);}catch(error){item.disabled=false;message.textContent=error.message;message.hidden=false;}});
    item.classList.add('workflow-pipeline-recipe');item.append(make('strong','',name),make('span','',description));body.append(item);
  }
  const message=make('p','h3-video-error');message.hidden=true;body.append(message);dialog.append(header,body);overlay.append(dialog);document.body.append(overlay);
  const close=bindMenuDismiss(overlay,()=>{overlay.remove();anchor?.isConnected&&anchor.focus();},()=>false);closeButton.focus();return {overlay,close};
}

// The original controls and forms remain the source of truth. This helper only
// places them and previews their real media; it owns no execution or graph state.
export function createWorkflowPipeline({container,dialog,form,sections,family='h3',title='Workflow',getInputs=()=>[],getSummary=()=>({}),setLoras,actions=[],queueNodes=[]}) {
  styles();const id=`workflow-pipeline-${++nextId}`;dialog.classList.add('workflow-pipeline-dialog');
  const root=make('section','workflow-pipeline');root.setAttribute('aria-label',`${title} pipeline`);container.append(root);
  const toolbar=make('div','workflow-pipeline-toolbar'),showAll=button('Show all settings',()=>{all=!all;applySelection();});toolbar.append(make('strong','',title),showAll);root.append(toolbar);
  const workspace=make('div','workflow-pipeline-workspace'),nav=make('nav','workflow-pipeline-stages'),center=make('section','workflow-pipeline-center'),inspector=make('section','workflow-pipeline-settings');nav.setAttribute('aria-label','Pipeline stages');inspector.setAttribute('aria-label','Pipeline settings');workspace.append(nav,center,inspector);root.append(workspace);
  const route=make('section','workflow-pipeline-model'),model=button('Model setup',()=>select('setup')),loras=make('div','workflow-pipeline-loras');route.append(make('strong','','Generation model'),model,loras);center.append(route);
  const previewToolbar=make('div','workflow-pipeline-preview-toolbar'),sourceTab=button('Source',()=>setMedia('source')),resultTab=button('Results',()=>setMedia('result'));previewToolbar.append(sourceTab,resultTab);center.append(previewToolbar);
  const source=make('section','workflow-pipeline-source'),inputSelect=make('select','cookbook-field-input'),media=make('div','workflow-pipeline-media');inputSelect.setAttribute('aria-label','Preview input');source.append(inputSelect,media);center.append(source);
  const result=make('section','workflow-pipeline-results');result.setAttribute('aria-label','Workflow results');center.append(result);
  const settingsTitle=make('h3');inspector.append(settingsTitle);form.id ||= `${id}-form`;inspector.append(form);
  const panels=[],stages=new Map(),lastStrengths=new Map();let all=false,selected='generate',mediaMode='source',closed=false,queued=false,sourceItems=[],identity=undefined,objectUrl=null;
  for(const section of sections){
    const panel=make('section','workflow-pipeline-section');panel.dataset.pipelineSection=section.id;panel.setAttribute('aria-label',section.title);panel.append(make('h3','workflow-pipeline-section-title',section.title));
    for(const n of section.nodes||[])if(n)panel.append(n);
    if(section.id==='review')result.append(panel);else if(section.parent===form)form.append(panel);else inspector.append(panel);
    panels.push({...section,node:panel});const item=button(section.title,()=>select(section.id));item.dataset.pipelineStage=section.id;nav.append(item);stages.set(section.id,item);
  }
  const queue=make('section','workflow-pipeline-queue');queue.setAttribute('aria-label','Queue controls');for(const n of queueNodes)if(n)queue.append(n);if(queue.childElementCount)root.append(queue);
  const footer=make('div','workflow-pipeline-footer');for(const action of actions){if(!action)continue;for(const submit of action.querySelectorAll('[type=submit]'))submit.setAttribute('form',form.id);footer.append(action);}if(footer.childElementCount)root.append(footer);
  function applySelection(){
    root.dataset.allSettings=String(all);showAll.textContent=all?'Focus one stage':'Show all settings';showAll.setAttribute('aria-pressed',String(all));
    for(const panel of panels)panel.node.hidden=panel.id==='review'?false:!all&&panel.id!==selected;
    form.hidden=!all&&!panels.some(p=>p.id===selected&&form.contains(p.node));settingsTitle.textContent=all?'All workflow settings':panels.find(p=>p.id===selected)?.title||'Workflow settings';
    for(const [key,n]of stages)n.setAttribute('aria-current',key===selected&&!all?'step':'false');
    inspector.hidden=!all&&selected==='review';root.dataset.stage=selected;
  }
  function select(key){if(!stages.has(key)||closed)return;selected=key;all=false;applySelection();if(key==='review'||key==='refine')setMedia('result');inspector.scrollTop=0;if(matchMedia('(max-width:900px)').matches)(key==='review'?result:inspector).scrollIntoView({block:'start',behavior:'smooth'});}
  function setMedia(mode){mediaMode=mode;source.hidden=mode!=='source';result.hidden=mode!=='result';sourceTab.setAttribute('aria-pressed',String(mode==='source'));resultTab.setAttribute('aria-pressed',String(mode==='result'));}
  function sourcePreview(){
    const candidate=sourceItems[Number(inputSelect.value)||0],item=workflowStudioInputPreview(candidate?[candidate]:[],family),next=item?.file||item?.url||null;
    if(identity===next)return;identity=next;releaseVideoJobMedia(media);if(objectUrl)URL.revokeObjectURL(objectUrl);objectUrl=null;media.replaceChildren();
    if(!item){media.append(make('p','h3-video-muted','Attach media in Inputs to preview it here.'),button('Choose inputs',()=>select('inputs')));return;}
    const url=item.file instanceof Blob?(objectUrl=URL.createObjectURL(item.file)):item.url;
    if(item.type==='image'){const img=make('img');img.alt=item.label||'Input image';img.src=url;media.append(img);}else media.append(createVideoJobMedia(url,{type:item.type==='audio'?'audio':'video',label:'Play source'}).node);
  }
  inputSelect.onchange=sourcePreview;
  function refresh(){
    if(closed)return;const state=getSummary()||{};model.textContent=state.model||'Choose model';model.title=model.textContent;
    const stack=state.loras||[],existing=[...loras.children];stack.forEach((row,index)=>{
      let label=existing[index];if(!label){label=make('label');const enabled=make('input');enabled.type='checkbox';const name=make('span');label.append(enabled,name);loras.append(label);}
      const enabled=label.firstChild;enabled.checked=Number(row.strength)!==0;enabled.disabled=!setLoras;enabled.setAttribute('aria-label',`Enable ${row.name||row.id}`);label.lastChild.textContent=`${row.name||row.id} · ${row.strength}`;
      if(Number(row.strength)!==0)lastStrengths.set(row.id,Number(row.strength));enabled.oninput=()=>{const latest=getSummary().loras||[];setLoras?.(latest.map(item=>({id:item.id,strength:item.id===row.id?(enabled.checked?lastStrengths.get(item.id)??1:0):item.strength})));refresh();};
    });existing.slice(stack.length).forEach(n=>n.remove());loras.hidden=!stack.length;
    const before=sourceItems[Number(inputSelect.value)||0];sourceItems=getInputs().filter(Boolean);const same=sourceItems.length===inputSelect.options.length&&sourceItems.every((item,i)=>inputSelect.options[i]?.text===item.name);
    if(!same){inputSelect.replaceChildren(...sourceItems.map((item,i)=>new Option(item.name||`Input ${i+1}`,String(i))));const found=sourceItems.indexOf(before);inputSelect.value=String(Math.max(0,found));}inputSelect.hidden=!sourceItems.length;sourcePreview();
  }
  const schedule=()=>{if(queued||closed)return;queued=true;queueMicrotask(()=>{queued=false;refresh();});};
  const invalid=event=>{const first=[...(event.target.form?.elements||[])].find(n=>n.willValidate&&!n.validity.valid)||event.target;const panel=panels.find(p=>p.node.contains(first));if(panel)select(panel.id);for(let n=first.parentElement;n&&n!==root;n=n.parentElement)if(n.tagName==='DETAILS')n.open=true;};
  root.addEventListener('input',schedule);root.addEventListener('change',schedule);root.addEventListener('invalid',invalid,true);
  const observer=new MutationObserver(schedule);observer.observe(form,{childList:true,subtree:true});
  setMedia(mediaMode);applySelection();refresh();
  return {root,select,refresh,destroy(){closed=true;observer.disconnect();root.removeEventListener('input',schedule);root.removeEventListener('change',schedule);root.removeEventListener('invalid',invalid,true);releaseVideoJobMedia(media);if(objectUrl)URL.revokeObjectURL(objectUrl);}};
}
