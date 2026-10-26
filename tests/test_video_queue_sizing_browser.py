"""Actual workflow queue editors: size changes persist without replacing media.

Browser plugin not available; use Playwright with isolated server responses.
"""
import asyncio
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('entry,stage,width', [
    ('showH3SeedHunter', 'preview', 1440),
    ('showH3SeedScout', 'scout', 430),
])
def test_queue_size_quick_edit_bulk_and_reopen(tmp_path, entry, stage, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')

    async def run():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            page.set_default_timeout(8000)
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            config = dict(recipe='seed_hunter', prompt='Keep this saved prompt', mode='ref2va',
                model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                gpu='GPU-5090', width=480, height=864, frames=107, steps=20, seed=42,
                sampler='er_sde', scheduler='beta', shift_video=12, shift_audio=3,
                reference_size='max', loras=[], seed_hunter={'stage':stage,'preview_count':3})
            jobs = [dict(id=c*32, status='queued', revision=0, recipe='seed_hunter',
                         source_name=f'Input {c}.mkv', prompt=config['prompt'], config=dict(config),
                         seed_hunter={'stage':stage,'candidates':[]}) for c in ['a','b']]
            queue = dict(paused=True,queued=2,running=0,total_queued=2,total_running=0)
            inventory = dict(runtime_ready=True, components=[dict(id=r,role=r,name=r,variant='ref2va')
                for r in ['model','encoder','video_vae','audio_vae','latent_upscaler']],
                gpus=[dict(id='GPU-5090',name='RTX 5090')],
                seed_hunter=dict(defaults=config,features={'seed_scout':True}))
            requests = []
            async def route(handler):
                path = urlparse(handler.request.url).path
                if path == '/':
                    return await handler.fulfill(content_type='text/html',body='<html><head><meta charset="utf-8"><title>Odysseus queue sizing QA</title><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                if path.startswith('/static/'):
                    f=ROOT/path.lstrip('/'); return await handler.fulfill(body=f.read_bytes(),content_type='text/javascript' if f.suffix=='.js' else mimetypes.guess_type(f.name)[0])
                if path.endswith('/inventory'): return await handler.fulfill(json=inventory)
                if path.endswith('/gpus'): return await handler.fulfill(json={'gpus':inventory['gpus']})
                if path.endswith('/prompt-enhancer'): return await handler.fulfill(json={'models':[]})
                if path.endswith('/prompt-history'): return await handler.fulfill(json={'prompts':[]})
                if path.endswith('/queue/edit'):
                    body=handler.request.post_data_json; requests.append(body)
                    for selected in body['jobs']:
                        job=next(j for j in jobs if j['id']==selected['id'])
                        assert job['revision']==selected['revision']
                        job['config'].update(body['patch']); job['revision']+=1
                    return await handler.fulfill(json={'updated':len(body['jobs']),'queue':queue})
                if path.endswith('/jobs'):
                    return await handler.fulfill(json={'jobs':jobs,'queue':queue,'pagination':{'page':1,'total':2,'total_pages':1}})
                return await handler.fulfill(status=404,json={'detail':path})
            await page.route('**/*',route)
            await page.goto('https://queue-sizing.test/')
            await page.evaluate("async entry=>{window.workflow=(await import('/static/js/h3SeedHunter.js'))[entry]()}",entry)
            assert await page.title()=='Odysseus queue sizing QA'
            card=page.locator('article[data-job-id="'+jobs[0]['id']+'"]')
            await card.get_by_role('button',name='Quick edit',exact=True).click()
            editor=card.locator('.video-queue-editor')
            ratio=editor.get_by_role('combobox',name='New aspect ratio',exact=True)
            await pw.expect(ratio).to_have_value('9:16')
            await pw.expect(ratio).to_be_enabled()
            await ratio.select_option('16:9')
            mp=editor.get_by_role('spinbutton',name='New megapixels',exact=True)
            await mp.fill('0.4')
            await pw.expect(editor.get_by_label('New width',exact=True)).to_have_value('864')
            await pw.expect(editor.get_by_label('New height',exact=True)).to_have_value('480')
            size_panel=editor.locator('.video-queue-fields > .video-queue-field-wide').first
            await size_panel.screenshot(path=f'/tmp/odysseus-queue-sizing-{width}.png')
            await editor.get_by_role('button',name='Save job settings',exact=True).click()
            await pw.expect(editor).to_be_hidden()
            assert requests[-1]['patch']=={'width':864,'height':480}
            assert len(requests[-1]['jobs'])==1 and jobs[1]['config']['width']==480
            await card.get_by_role('button',name='Quick edit',exact=True).click()
            await pw.expect(ratio).to_have_value('16:9')
            await pw.expect(editor.get_by_label('New width',exact=True)).to_have_value('864')
            # Invalid sizing cannot silently save the preceding valid dimensions.
            await mp.fill('0')
            await pw.expect(editor.get_by_role('button',name='Save job settings',exact=True)).to_be_disabled()
            await mp.fill('0.4')
            await editor.get_by_role('button',name='Close parameter editor',exact=True).click()
            await page.get_by_text('Queue-wide actions',exact=True).click()
            await page.get_by_role('button',name='Edit queued parameters',exact=True).click()
            bulk=page.locator('.video-queue-editor:visible')
            await bulk.get_by_role('combobox',name='New aspect ratio',exact=True).select_option('1:1')
            await bulk.get_by_role('spinbutton',name='New megapixels',exact=True).fill('0.4')
            await bulk.locator('.video-queue-approval input').check()
            await bulk.get_by_role('button',name='Apply to 2 queued jobs',exact=True).click()
            await pw.expect(bulk).to_have_count(0)
            assert requests[-1]['patch']=={'width':640,'height':640}
            assert len(requests[-1]['jobs'])==2
            assert all(j['config']['prompt']=='Keep this saved prompt' for j in jobs)
            assert not errors,errors
            await browser.close()
    asyncio.run(run())
