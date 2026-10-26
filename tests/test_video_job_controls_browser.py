"""Exercise real job cards with delayed requests and polling, without a GPU."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse, parse_qs
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_batch_files_stop_feedback_and_process_now(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')

    async def run():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1280, 'height': 1000})
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            batch = 'b'*32
            rows = [dict(id=f'{i:032x}', status='running' if i == 1 else 'queued', source_name=f'Source clip {i:02}.mkv',
                prompt=f'Saved prompt for clip {i:02}', recipe='seed_hunter', queue_position=i-1 if i>1 else None,
                seed_hunter={'stage':'preview','frames':98,'final_frames':362,'candidates':[]},
                input_videos=[dict(field='reference_videos',index=0,name=f'Source clip {i:02}.mkv',url=f'/api/video/h3/jobs/{i:032x}/inputs/video')]) for i in range(1,12)]
            inventory = dict(runtime_ready=True, components=[dict(id=r, role=r, name=r, variant='ref2va') for r in
                ['model','encoder','video_vae','audio_vae','latent_upscaler']], gpus=[dict(id='GPU-5090',name='RTX 5090',nvfp4=True)],seed_hunter=dict(features={},defaults={}))
            calls = []; gate = asyncio.Event(); failed_detail = False

            def queue():
                running = next((r for r in rows if r['status']=='running'), None)
                return dict(paused=False,total_running=int(bool(running)),total_queued=sum(r['status']=='queued' for r in rows),current_jobs=[running] if running else [])

            async def route(handler):
                nonlocal failed_detail
                url=urlparse(handler.request.url); path=url.path; q=parse_qs(url.query)
                if path=='/': return await handler.fulfill(content_type='text/html',body='<html><head><meta charset="utf-8"><title>Odysseus job controls QA</title><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                if path.startswith('/static/'):
                    f=ROOT/path.lstrip('/');return await handler.fulfill(body=f.read_bytes(),content_type='text/javascript' if f.suffix=='.js' else mimetypes.guess_type(f.name)[0])
                if path.endswith('/inventory'):return await handler.fulfill(json=inventory)
                if path.endswith('/gpus'):return await handler.fulfill(json={'gpus':inventory['gpus']})
                if path.endswith('/prompt-enhancer'):return await handler.fulfill(json={'models':[]})
                if path=='/api/workflows/prompt-history':return await handler.fulfill(json={'prompts':[],'has_more':False})
                master=dict(id='batch-'+batch,batch=dict(id=batch,name='Eleven video batch',count=11),batch_count=11,status='running',recipe='seed_hunter',seed_hunter={'stage':'preview'},counts={'running':queue()['total_running'],'queued':queue()['total_queued']})
                if path.endswith('/jobs'):return await handler.fulfill(json=dict(jobs=[master],queue=queue(),counts=master['counts'],pagination=dict(page=1,total=1,total_pages=1,before=9999999999)))
                if '/batches/' in path:
                    if not failed_detail:
                        failed_detail=True;return await handler.fulfill(status=503,json={'detail':'Temporary batch read failure'})
                    n=int(q.get('page',['1'])[0]);visible=rows
                    if q.get('active_only')==['true']:
                        visible=sorted((r for r in rows if r['status'] in ['running','queued']),key=lambda r:(r['status']!='running',r.get('queue_position') or 0))
                    return await handler.fulfill(json=dict(jobs=visible[(n-1)*10:n*10],pagination=dict(page=n,total=len(visible),total_pages=2),queue=queue()))
                if path.endswith('/actions'):
                    identity=path.split('/')[-2];action=handler.request.post_data_json['action'];calls.append((identity,action))
                    await gate.wait()
                    row=next(r for r in rows if r['id']==identity)
                    if action=='stop':row['status']='stopped';rows[1]['status']='running'
                    else:
                        for r in rows:
                            if r['status']=='queued':r['queue_position']+=1
                        row['queue_position']=1
                    return await handler.fulfill(json=dict(failed=0,prioritized=identity,queue=queue()))
                return await handler.fulfill(status=404,json={'detail':path})

            await page.route('**/*', route)
            await page.goto('https://job-controls.test/')
            await page.evaluate("async()=>{window.workflow=(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter()}")
            dialog=page.get_by_role('dialog');master=dialog.locator('.video-batch-master')
            await master.get_by_role('button',name='View 11 videos & prompts',exact=True).click()
            await pw.expect(master).to_contain_text('Could not load files: Temporary batch read failure')
            await master.get_by_role('button',name='Retry loading files',exact=True).click()
            await pw.expect(master.locator('[data-job-id]')).to_have_count(10)
            await pw.expect(master.get_by_role('navigation',name='Files in batch')).to_contain_text('Page 1 of 2 · 11 file jobs')
            first=master.locator('[data-job-id]').first
            await first.get_by_text('Prompt used',exact=True).click()
            await pw.expect(first).to_contain_text('Saved prompt for clip 01')
            await pw.expect(first.get_by_role('button',name='Play original video',exact=True)).to_be_visible()
            stop=first.get_by_role('button',name='Stop job',exact=True);await stop.click()
            await pw.expect(first.get_by_role('button',name='Stopping…',exact=True)).to_be_disabled()
            # A poll arrives while the action is pending. It must not reset it.
            await page.wait_for_timeout(3500)
            await pw.expect(first.get_by_role('button',name='Stopping…',exact=True)).to_be_disabled()
            assert calls==[(rows[0]['id'],'stop')]
            gate.set()
            await pw.expect(first).to_contain_text('Stopped. Saved files are kept.')
            await pw.expect(first).to_contain_text('Now rendering: Source clip 02.mkv')
            gate.clear()
            third=master.locator('[data-job-id]').nth(2)
            await third.get_by_role('button',name='Process now',exact=True).click()
            await pw.expect(third.get_by_role('button',name='Moving…',exact=True)).to_be_disabled()
            gate.set()
            await pw.expect(third).to_contain_text('Moved to queue position 1.')
            await pw.expect(third).to_contain_text('Starts after Source clip 02.mkv finishes.')
            await pw.expect(third).to_contain_text('Queue position 1')
            await master.get_by_role('button',name='Next files',exact=True).click()
            await pw.expect(master.locator('[data-job-id]')).to_have_count(1)
            await pw.expect(master).to_contain_text('Source clip 11.mkv')
            await pw.expect(master.get_by_role('navigation',name='Files in batch')).to_contain_text('Page 2 of 2')
            await master.screenshot(path='/tmp/odysseus-batch-controls-desktop.png')
            await page.set_viewport_size({'width':390,'height':844})
            await master.scroll_into_view_if_needed()
            await page.screenshot(path='/tmp/odysseus-batch-controls-mobile.png')
            assert not errors,errors
            await browser.close()
    asyncio.run(run())
