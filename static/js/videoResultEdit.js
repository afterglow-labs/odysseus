export function createResultEditor({ jobId, request, container, onQueued }) {
  const make=(tag,text='')=>{const n=document.createElement(tag);n.textContent=text;return n;};
  const form=make('form');form.className='video-result-editor';form.hidden=true;
  const heading=make('h4','Ask for a change'),summary=make('p'),note=make('p');note.className='h3-video-muted';
  const label=make('label','What is the one change?'),change=make('textarea');change.rows=3;change.required=true;change.maxLength=12000;change.className='cookbook-field-input';label.append(change);
  const audioLabel=make('label',' Keep the original soundtrack'),audio=make('input');audio.type='checkbox';audio.checked=true;audioLabel.prepend(audio);
  const stepsLabel=make('label','Sampling steps'),steps=make('input');steps.type='number';steps.min=1;steps.max=100;steps.className='cookbook-field-input';stepsLabel.append(steps);
  const details=make('details'),prompt=make('textarea');prompt.rows=9;prompt.readOnly=true;prompt.setAttribute('aria-label','Prepared preservation prompt');details.append(make('summary','Read the prepared prompt'),prompt);
  const status=make('p');status.setAttribute('role','status');
  const submit=make('button','Queue this edit'),close=make('button','Close edit');submit.type='submit';close.type='button';
  for(const n of [submit,close])n.className='memory-toolbar-btn';
  form.append(heading,summary,note,label,audioLabel,stepsLabel,details,status,submit,close);container.append(form);
  let data,busy=false,pending=null;
  const key=`odysseus-result-edit:${jobId}`;
  try{pending=JSON.parse(sessionStorage.getItem(key)||'null');}catch{}
  function update(){
    if(data)prompt.value=data.prompt_template.replace('{change}',change.value.trim()||'[Your requested change]').replace('{audio}',audio.checked?'Keep the original soundtrack and its exact timing.':'Change audio only if explicitly requested; otherwise match the original soundtrack and timing.');
    submit.disabled=busy||!data||!change.value.trim();submit.textContent=busy?'Queuing…':pending?'Retry edit request':'Queue this edit';
    change.disabled=audio.disabled=steps.disabled=busy||!!pending;close.disabled=busy;
  }
  change.oninput=audio.onchange=steps.oninput=update;close.onclick=()=>{form.hidden=true;};
  form.onsubmit=async event=>{event.preventDefault();if(submit.disabled||!form.reportValidity())return;
    pending ||= {change:change.value.trim(),preserve_audio:audio.checked,steps:Number(steps.value),request_id:crypto.randomUUID()};
    sessionStorage.setItem(key,JSON.stringify(pending));busy=true;update();
    try{const result=await request(`/jobs/${jobId}/edit-result`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});
      if(!result.id)throw new Error('The server did not confirm this edit. Retry safely.');
      pending=null;sessionStorage.removeItem(key);status.textContent=`Edit queued · ${result.id}`;await onQueued();
    }catch(error){if(error.status===400){pending=null;sessionStorage.removeItem(key);}status.textContent=error.message;}
    finally{busy=false;update();}
  };
  return {async open(){form.hidden=false;form.scrollIntoView({block:'nearest'});if(data){update();return;}
    try{data=await request(`/jobs/${jobId}/edit-result`);summary.textContent=data.summary;note.textContent=data.note;
      change.value=pending?.change||'';audio.checked=pending?.preserve_audio??true;steps.value=pending?.steps??data.config.steps;update();change.focus({preventScroll:true});
    }catch(error){status.textContent=error.message;submit.disabled=true;}
  }};
}
