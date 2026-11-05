import { showH3Video } from './h3Video.js';

export const showH3MD = (options={}, anchor) => showH3Video({...options,recipe:'h3md'},anchor);
const copy=value=>JSON.parse(JSON.stringify(value));
const make=(tag,text='')=>{const node=document.createElement(tag);node.textContent=text;return node;};
export function createH3MDControls({form,before,onChange=()=>{},getLoras=()=>[],onPreset=()=>{},showResume=false,showUpscaler=true}) {
  let options={},components=[],upscaler='',previousRows=[];
  const node=make('section');node.className='h3-director-controls h3md-controls';node.setAttribute('aria-label','H3MD Director');
  const presets=make('div');presets.className='h3-video-toolbar';
  const attribution=make('a','Based on H3MD Director by YJ');attribution.href='https://github.com/1113800942/comfy-h3md-director';attribution.target='_blank';attribution.rel='noopener noreferrer';
  const settings=make('div');settings.className='h3-video-grid';
  const shots=make('div'),adapters=make('div');
  node.append(presets,attribution,make('p','Low-resolution first pass → learned latent upscale → final pass. Long shots are split into linked segments.'),settings,make('h3','Shots'),shots,make('h3','LoRAs after upscaling'),adapters);
  form.insertBefore(node,before);
  function field(parent,title,key,type='number',choices=null) {
    const label=make('label');label.className='h3-video-field';label.append(make('span',title));
    const input=make(choices?'select':'input');input.className='cookbook-field-input';input.setAttribute('aria-label',title);
    if(choices)for(const [value,text] of choices)input.add(new Option(text,value));else input.type=type;
    if(type==='number')input.step='any';
    if(type==='checkbox')input.checked=options[key]===true;else input.value=key==='latent_upscaler'?upscaler:options[key]??'';
    input.onchange=()=>{if(key==='latent_upscaler')upscaler=input.value;else options[key]=type==='checkbox'?input.checked:type==='number'?Number(input.value):input.value;onChange();};
    label.append(input);parent.append(label);return input;
  }
  function render() {
    settings.replaceChildren();shots.replaceChildren();adapters.replaceChildren();
    field(settings,'Steps before upscaling','transition_step');field(settings,'First-pass size multiplier','lowres_scale');
    if(showUpscaler)field(settings,'Latent upscaler','latent_upscaler','text',components.filter(c=>c.role==='latent_upscaler').map(c=>[c.id,c.name]));
    field(settings,'Tile final pass when needed','highres_tiling','checkbox');
    field(settings,'Color consistency (0 off, 1 full)','lowres_anchor');
    field(settings,'Stop after segment (0 = all)','stop_after');
    if(showResume){field(settings,'Resume saved segments','resume_from_cache','checkbox');
    field(settings,'Segments to redo, e.g. 3, 5-7','rerun_segments','text');
    field(settings,'Use new seeds for redone segments','rerun_new_seed','checkbox');}
    (options.shots||[]).forEach((shot,index)=>{
      const section=make('fieldset');section.append(make('legend',`Shot ${index+1}`));
      const prompt=make('textarea');prompt.className='cookbook-field-input';prompt.value=shot.prompt||'';prompt.rows=3;prompt.setAttribute('aria-label',`Shot ${index+1} prompt`);
      prompt.oninput=()=>{shot.prompt=prompt.value;onChange();};section.append(prompt);
      const seconds=make('input');seconds.type='number';seconds.min=.21;seconds.max=600;seconds.step='any';seconds.value=(shot.frames/24).toFixed(2);seconds.className='cookbook-field-input';seconds.setAttribute('aria-label',`Shot ${index+1} seconds`);
      seconds.onchange=()=>{shot.frames=Math.max(5,5+17*Math.round((Number(seconds.value)*24-5)/17));seconds.value=(shot.frames/24).toFixed(2);onChange();};
      const length=make('label', 'Length in seconds');length.className='h3-video-field';length.append(seconds);section.append(length);
      if(index){const transition=make('select');transition.className='cookbook-field-input';transition.setAttribute('aria-label',`Shot ${index+1} transition`);for(const [v,t]of[[0,'New shot / hard cut'],[22,'Continue · 22-frame overlap'],[39,'Continue · 39-frame overlap']])transition.add(new Option(t,v));transition.value=shot.overlap;transition.onchange=()=>{shot.overlap=Number(transition.value);onChange();};section.append(transition);}
      const remove=make('button','Remove shot');remove.type='button';remove.className='memory-toolbar-btn';remove.disabled=options.shots.length===1;remove.onclick=()=>{options.shots.splice(index,1);options.shots[0].overlap=0;render();onChange();};section.append(remove);shots.append(section);
    });
    const add=make('button','Add shot');add.type='button';add.className='memory-toolbar-btn';add.disabled=(options.shots||[]).length>=64;add.onclick=()=>{options.shots.push({id:crypto.randomUUID(),prompt:'',frames:158,overlap:39,images:[],videos:[],audios:[]});render();onChange();};shots.append(add);
    const rows=getLoras();
    rows.forEach((row,index)=>{
      const title=components.find(c=>c.id===row.id)?.name||row.id;
      const label=make('label',title);label.className='h3-video-field';
      const input=make('input');input.type='number';input.step=.01;input.className='cookbook-field-input';input.setAttribute('aria-label',`Final-pass LoRA ${index+1} strength`);input.value=options.high_res_strengths?.[index]??row.strength;
      input.onchange=()=>{options.high_res_strengths[index]=Number(input.value);onChange();};label.append(input);adapters.append(label);
    });
  }
  return {node,
    get:()=>({recipe:'h3md',h3md:copy(options),latent_upscaler:upscaler}),
    set:(config,available=components)=>{options=copy(config.h3md||{});upscaler=config.latent_upscaler||'';components=available;previousRows=copy(config.loras||[]);render();},
    syncLoras:()=>{const next=getLoras(),old=options.high_res_strengths||[];options.high_res_strengths=next.map(row=>{const index=previousRows.findIndex(r=>r.id===row.id);return index<0?row.strength:old[index]??row.strength;});previousRows=copy(next);render();},
    presets:capability=>{presets.replaceChildren();for(const preset of capability?.presets||[]){const item=make('button',preset.name);item.type='button';item.className='memory-toolbar-btn';item.disabled=!preset.ready;item.onclick=()=>onPreset(preset);presets.append(item);if(!preset.ready)presets.append(make('p',`Needs: ${preset.missing.join(', ')}`));}},
    hasPrompt:()=>(options.shots||[]).some(s=>s.prompt?.trim()),
    refreshReferences:()=>{},
    removeReference:(field,index)=>{const kind={reference_images:'images',reference_videos:'videos',reference_audio:'audios'}[field];if(!kind)return;for(const shot of options.shots||[])shot[kind]=(shot[kind]||[]).filter(r=>(kind==='images'?r:r.index)!==index).map(r=>kind==='images'?(r>index?r-1:r):({...r,index:r.index>index?r.index-1:r.index}));render();},
  };
}

export function h3mdQueueFields(config,components) {
  return [{key:'latent_upscaler',label:'Latent upscaler',options:components.filter(c=>c.role==='latent_upscaler').map(c=>[c.id,c.name])},{key:'h3md',label:'H3MD shots and two-pass settings',getSavedValue:saved=>saved.h3md,
    create:onChange=>{const node=make('fieldset');const controls=createH3MDControls({form:node,before:null,onChange,showResume:true,showUpscaler:false,getLoras:()=>config.loras||[]});controls.set(config,components);return{node,getValue:()=>controls.get().h3md,setValue:value=>controls.set({...config,h3md:value},components),setDisabled:value=>{node.disabled=value;}};}}];
}
