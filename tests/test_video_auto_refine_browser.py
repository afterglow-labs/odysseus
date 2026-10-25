import asyncio
from email.parser import BytesParser
from email.policy import default
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('scout,layout,width', [(False,'classic',1440), (True,'pipeline',1440), (False,'classic',430), (True,'classic',430)])
def test_refinements_require_explicit_saved_candidate_action(tmp_path, scout, layout, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Chrome required')
    config = dict(recipe='seed_hunter', prompt='Keep the saved source.', mode='ref2va', model='model', encoder='encoder',
                  video_vae='video_vae', audio_vae='audio_vae', latent_upscaler='upscaler', gpu='GPU-5090', vae_gpu='',
                  width=480,height=864,frames=107,steps=20,seed=714,sampler='euler',scheduler='simple',shift_video=12,
                  shift_audio=3,reference_size='max',loras=[],seed_hunter=dict(stage='scout' if scout else 'preview',
                  preview_count=3,scout_count=3,scout_step=4,scout_mode='scout',final_frames=294))
    components=[dict(id=r,role=r,name=r,variant='ref2va') for r in ['model','encoder','video_vae','audio_vae']]
    components += [dict(id='upscaler',role='latent_upscaler',name='H3 learned upscaler'),dict(id='lora',role='lora',name='Test LoRA',variant='ref2va')]
    inventory=dict(runtime_ready=True,components=components,gpus=[dict(id='GPU-5090',name='RTX 5090',nvfp4=True)],
        seed_hunter=dict(defaults=config,scout_defaults=config,features={k:True for k in ['auto_refine','latent_upscaler','scout_upscale','seed_scout','separate_final_length','rife']}))
    jobs=[dict(id=c*32,status='completed',recipe='seed_hunter',prompt='Saved prompt',source_name=f'clip{c}.mkv',
               seed_hunter={'stage':'scout' if scout else 'preview','candidates':[]},
               **({'scout':{'mode':'scout','seeds':[714,715,716],'previews':[]}} if scout else {})) for c in ['a','b']]
    requests=[]; submissions=[]; errors=[]
    async def run():
        async with pw.async_playwright() as p:
            browser=await p.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox'])
            page=await browser.new_page(viewport={'width':width,'height':1000});page.set_default_timeout(10000)
            page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(h):
                req=h.request;path=urlparse(req.url).path
                if path=='/':await h.fulfill(content_type='text/html',body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    f=ROOT/path.lstrip('/');await h.fulfill(body=f.read_bytes(),content_type='text/javascript' if f.suffix=='.js' else mimetypes.guess_type(f.name)[0])
                elif path.endswith('/inventory'):await h.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):await h.fulfill(json={'models':[]})
                elif path.endswith('/prompt-history'):await h.fulfill(json={'prompts':[]})
                elif path.endswith('/seed-hunter'):await h.fulfill(json={'config':config,'seed_hunter':jobs[0]['seed_hunter']})
                elif path.endswith('/auto-refine'):
                    requests.append(req.post_data_json)
                    if len(requests)==1:await h.fulfill(status=503,json={'detail':'Response lost after acceptance'})
                    else:await h.fulfill(json={'jobs':{j['id']:[] for j in jobs},'created':4,'skipped':2,'errors':{},'output_folder':'/fixture/outputs/video-projects/Test'})
                elif path.endswith('/jobs'):
                    if req.method=='POST':
                        msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+req.headers['content-type'].encode()+b'\r\nMIME-Version: 1.0\r\n\r\n'+req.post_data_buffer)
                        submissions.append(json.loads(next(part.get_payload(decode=True) for part in msg.iter_parts() if part.get_param('name',header='content-disposition')=='config')))
                        await h.fulfill(json={'id':'newjob'})
                    else:await h.fulfill(json={'jobs':jobs,'queue':{'paused':True},'pagination':{'page':1,'total':2,'total_pages':1,'before':100}})
                else:await h.fulfill(status=404,json={'detail':'Fixture route missing'})
            await page.route('**/*',route)
            try:
                await page.goto('https://auto-refine.test/')
                await page.evaluate("async v=>(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter(v)",{'scout':scout,'layout':layout})
                await page.get_by_text(('H3 Seed Scout' if scout else 'H3 Seed Hunter')+' is ready.',exact=True).wait_for()
                if layout=='pipeline':await page.locator('[data-pipeline-stage=generate]').click()
                assert not await page.get_by_role('button',name='Automatic refinements…',exact=True).is_visible()
                assert not requests, 'Opening a workflow must never queue refinements'
                if layout=='pipeline':await page.locator('[data-pipeline-stage=review]').click()
                await page.get_by_role('button',name='Select all preview jobs',exact=True).click()
                await page.get_by_role('button',name='Refine selected jobs (2)…',exact=True).click()
                await page.locator('#h3-auto-project').fill('Our batch')
                await page.get_by_label('All candidates',exact=True).check()
                await page.get_by_label('Existing results',exact=True).select_option('rerender')
                await page.locator('#seed-hunter-final-steps').fill('7')
                await page.locator('#seed-hunter-final-output_fps').select_option('48')
                assert not await page.locator('#seed-hunter-final-seed').is_visible()
                assert await page.locator('#seed-hunter-final-latent_upscaler').is_visible()
                await page.locator('.h3-auto-refine-settings').scroll_into_view_if_needed()
                await page.screenshot(path=str(tmp_path/f'auto-refine-{scout}-{width}.png'))
                await page.get_by_role('button',name='Queue bulk refinements',exact=True).click()
                await page.get_by_text('Response lost after acceptance',exact=False).wait_for()
                assert await page.locator('#seed-hunter-final-steps').is_disabled()
                if width > 900:
                    await page.locator('.h3-seed-hunter-final').get_by_role('button',name='Retry bulk refinement submission',exact=True).click()
                else:
                    await page.get_by_role('button',name='Retry bulk refinement submission',exact=True).last.click()
                await page.get_by_text('4 refinements queued',exact=False).wait_for()
                assert len(requests)==2 and requests[0]==requests[1]
                assert set(requests[0]['jobs'])=={j['id'] for j in jobs}
                assert requests[0]['settings']['candidates']=='all' and requests[0]['settings']['existing']=='rerender'
                assert requests[0]['settings']['refinement']['steps']==7
                assert not errors,errors
            finally:await browser.close()
    asyncio.run(run())
