"""Exercise the real preset picker in classic and Pipeline layouts."""
import asyncio
from email.parser import BytesParser
from email.policy import default
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

from src.h3_seed_hunter_presets import catalog
from scripts.h3_seed_hunter_presets import CHARACTER_SWAP, CHARACTER_TURBO, TURBO4, TURBO8, PIKA, LIGHTVAE, PRISM8

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout,width', [('classic',1440),('pipeline',1440),('classic',390),('pipeline',390)])
def test_presets_keep_prompt_media_and_clip_lengths_and_queue_real_settings(layout,width,tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Chrome required')
    components = [dict(id=name, name=name, role=role, variant='ref2va') for role,name in [
        ('model','Minimax-h3_Singularity_ref2va_v2_pruned_int8.safetensors'),('model','Minimax_h3_ref2va_pruned_int8.safetensors'),
        ('model',PRISM8),('video_vae','minimax_h3_video_vae_fp16.safetensors'),('video_vae',LIGHTVAE),
        ('latent_upscaler','minimax_h3_latent_upscaler.safetensors'),('encoder','encoder'),('audio_vae','audio_vae'),
        *[('lora',name) for name in (TURBO4,TURBO8,PIKA,CHARACTER_SWAP,CHARACTER_TURBO)]]]
    components[-1]['variant']='fl2va'
    defaults=dict(mode='ref2va',gpu='GPU-5090',vae_gpu='',encoder='encoder',audio_vae='audio_vae',
        width=480,height=864,frames=107,seed=123,steps=20,sampler='er_sde',scheduler='beta',
        reference_size='max',shift_video=12,shift_audio=6,**{role:next(c['id'] for c in components if c['role']==role) for role in ('model','video_vae','latent_upscaler')})
    inventory=dict(runtime_ready=True,components=components,gpus=[dict(id='GPU-5090',name='RTX 5090')],
        seed_hunter=dict(defaults=defaults,presets=catalog(components),features=dict(latent_upscaler=True,separate_final_length=True)))
    requests, errors = [], []
    async def exercise():
        async with pw.async_playwright() as p:
            browser=await p.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox'])
            page=await browser.new_page(viewport=dict(width=width,height=1000))
            page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(handler):
                request=handler.request;path=urlparse(request.url).path
                if path=='/':await handler.fulfill(content_type='text/html',body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file=ROOT/path.lstrip('/');await handler.fulfill(body=file.read_bytes(),content_type='text/javascript' if file.suffix=='.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):await handler.fulfill(json=inventory)
                elif path.endswith('/jobs') and request.method=='POST':
                    msg=BytesParser(policy=default).parsebytes(b'Content-Type: '+request.headers['content-type'].encode()+b'\r\n\r\n'+request.post_data_buffer)
                    parts=list(msg.iter_parts())
                    requests.append((json.loads(next(part.get_payload(decode=True) for part in parts if part.get_param('name',header='content-disposition')=='config')), [part.get_filename() for part in parts if part.get_filename()]))
                    await handler.fulfill(json=dict(id='queued',status='queued'))
                elif path.endswith('/jobs'):await handler.fulfill(json=dict(jobs=[],queue={},pagination=dict(page=1,total=0,total_pages=1)))
                else:await handler.fulfill(json=dict(models=[],prompts=[],jobs=[],queue={},targets=[]))
            await page.route('**/*',route)
            try:
                await page.goto('https://hunter-presets.test/')
                key='odysseus-h3-seed-hunter-v1'+(':pipeline' if layout=='pipeline' else '')
                await page.evaluate('({key,defaults})=>localStorage.setItem(key,JSON.stringify({config:{...defaults,recipe:"seed_hunter",prompt:"Keep the boat and scene.",seed_hunter:{stage:"preview",preview_count:3,final_frames:311}}}))',dict(key=key,defaults=defaults))
                await page.evaluate("async layout=>(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})",layout)
                await page.get_by_text('H3 Seed Hunter is ready.',exact=True).wait_for()
                picker=page.get_by_label('Generation preset',exact=True)
                await pw.expect(picker).to_be_visible()
                await picker.select_option('pika')
                assert await page.locator('#seed-hunter-sampler').input_value()=='lcm'
                await picker.select_option('director')
                assert await page.locator('#seed-hunter-option-preview_count').input_value()=='4'
                if layout=='pipeline':await page.get_by_role('button',name='Show all settings',exact=True).click()
                image=page.locator('#seed-hunter-input-reference_images')
                await image.set_input_files(dict(name='identity.png',mimeType='image/png',buffer=b'fixture-owned-image'))
                await page.locator('#seed-hunter-option-director_shots').fill('2 | Wide shot.\n1 | Close-up.')
                await picker.select_option('turbo4')
                await pw.expect(page.locator('#seed-hunter-prompt')).to_have_value('Keep the boat and scene.')
                assert await page.locator('#seed-hunter-frames').input_value()=='107'
                assert 'identity.png' in await page.locator('.h3-seed-hunter-dialog').inner_text()
                await picker.scroll_into_view_if_needed()
                output=ROOT/'outputs/qa';output.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output/f'hunter-presets-{layout}-{width}.png'))
                await page.get_by_role('button',name='Generate previews',exact=True).click()
                await page.wait_for_function('document.body.textContent.includes("Job added to the shared video queue")')
                assert len(requests)==1
                saved,files=requests[0]
                assert saved['steps']==4 and saved['sampler']=='euler'
                assert saved['seed_hunter']['preset']=='turbo4' and saved['seed_hunter']['final_frames']==311
                assert saved['frames']==107 and saved['prompt']=='Keep the boat and scene.'
                assert files==['identity.png']
                assert not errors
            finally:
                (tmp_path/'page.html').write_text(await page.content())
                await browser.close()
    asyncio.run(exercise())
