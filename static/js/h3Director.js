import { createVideoJobMedia } from './videoJobMedia.js';

const make = (tag, text = '') => { const n = document.createElement(tag); n.textContent = text; return n; };
const copy = value => JSON.parse(JSON.stringify(value));
const defaults = {stage:'generation',duration_seconds:10,chunk_duration_seconds:10,resize_method:'crop',hybrid_continuation:false,
  candidate_2:false,candidate_3:false,candidate_4:false,segments:[{id:'cut-1',prompt:'',weight:1,speakerCharIdxs:[]}],
  characters:[],background:null,refVideos:[],refAudios:[],dialogue_language:'English',overall_soundscape:'',non_diegetic_music:'',denoise:.2};
export async function showH3Director(options = {}, anchor = document.activeElement) {
  const {showH3Video} = await import('./h3Video.js');
  return showH3Video({...options, recipe:'director'}, anchor);
}

export function removingDirectorReference(options, field, index) {
  const key={reference_images:'characters',reference_videos:'refVideos',reference_audio:'refAudios'}[field];
  if(!key)return options;
  const result=copy(options), indexKey=field==='reference_images'?'image_index':'input_index';
  result[key]=(result[key]||[]).filter(row=>row[indexKey]!==index).map(row=>({...row,[indexKey]:row[indexKey]>index?row[indexKey]-1:row[indexKey]}));
  if(field==='reference_images'){
    if(result.background?.image_index===index)result.background=null;
    else if(result.background?.image_index>index)result.background.image_index--;
    result.segments=(result.segments||[]).map(cut=>({...cut,speakerCharIdxs:(cut.speakerCharIdxs||[]).filter(i=>i!==index).map(i=>i>index?i-1:i)}));
  }
  return result;
}

export function createDirectorControls({form, before = null, onChange, getMode, getInputs, showModels = true, showReferences = true}) {
  const node=make('section');node.className='h3-director-controls';node.setAttribute('aria-label','Director timeline');
  form.insertBefore(node,before);
  let options=copy(defaults), secondary='', componentOptions=[], refKey='';
  const field=(parent,label,value,change,{type='text',choices=null,min,max,step}={})=>{
    const wrap=make('label');wrap.className='h3-video-field';wrap.append(make('span',label));
    const input=make(choices?'select':type==='textarea'?'textarea':'input');input.className='cookbook-field-input';
    if(choices)for(const [v,t] of choices)input.add(new Option(t,v));
    else if(type!=='textarea')input.type=type;
    if(type==='checkbox')input.checked=!!value;else input.value=value??'';
    if(min!==undefined)input.min=min;if(max!==undefined)input.max=max;if(step!==undefined)input.step=step;
    if(type==='textarea'){input.rows=3;input.maxLength=16000;}
    input.setAttribute('aria-label',label);input.oninput=()=>{change(type==='checkbox'?input.checked:type==='number'?(input.value===''?null:Number(input.value)):input.value);onChange();};
    wrap.append(input);parent.append(wrap);return input;
  };
  function render(){
    node.replaceChildren(make('h3','Director timeline'));
    const grid=make('div');grid.className='h3-video-grid';node.append(grid);
    for(const [key,label,min,max] of [['duration_seconds','Total seconds',1,120],['chunk_duration_seconds','Chunk seconds',3,15]])
      field(grid,label,options[key],v=>options[key]=v,{type:'number',min,max,step:.1});
    const help=make('p','Cut weights divide the total duration. H3 generates chunks in sequence and carries continuity between them.');help.className='h3-video-muted';node.append(help);
    options.segments.forEach((cut,index)=>{
      const section=make('fieldset');section.append(make('legend',`Cut ${index+1}`));node.append(section);
      field(section,`Cut ${index+1} prompt`,cut.prompt,v=>cut.prompt=v,{type:'textarea'});
      field(section,`Cut ${index+1} duration weight`,cut.weight,v=>cut.weight=v,{type:'number',min:.01,max:1000,step:.01});
      field(section,`Cut ${index+1} speaking references`,(cut.speakerCharIdxs||[]).map(i=>i+1).join(', '),v=>{cut.speakerCharIdxs=v.split(',').map(s=>s.trim()).filter(Boolean).map(s=>Number(s)-1);});
      const move=make('button','Move up');move.type='button';move.className='memory-toolbar-btn';move.disabled=index===0;
      move.onclick=()=>{[options.segments[index-1],options.segments[index]]=[cut,options.segments[index-1]];render();onChange();};
      const remove=make('button','Remove cut');remove.type='button';remove.className='memory-toolbar-btn';remove.disabled=options.segments.length===1;
      remove.onclick=()=>{options.segments.splice(index,1);render();onChange();};section.append(move,remove);
    });
    const add=make('button','Add cut');add.type='button';add.className='memory-toolbar-btn';add.disabled=options.segments.length>=32;
    add.onclick=()=>{options.segments.push({id:crypto.randomUUID(),prompt:'',weight:1,speakerCharIdxs:[]});render();onChange();};node.append(add);
    for(const [key,label] of [['dialogue_language','Dialogue language'],['overall_soundscape','Overall soundscape'],['non_diegetic_music','Non-diegetic music']])
      field(node,label,options[key],v=>options[key]=v,{type:key==='dialogue_language'?'text':'textarea'});
    const seeds=make('fieldset');seeds.append(make('legend','Seed hunt'));node.append(seeds);
    seeds.append(make('p','Candidate 1 always runs. Each selected extra candidate repeats the full timeline with a different seed.'));
    for(const i of [2,3,4])field(seeds,`Candidate ${i}`,options[`candidate_${i}`],v=>options[`candidate_${i}`]=v,{type:'checkbox'});
    field(node,'Reference fit',options.resize_method,v=>options.resize_method=v,{choices:['crop','pad','stretch'].map(v=>[v,v])});
    if (getMode() === 'ref2va') {
      field(node,'Hybrid continuation',options.hybrid_continuation,v=>options.hybrid_continuation=v,{type:'checkbox'});
      if (showModels) field(node,'Continuation checkpoint',secondary,v=>secondary=v,{choices:[['','Choose First/Last Frame model'],...componentOptions]});
    }
    if (!showReferences || getMode() !== 'ref2va') return;
    const refs=make('details');refs.append(make('summary','Reference direction'));node.append(refs);
    const inputs=getInputs();
    field(refs,'Location reference image',options.background?.image_index??'',v=>{
      const existing=options.characters.find(row=>row.image_index===Number(v));
      options.background=v===''?null:{...existing,image_index:Number(v)};
      render();
    },{choices:[['','No location reference'],...Array.from({length:inputs.reference_images||0},(_,i)=>[String(i),`Image ${i+1}`])]});
    for(const [key,fieldName,label] of [['characters','reference_images','Image'],['refVideos','reference_videos','Video'],['refAudios','reference_audio','Audio']]){
      for(let i=0;i<(inputs[fieldName]||0);i++){
        const indexKey=key==='characters'?'image_index':'input_index';
        const location=key==='characters'&&options.background?.image_index===i;
        let row=location?options.background:options[key].find(r=>r[indexKey]===i);
        const set=(name,value)=>{if(!row){row={[indexKey]:i};options[key].push(row);}row[name]=value;};
        const section=make('fieldset');section.append(make('legend',`${label} ${i+1}`));refs.append(section);
        field(section,`${label} ${i+1} name`,row?.name||'',v=>set('name',v));
        field(section,`${label} ${i+1} description`,row?.description||'',v=>set('description',v),{type:'textarea'});
        const retention=key==='refAudios'?['reference','fully_copy','partially_copy','weak_reference']:['fully_preserved','partially_preserved','attribute_transfer','weak_reference'];
        const defaultRetention=key==='refVideos'?(row?.description?'fully_preserved':'weak_reference'):retention[0];
        field(section,`${label} ${i+1} retention`,row?.retention||defaultRetention,v=>set('retention',v),{choices:retention.map(v=>[v,key==='refVideos'&&v==='partially_preserved'?'Partially preserved · keep scene, replace character':v.replaceAll('_',' ')])});
        if(key!=='characters'){
          field(section,`${label} ${i+1} start seconds`,row?.trimStartSec??0,v=>set('trimStartSec',v??0),{type:'number',min:0,step:.1});
          field(section,`${label} ${i+1} end seconds`,row?.trimEndSec??'',v=>set('trimEndSec',v),{type:'number',min:0,step:.1});
        }
        if(key==='refVideos'){
          field(section,`Video ${i+1} include audio`,row?.includeAudio||false,v=>set('includeAudio',v),{type:'checkbox'});
          field(section,`Video ${i+1} role`,row?.role||'reference',v=>set('role',v),{choices:['reference','editing_source','continuation_source'].map(v=>[v,v.replaceAll('_',' ')])});
        }
      }
    }
  }
  return {node,get:()=>({recipe:'director',director:copy(options),model_fl2va:secondary}),
    hasPrompt:()=>options.segments.some(s=>s.prompt.trim()),
    set:(config,components=[])=>{options={...copy(defaults),...copy(config.director||{})};secondary=config.model_fl2va||'';
      componentOptions=components.filter(c=>c.role==='model'&&c.variant==='fl2va').map(c=>[c.id,c.name]);render();},
    refreshReferences:()=>{const key=JSON.stringify([getMode(),getInputs()]);if(key!==refKey){refKey=key;render();}},
    removeReference:(field,index)=>{options=removingDirectorReference(options,field,index);render();onChange();},
  };
}

export function directorQueueFields(config, components) {
  if(config.director?.stage==='refine')return [{
    key:'director',label:'Director denoise',getSavedValue:saved=>saved.director,
    create:onChange=>{
      const node=make('input');node.type='number';node.min=.05;node.max=1;node.step=.01;node.className='cookbook-field-input';node.setAttribute('aria-label','Director denoise');node.value=config.director.denoise;
      node.oninput=onChange;
      return {node,getValue:()=>({denoise:Number(node.value)}),setValue:value=>{node.value=value.denoise;},setDisabled:value=>{node.disabled=value;}};
    },
  }];
  const secondary=make('select');secondary.add(new Option('None',''));
  for(const item of components)if(item.role==='model'&&item.variant==='fl2va')secondary.add(new Option(item.name,item.id));
  secondary.value=config.model_fl2va||'';
  return [{
    key:'director', label:'Director timeline and seed hunt', getSavedValue: saved=>saved.director,
    create:onChange=>{
      const node=make('fieldset');
      const controls=createDirectorControls({form:node,onChange,getMode:()=>config.mode,getInputs:()=>({}),showModels:false,showReferences:false});
      controls.set(config,components);
      return {node,getValue:()=>controls.get().director,setValue:value=>controls.set({...config,director:value},components),setDisabled:value=>{node.disabled=value;}};
    },
  },{key:'model_fl2va',label:'Continuation checkpoint',control:secondary}];
}

export function renderDirectorCandidates(container,job,request,reload){
  if(!job.director)return;
  const fingerprint=JSON.stringify(job.director.candidates);
  if(container.dataset.candidates===fingerprint)return;
  container.dataset.candidates=fingerprint;
  for(const candidate of job.director.candidates||[]){
    if(container.querySelector(`[data-director-candidate="${candidate.index}"]`))continue;
    const card=make('section');card.dataset.directorCandidate=candidate.index;
    card.append(make('h4',`Candidate ${candidate.index} · Seed ${candidate.seed}`));
    const url=`/api/video/h3/jobs/${encodeURIComponent(job.id)}/director/candidates/${candidate.index}/video`;
    card.append(createVideoJobMedia(url).node);
    const download=make('a','Download candidate');download.href=url;download.download=`director-candidate-${candidate.index}.mp4`;download.className='memory-toolbar-btn';card.append(download);
    if(candidate.can_refine){
      const panel=make('details');panel.className='h3-seed-hunter-details';panel.append(make('summary','Refine candidate'));card.append(panel);
      const grid=make('div');grid.className='h3-video-grid';panel.append(grid);
      const storageKey=`odysseus.director.refine.${job.id}.${candidate.index}`;
      let body=null;
      try { const saved=JSON.parse(sessionStorage.getItem(storageKey));if(saved?.candidate===candidate.index&&typeof saved.request_id==='string'&&saved.config)body=saved; } catch {}
      const values={steps:20,denoise:.2,...body?.config}, controls=[];
      for(const [key,label] of [['steps','Refinement steps'],['denoise','Denoise']]){
        const wrap=make('label',label),input=make('input');wrap.className='h3-video-field';input.type='number';input.className='cookbook-field-input';input.value=values[key];input.step=key==='denoise'?.01:1;input.min=key==='denoise'?.05:1;input.max=key==='denoise'?1:100;input.required=true;input.disabled=!!body;
        input.oninput=()=>values[key]=Number(input.value);wrap.append(input);grid.append(wrap);controls.push(input);
      }
      panel.append(make('p','Uses approximately 1.96 MP at the original aspect ratio, RES multistep/simple, and the original audio.'));
      const button=make('button',body?'Retry same submission':'Queue refinement');button.type='button';button.className='memory-toolbar-btn';const status=make('p');status.setAttribute('role','status');panel.append(button,status);
      const persist=()=>{try { if(body)sessionStorage.setItem(storageKey,JSON.stringify(body));else sessionStorage.removeItem(storageKey); } catch {}};
      button.onclick=async()=>{
        if(!body&&controls.some(control=>!control.reportValidity()))return;
        body||={request_id:crypto.randomUUID(),candidate:candidate.index,config:{...values}};persist();controls.forEach(c=>c.disabled=true);button.disabled=true;
        try{
          const result=await request(`/jobs/${encodeURIComponent(job.id)}/director/refine`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
          if(!/^[a-f0-9]{32}$/i.test(result?.id||''))throw new Error('The server did not confirm the refinement. Retry the same submission.');
          body=null;persist();status.textContent='Refinement queued.';button.textContent='Refinement queued';
          try { await reload(); } catch { status.textContent='Refinement queued. Refresh history to see the job.'; }
        } catch(error){
          if(error.status===400){body=null;persist();controls.forEach(c=>c.disabled=false);}
          status.textContent=error.message;button.textContent=body?'Retry same submission':'Queue refinement';button.disabled=false;
        }
      };
    }else card.append(make('p','Multi-chunk candidate: play or save the full clip. Muse Refine accepts a single chunk.'));
    container.append(card);
  }
}
