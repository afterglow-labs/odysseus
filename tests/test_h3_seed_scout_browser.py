"""Native Scout uses real form handlers; routes are isolated and never render."""
import asyncio
import base64
import json
import mimetypes
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import shutil
from urllib.parse import urlparse, parse_qs
import pytest

ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('layout',['classic','pipeline'])
def test_native_scout_choices_retained_draft_and_classic_hunter(tmp_path,layout):
    pw=pytest.importorskip('playwright.async_api')
    chrome=shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Chrome required')
    requests, errors, selections, submissions=[],[],[],[]
    components=[{'id':role,'role':role,'name':role+'.safetensors','variant':'ref2va'} for role in ['model','encoder','video_vae','audio_vae','latent_upscaler']]
    components.append({'id':'turbo','role':'lora','name':'minimax_h3_DMAD_4step_full_lora_avg_rank_39_bf16.safetensors','variant':'ref2va'})
    inventory={'runtime_ready':True,'components':components,'gpus':[{'id':'GPU-5090','name':'RTX 5090','nvfp4':True},{'id':'GPU-4090','name':'RTX 4090','nvfp4':False}], 'batch_jobs':True,'batch_auto_video_length':True,'server_video_files':True,'seed_hunter':{'features':{'seed_scout':True,'scout_upscale':True,'guides':True,'force_audio':True,'latent_upscaler':True,'attention_backend':True,'ff_chunking':True},'defaults':{'steps':6,'loras':[{'id':'turbo','strength':1}]}}}
    scout={'status':'waiting_for_selection','seeds':[100,101,102],'scout_step':3,'total_steps':20,'selected_seed':None,'previews':[{'seed':n,'provisional':True,'url':f'/api/video/h3/jobs/scout-job/scout/previews/{n}'} for n in [100,101,102]]}
    job={'id':'scout-job','recipe':'seed_hunter','seed_hunter':{'stage':'scout','scout_count':3},'scout':scout,'status':'running','prompt':'Fixture job','phase':'waiting_for_selection','seed':100}
    hunter={'id':'hunter-job','recipe':'seed_hunter','seed_hunter':{'stage':'preview','candidates':[]},'status':'completed'}
    image=base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    async def exercise():
      async with pw.async_playwright() as p:
        browser=await p.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox'])
        page=await browser.new_page(viewport={'width':1280,'height':1000})
        page.on('pageerror',lambda e:errors.append(str(e)))
        async def route(handler):
          r=handler.request;url=urlparse(r.url);path=url.path;requests.append((r.method,path))
          if path=='/':await handler.fulfill(content_type='text/html',body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
          elif path.startswith('/static/'):
            source=ROOT/path.lstrip('/');await handler.fulfill(body=source.read_bytes(),content_type='text/javascript' if source.suffix=='.js' else mimetypes.guess_type(source.name)[0])
          elif path.endswith('/inventory'):await handler.fulfill(json=inventory)
          elif path.endswith('/prompt-enhancer'):await handler.fulfill(json={'models':[]})
          elif path=='/api/video/h3/jobs' and r.method=='GET':
            chosen=[job] if parse_qs(url.query).get('stage')==['scout'] else [hunter]
            await handler.fulfill(json={'jobs':chosen,'queue':{'paused':False},'pagination':{'page':1,'total':1,'total_pages':1},'counts':{'running':1}})
          elif path=='/api/video/h3/jobs' and r.method=='POST':
            msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+r.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+r.post_data_buffer)
            parts=list(msg.iter_parts());cfg=json.loads(next(part.get_payload(decode=True) for part in parts if part.get_param('name',header='content-disposition')=='config'))
            submissions.append({'config':cfg,'files':[part.get_filename() for part in parts if part.get_filename()], 'auto':any(part.get_param('name',header='content-disposition')=='auto_video_length' and part.get_payload(decode=True)==b'true' for part in parts)});await handler.fulfill(json={'id':'new-job','status':'queued'})
          elif path.endswith('/scout/select'):
            assert path=='/api/video/h3/jobs/scout-job/scout/select';value=r.post_data_json;selections.append(value);scout['selected_seed']=value['seed'];scout['status']='continuing';await handler.fulfill(json=scout)
          elif '/scout/previews/' in path:await handler.fulfill(body=image,content_type='image/gif')
          elif path.endswith('/video'):await handler.fulfill(body=b'',content_type='video/mp4')
          else:await handler.fulfill(status=404,json={'detail':'Fixture not found'})
        await page.route('**/*',route)
        try:
          await page.goto('http://scout.test/')
          async def open_scout():
            await page.evaluate("async layout=>window.dialog=(await import('/static/js/h3SeedHunter.js')).showH3SeedScout({layout})",layout)
            await page.get_by_text('H3 Seed Scout is ready.',exact=True).wait_for()
          await open_scout();panel=page.get_by_role('dialog',name='H3 Seed Scout',exact=True)
          assert await page.locator('#seed-hunter-steps').input_value()=='20'
          assert await page.locator('#seed-hunter-option-scout_count').input_value()=='3'
          assert await page.locator('#seed-hunter-option-scout_step').input_value()=='3'
          await page.wait_for_function('()=>document.querySelector("[data-seed-hunter-help=seed]").dataset.helpHint.includes("consecutive seeds")')
          assert await page.locator('#seed-hunter-option-stage').input_value()=='scout'
          assert await panel.locator('.h3-lora-row').count()==0
          await panel.get_by_text('Sampling and optional controls',exact=True).click()
          await page.locator('#seed-hunter-option-scout_mode').select_option('final')
          assert await page.locator('#seed-hunter-option-scout_count').is_disabled()
          assert await page.locator('#seed-hunter-option-scout_timeout').is_disabled()
          await page.locator('#seed-hunter-option-scout_mode').select_option('interactive')
          assert await page.locator('#seed-hunter-option-scout_count').is_enabled()
          assert await page.locator('#seed-hunter-option-scout_timeout').is_enabled()
          assert await panel.get_by_role('combobox',name='Preview aspect ratio',exact=True).count()==1
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=setup]').click()
          assert await page.locator('#seed-hunter-latent_upscaler').is_visible()
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=generate]').click()
          assert await page.locator('#seed-hunter-option-scout_upscale').is_checked()
          await page.locator('#seed-hunter-option-scout_final_width').fill('1280')
          await page.locator('#seed-hunter-option-scout_final_height').fill('1728')
          await page.locator('#seed-hunter-option-scout_refine_steps').fill('3')
          await page.locator('#seed-hunter-option-scout_upscale').scroll_into_view_if_needed()
          await page.screenshot(path=str(tmp_path/'seed-scout-final-upscale-desktop.png'))
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=prepare]').click()
          await page.locator('#seed-hunter-prompt').fill('Keep everything except the jacket unchanged.')
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=generate]').click()
          await page.locator('#seed-hunter-width').fill('640')
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=inputs]').click()
          await page.locator('#seed-hunter-input-reference_images').set_input_files({'name':'reference.png','mimeType':'image/png','buffer':image})
          if layout=='pipeline':
            await panel.locator('.workflow-pipeline-media img').wait_for()
            await page.evaluate('window.sourceImage=document.querySelector(".workflow-pipeline-media img")')
            await panel.locator('[data-pipeline-stage=generate]').click()
            assert await page.evaluate('window.sourceImage===document.querySelector(".workflow-pipeline-media img")')
          await panel.get_by_role('button',name='Close',exact=True).click();await open_scout()
          assert await page.locator('#seed-hunter-width').input_value()=='640'
          assert await page.locator('#seed-hunter-prompt').input_value()=='Keep everything except the jacket unchanged.'
          assert await panel.get_by_text('reference.png',exact=False).count()>=1
          await panel.get_by_role('button',name='Start seed scout',exact=True).click()
          await page.wait_for_function('()=>[...document.querySelectorAll(".h3-seed-hunter-overlay button[type=submit]")].some(n=>n.textContent==="Start seed scout"&&!n.disabled)')
          assert len(submissions)==1
          cfg=submissions[0]['config'];assert cfg['seed_hunter']['stage']=='scout' and cfg['seed_hunter']['scout_count']==3 and cfg['seed_hunter']['scout_mode']=='interactive' and cfg['seed_hunter']['scout_timeout']==0
          assert cfg['latent_upscaler']=='latent_upscaler'
          assert cfg['seed_hunter']['scout_upscale'] is True and cfg['seed_hunter']['scout_refine_steps']==3
          assert cfg['seed_hunter']['scout_final_width']==1280 and cfg['seed_hunter']['scout_final_height']==1728
          assert cfg['steps']==20 and cfg['loras']==[]
          assert submissions[0]['files']==['reference.png']
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=inputs]').click()
          await panel.locator('[data-batch-toggle]').check()
          await panel.locator('input[data-batch-files]').set_input_files([{'name':name,'mimeType':'video/mp4','buffer':b'fixture'} for name in ['clip-one.mp4','clip-two.mp4']])
          await panel.get_by_role('button',name='Queue 2 videos',exact=True).click()
          await panel.get_by_text('2 / 2 queued',exact=False).wait_for()
          assert len(submissions)==3
          for submission,filename in zip(submissions[1:],['clip-one.mp4','clip-two.mp4']):
            assert submission['config']['seed_hunter']['stage']=='scout'
            assert submission['config']['seed_hunter']['scout_count']==3
            assert submission['files']==['reference.png',filename]
            assert submission['auto'] is True
          await panel.locator('[data-batch-toggle]').uncheck()
          if layout=='pipeline':await panel.locator('[data-pipeline-stage=review]').click()
          choices=panel.locator('.h3-scout-choice');assert await choices.count()==3
          await choices.first.scroll_into_view_if_needed();await choices.first.locator('img').click()
          await page.evaluate('window.previewElement=document.querySelector(".h3-scout-choice img")')
          await panel.get_by_role('button',name='Refresh jobs',exact=True).click();await page.wait_for_timeout(150)
          assert await page.evaluate('window.previewElement===document.querySelector(".h3-scout-choice img")')
          assert selections==[]
          assert await choices.first.locator('img').get_attribute('src')=='/api/video/h3/jobs/scout-job/scout/previews/100?preview=rough'
          scout['previews'][0]['provisional']=False
          await panel.get_by_role('button',name='Refresh jobs',exact=True).click();await page.wait_for_timeout(100)
          assert await choices.first.locator('img').get_attribute('src')=='/api/video/h3/jobs/scout-job/scout/previews/100?preview=decoded'
          assert await page.evaluate('window.previewElement===document.querySelector(".h3-scout-choice img")')
          await choices.nth(1).get_by_role('button',name='Continue this seed',exact=True).click()
          await choices.nth(1).get_by_role('button',name='Selected seed',exact=True).wait_for()
          assert selections==[{'seed':101}]
          await panel.get_by_role('button',name='Refresh jobs',exact=True).click();await page.wait_for_timeout(100)
          assert await page.evaluate('window.previewElement===document.querySelector(".h3-scout-choice img")')
          await choices.first.scroll_into_view_if_needed();await page.screenshot(path=str(tmp_path/'seed-scout-native-desktop.png'))
          job['status']='completed';await panel.get_by_role('button',name='Refresh jobs',exact=True).click()
          await panel.get_by_role('button',name='Play video',exact=True).wait_for();assert await panel.locator('.h3-video-output video[src]').count()==0
          await panel.get_by_role('button',name='Close',exact=True).click()
          await page.evaluate("async layout=>window.dialog=(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})",layout)
          await page.get_by_text('H3 Seed Hunter is ready.',exact=True).wait_for()
          hunter_panel=page.get_by_role('dialog',name='H3 Seed Hunter',exact=True)
          assert await page.locator('#seed-hunter-option-stage').input_value()=='preview'
          assert await page.locator('#seed-hunter-option-preview_count').is_visible()
          if layout=='pipeline':await hunter_panel.get_by_role('button',name='Show all settings',exact=True).click()
          assert await page.locator('#seed-hunter-latent_upscaler').is_visible()
          assert await page.locator('#seed-hunter-steps').input_value()=='20'
          assert await hunter_panel.locator('.h3-lora-row').count()==0
          assert await page.locator('#seed-hunter-prompt').input_value()==''
          await page.locator('#seed-hunter-steps').fill('18')
          await hunter_panel.get_by_role('button',name='Close',exact=True).click()
          await page.evaluate("async layout=>window.dialog=(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})",layout)
          await page.get_by_text('H3 Seed Hunter is ready.',exact=True).wait_for()
          assert await page.locator('#seed-hunter-steps').input_value()=='18'
          await hunter_panel.get_by_role('button',name='Close',exact=True).click()
          await page.set_viewport_size({'width':390,'height':844});await open_scout()
          await page.locator('#seed-hunter-option-scout_count').scroll_into_view_if_needed();await page.screenshot(path=str(tmp_path/'seed-scout-native-mobile.png'))
          assert await page.locator('.h3-seed-hunter-dialog').evaluate('(n)=>n.getBoundingClientRect().right<=window.innerWidth')
          assert not errors,errors
        finally:await browser.close()
    asyncio.run(exercise())
