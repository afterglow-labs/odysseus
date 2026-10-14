"""Existing form submission and inline playback through the optional pipeline layout."""
import asyncio
import json
import mimetypes
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import shutil
import subprocess
from urllib.parse import urlparse
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_pipeline_chooser_h3_bfs_real_settings_and_stable_inline_video(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    ffmpeg = pytest.importorskip('imageio_ffmpeg').get_ffmpeg_exe()
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Chrome required')
    clip = tmp_path/'clip.mp4'
    subprocess.run([ffmpeg,'-v','error','-f','lavfi','-i','color=c=purple:s=320x180:d=2','-c:v','libx264','-pix_fmt','yuv420p','-movflags','+faststart',str(clip)],check=True)
    video = clip.read_bytes()
    errors, submissions = [], []
    components = [{'id':r,'role':r,'name':r+'.safetensors','variant':'ref2va'} for r in ['model','encoder','video_vae','audio_vae']]
    components += [{'id':'adapter','role':'lora','name':'Identity adapter','variant':'ref2va'}]
    gpus = [{'id':'GPU-5090','name':'RTX 5090','nvfp4':True}]
    defaults = dict(mode='ref2va',model='model',encoder='encoder',video_vae='video_vae',audio_vae='audio_vae',gpu='GPU-5090',vae_gpu='',width=640,height=384,frames=124,steps=20,seed=42,sampler='euler',scheduler='simple',shift_video=12,shift_audio=3,reference_size='match',loras=[])
    h3 = dict(runtime_ready=True,components=components,gpus=gpus,defaults=defaults,lora_stack=True,max_loras=8,batch_jobs=True,batch_auto_video_length=True,server_video_files=True)
    bfs = dict(runtime_ready=True,components=components,gpus=gpus,defaults={'gpu':'GPU-5090'},batch_jobs=True,server_video_files=True,workflows=[{'id':'h3','label':'MiniMax H3 head swap','family':'h3','ready':True,'slots':[{'key':'model','label':'H3 model','required':True,'component_ids':['model'],'default_id':'model'}], 'controls':[{'key':key,'label':key.title(),'type':'integer','min':1,'max':2048,'default':value} for key,value in [('width',640),('height',384),('steps',20)]], 'defaults':{'prompt':'Keep the scene unchanged.','gpu':'GPU-5090'}}])
    job = {'id':'fixture-result','status':'completed','prompt':'Fixture output','seed':42}
    async def exercise():
      async with pw.async_playwright() as p:
        browser = await p.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox'])
        page = await browser.new_page(viewport={'width':1440,'height':1000})
        page.on('pageerror',lambda error:errors.append(str(error)))
        async def route(handler):
          req = handler.request; path=urlparse(req.url).path
          if path=='/': await handler.fulfill(content_type='text/html',body='<html><head><meta charset="utf-8"><title>Workflow pipeline QA</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
          elif path.startswith('/static/'):
            file=ROOT/path.lstrip('/');await handler.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else mimetypes.guess_type(file.name)[0])
          elif path.endswith('/inventory'):await handler.fulfill(json=bfs if '/bfs/' in path else h3)
          elif path.endswith('/prompt-enhancer'):await handler.fulfill(json={'models':[]})
          elif path.endswith('/gpus'):await handler.fulfill(json={'gpus':gpus})
          elif path.endswith('/jobs') and req.method=='GET':await handler.fulfill(json={'jobs':[job],'queue':{'paused':False},'pagination':{'page':1,'total':1,'total_pages':1},'counts':{'completed':1}})
          elif path.endswith('/jobs') and req.method=='POST':
            msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+req.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+req.post_data_buffer)
            parts=list(msg.iter_parts());config=json.loads(next(part.get_payload(decode=True) for part in parts if part.get_param('name',header='content-disposition')=='config'))
            submissions.append({'path':path,'config':config,'files':[part.get_filename() for part in parts if part.get_filename()]});await handler.fulfill(json={'id':'new-job','status':'queued'})
          elif path.endswith('/video'):await handler.fulfill(body=video,content_type='video/mp4')
          else:await handler.fulfill(status=404,json={'detail':'Fixture not found'})
        await page.route('**/*',route)
        try:
          await page.goto('http://pipeline.test/')
          assert await page.title()=='Workflow pipeline QA'
          async def choose(name):
            await page.evaluate("async()=>window.chooser=(await import('/static/js/workflowPipeline.js')).showWorkflowPipeline()")
            await page.get_by_role('dialog',name='Build Pipeline',exact=True).get_by_role('button',name=name,exact=False).click()
          await choose('MiniMax H3 Video')
          panel=page.get_by_role('dialog',name='MiniMax H3 Video',exact=True)
          await page.get_by_text('Components ready on this Odysseus server.',exact=True).wait_for()
          assert await panel.locator('[data-pipeline-stage]').count()==6
          assert await panel.evaluate('(n)=>n.getBoundingClientRect().width>1200')
          assert await panel.get_by_text('Attach media in Inputs to preview it here.',exact=True).is_visible()
          await panel.locator('[data-pipeline-stage=setup]').click()
          await panel.get_by_role('button',name='Add LoRA',exact=True).click()
          await panel.locator('.h3-lora-row select').select_option('adapter')
          await panel.locator('.h3-lora-row input[type=number]').fill('-0.5')
          enabled=panel.get_by_role('checkbox',name='Enable Identity adapter',exact=True)
          await enabled.uncheck();assert await panel.locator('.h3-lora-row input[type=number]').input_value()=='0'
          await enabled.check();assert await panel.locator('.h3-lora-row input[type=number]').input_value()=='-0.5'
          await panel.locator('[data-pipeline-stage=inputs]').click()
          await page.locator('#h3-reference_videos').set_input_files(str(clip))
          await panel.locator('.workflow-pipeline-media').get_by_role('button',name='Play source',exact=True).click()
          await page.wait_for_function('()=>document.querySelector(".workflow-pipeline-media video")?.readyState>=2')
          await page.evaluate('window.sourcePlayer=document.querySelector(".workflow-pipeline-media video")')
          await panel.locator('[data-pipeline-stage=prepare]').click()
          await page.locator('#h3-prompt').fill('Change only the jacket color.')
          await panel.locator('[data-pipeline-stage=generate]').click()
          await page.locator('#h3-steps').fill('25')
          assert await page.evaluate('window.sourcePlayer===document.querySelector(".workflow-pipeline-media video")&&window.sourcePlayer.readyState>=2')
          await page.screenshot(path=str(tmp_path/'pipeline-h3-desktop.png'))
          await panel.get_by_role('button',name='Generate video',exact=True).click()
          await page.get_by_text('Job new-job added to the queue.',exact=False).wait_for()
          assert submissions[-1]['config']['steps']==25 and submissions[-1]['config']['loras']==[{'id':'adapter','strength':-0.5}]
          assert submissions[-1]['files']==['clip.mp4']
          await panel.locator('[data-pipeline-stage=review]').click()
          await panel.get_by_role('button',name='Play video',exact=True).click()
          await page.wait_for_function('()=>document.querySelector(".workflow-pipeline-results video")?.readyState>=2')
          await page.evaluate('window.resultPlayer=document.querySelector(".workflow-pipeline-results video")')
          await panel.get_by_role('button',name='Refresh jobs',exact=True).click();await page.wait_for_timeout(150)
          assert await page.evaluate('window.resultPlayer===document.querySelector(".workflow-pipeline-results video")&&window.resultPlayer.readyState>=2')
          await panel.locator('[data-pipeline-stage=generate]').click()
          await panel.locator('[data-pipeline-stage=review]').click()
          assert await page.evaluate('window.resultPlayer===document.querySelector(".workflow-pipeline-results video")')
          await panel.get_by_role('button',name='Close',exact=True).click()
          await page.evaluate("async()=>window.classic=(await import('/static/js/h3Video.js')).showH3Video()")
          await page.get_by_text('Components ready on this Odysseus server.',exact=True).wait_for()
          assert await page.locator('.workflow-pipeline').count()==0
          assert await page.locator('#h3-steps').input_value()=='20'
          await panel.get_by_role('button',name='Close',exact=True).click()
          await choose('BFS Video')
          panel=page.get_by_role('dialog',name='BFS Video Workflows',exact=True)
          await panel.locator('[data-pipeline-stage=inputs]').click()
          await page.locator('#bfs-workflow').select_option('h3')
          await page.locator('#bfs-source_video').set_input_files(str(clip))
          # A local image is retained alongside the source across stage changes.
          await page.locator('#bfs-identity_image').set_input_files({'name':'face.png','mimeType':'image/png','buffer':b'fixture-image'})
          await panel.locator('[data-pipeline-stage=generate]').click()
          await page.locator('#bfs-control-steps').fill('22')
          await panel.locator('[data-pipeline-stage=setup]').click()
          assert await page.locator('#bfs-component-model').input_value()=='model'
          await panel.get_by_role('button',name='Show all settings',exact=True).click()
          assert await page.locator('#bfs-control-steps').is_visible() and await page.locator('#bfs-source_video').is_visible()
          await panel.get_by_role('button',name='Generate video',exact=True).click()
          await page.get_by_text('Job new-job added to the queue.',exact=False).wait_for()
          assert submissions[-1]['path']=='/api/video/bfs/jobs'
          assert submissions[-1]['config']['steps']==22 and submissions[-1]['config']['components']=={'model':'model'}
          assert sorted(submissions[-1]['files'])==['clip.mp4','face.png']
          await panel.get_by_role('button',name='Focus one stage',exact=True).click()
          await panel.locator('[data-pipeline-stage=generate]').click()
          await page.set_viewport_size({'width':390,'height':844})
          await page.locator('#bfs-control-steps').scroll_into_view_if_needed()
          await page.screenshot(path=str(tmp_path/'pipeline-bfs-mobile.png'))
          assert await panel.evaluate('(n)=>n.getBoundingClientRect().right<=window.innerWidth')
          assert not errors,errors
        finally:await browser.close()
    asyncio.run(exercise())
