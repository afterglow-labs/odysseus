"""Actual browser controls attach each row's media without creating extra jobs."""
import asyncio
import json
import mimetypes
import re
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('workflow,layout', [('h3', 'classic'), ('hunter', 'classic'), ('scout', 'pipeline')])
def test_per_row_audio_images_server_files_and_optional_trims(tmp_path, workflow, layout):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required; Browser plugin not available')
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')]
    defaults = dict(mode='ref2va', model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                    latent_upscaler='latent_upscaler', gpu='GPU-5090', vae_gpu='', width=640, height=384,
                    frames=124, steps=20, seed=42, sampler='euler', scheduler='simple', shift_video=12, shift_audio=3,
                    reference_size='match', loras=[])
    inventory = dict(runtime_ready=True, components=components, gpus=[{'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}],
                     defaults=defaults, batch_jobs=True, batch_auto_video_length=True, server_video_files=True,
                     seed_hunter={'features': {'separate_final_length': True, 'seed_scout': True, 'force_audio': True, 'guides': True, 'latent_upscaler': True},
                                  'defaults': defaults, 'scout_defaults': defaults})
    errors, submissions, kinds = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1400, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                url = urlparse(request.url)
                path, query = url.path, parse_qs(url.query)
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><title>Batch media QA</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                elif path.endswith('/server-file-roots'):
                    await handler.fulfill(json={'roots': [{'id': drive, 'name': drive.upper() + ':', 'available': True} for drive in ('e', 'f')]})
                elif path.endswith('/server-files'):
                    kind, drive = query['kind'][0], query['root'][0]
                    kinds.append(kind)
                    names = {'video': ['clip1.mp4', 'clip2.mp4'], 'image': ['face.png'], 'audio': ['voice.mka']}[kind]
                    entries = [{'name': name, 'path': name, 'kind': kind, 'size': 1234,
                                'selection': {'root': drive, 'path': name, 'size': 1234, 'mtime_ns': '1780000000123456789'}} for name in names]
                    await handler.fulfill(json={'root': drive, 'path': '', 'parent_path': None, 'entries': entries, 'total': len(entries), 'has_more': False})
                elif path.endswith('/server-files/metadata'):
                    await handler.fulfill(json={'duration_seconds': 4, 'source_frames': 96})
                elif path.endswith('/jobs') and request.method == 'GET':
                    await handler.fulfill(json={'jobs': [], 'queue': {'paused': False}, 'pagination': {'page': 1, 'total': 0, 'total_pages': 1}, 'counts': {}})
                elif path.endswith('/jobs') and request.method == 'POST':
                    msg = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.post_data_buffer)
                    parts = list(msg.iter_parts())
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True) for part in parts if not part.get_filename()}
                    submissions.append({'config': json.loads(fields['config']), 'server_inputs': json.loads(fields['server_inputs']),
                                        'local_files': [part.get_filename() for part in parts if part.get_filename()], 'auto': fields.get('auto_video_length') == b'true'})
                    await handler.fulfill(json={'id': 'job-' + str(len(submissions)), 'status': 'queued'})
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('http://batch-media.test/')
                assert await page.title() == 'Batch media QA'
                if workflow=='hunter':
                    await page.evaluate("config=>localStorage.setItem('odysseus-h3-seed-hunter-v1',JSON.stringify({config:{...config,seed_hunter:{stage:'preview',preview_count:94,shift_enabled:false}}}))",defaults)
                if workflow == 'h3':
                    await page.evaluate("async layout=>(await import('/static/js/h3Video.js')).showH3Video({layout})", layout)
                    await page.get_by_text('Components ready on this Odysseus server.', exact=True).wait_for()
                    prefix, title = 'h3-', 'MiniMax H3 Video'
                else:
                    await page.evaluate("async({workflow,layout})=>{const mod=await import('/static/js/h3SeedHunter.js');mod[workflow==='scout'?'showH3SeedScout':'showH3SeedHunter']({layout});}", {'workflow': workflow, 'layout': layout})
                    prefix, title = 'seed-hunter-', 'H3 Seed Scout' if workflow == 'scout' else 'H3 Seed Hunter'
                    await page.get_by_text(title + ' is ready.', exact=True).wait_for()
                panel = page.get_by_role('dialog', name=title, exact=True)
                if workflow=='hunter':
                    count=panel.locator('#seed-hunter-option-preview_count')
                    assert await count.evaluate('(n)=>n.tagName')=='SELECT'
                    assert await count.locator('option').all_text_contents()==['1','2','3']
                    assert await count.input_value()=='3'
                if layout == 'pipeline':
                    await panel.get_by_role('button', name='Show all settings', exact=True).click()
                await page.locator('#' + prefix + 'prompt').fill('Use each job’s own references.')

                async def use_server(kind, drive='e', all_files=False):
                    picker = page.get_by_role('dialog', name='Server files', exact=True)
                    if drive != 'e':
                        await picker.get_by_role('combobox', name='Drive', exact=True).select_option(drive)
                    await picker.get_by_role('checkbox').first.wait_for()
                    if all_files:
                        await picker.get_by_role('button', name='Select shown videos', exact=True).click()
                    else:
                        await picker.get_by_role('checkbox').first.check()
                    noun = 'audio file' if kind == 'audio' else kind
                    count = 2 if all_files else 1
                    await picker.get_by_role('button', name=f'Use {count} {noun}' + ('s' if count != 1 else ''), exact=True).click()

                # Ordinary references can also browse server images and audio.
                input_prefix = prefix if workflow == 'h3' else prefix + 'input-'
                for kind, field in [('image', 'reference_images'), ('audio', 'reference_audio')]:
                    wrap = page.locator('#' + input_prefix + field).locator('..')
                    if kind=='audio':assert '.mka' in await page.locator('#'+input_prefix+field).get_attribute('accept')
                    await wrap.get_by_role('button', name='Server files', exact=True).click()
                    await use_server(kind)
                    await wrap.get_by_role('button', name=re.compile('^Remove ')).click()

                batch = panel.locator('.video-batch-panel')
                await batch.locator('[data-batch-toggle]').check()
                await batch.get_by_role('button', name='Server files', exact=True).click()
                await use_server('video', all_files=True)
                rows = batch.locator('.video-batch-row')
                assert await rows.count() == 2
                assert not await rows.locator('input[aria-label^="Choose a section"]').first.is_checked()
                assert await rows.locator('input[aria-label^="Start at"]').first.is_disabled()
                assert await batch.get_by_role('button', name='Queue 2 videos', exact=True).is_enabled()

                if workflow == 'hunter':
                    # Reproduce the old valid-default rejection, then verify the fix.
                    await panel.get_by_text('Sampling and optional controls', exact=True).click()
                    await page.locator('#seed-hunter-option-shift_enabled').check()
                    assert await batch.get_by_role('button', name='Queue 2 videos', exact=True).is_enabled()
                    await page.evaluate("()=>{for(const key of ['shift_video','shift_audio'])document.getElementById('seed-hunter-'+key).step='.1';document.getElementById('seed-hunter-shift_video').dispatchEvent(new Event('input',{bubbles:true}));}")
                    assert await batch.get_by_role('button', name='Queue 2 videos', exact=True).is_disabled()
                    assert 'Video shift:' in await batch.locator('.video-batch-status').inner_text()
                    await page.evaluate("()=>{for(const key of ['shift_video','shift_audio'])document.getElementById('seed-hunter-'+key).step='.01';document.getElementById('seed-hunter-shift_video').dispatchEvent(new Event('input',{bubbles:true}));}")

                for index, drive in [(0, 'e'), (1, 'f')]:
                    row = rows.nth(index)
                    await row.get_by_text('Inputs for this job', exact=True).click()
                    await row.get_by_role('button', name=f'Server audio for clip{index + 1}.mp4', exact=True).click()
                    await use_server('audio', drive=drive)
                await rows.nth(0).locator('[data-batch-input-field=reference_images] input[type=file]').set_input_files({'name': 'own-face.png', 'mimeType': 'image/png', 'buffer': b'image-fixture'})
                await rows.nth(1).get_by_role('button', name='Server image for clip2.mp4', exact=True).click()
                await use_server('image', drive='f')
                await page.set_viewport_size({'width': 390, 'height': 844})
                await rows.nth(1).scroll_into_view_if_needed()
                await page.screenshot(path=str(tmp_path / 'batch-own-media-mobile.png'))
                assert await panel.evaluate('(n)=>n.getBoundingClientRect().right<=innerWidth')
                await page.set_viewport_size({'width': 1400, 'height': 1000})
                await page.screenshot(path=str(tmp_path / 'batch-own-media-desktop.png'))

                # Closing/reopening keeps each row's primary and companions.
                await panel.get_by_role('button', name='Close', exact=True).click()
                if workflow == 'h3':
                    await page.evaluate("async layout=>(await import('/static/js/h3Video.js')).showH3Video({layout})", layout)
                else:
                    await page.evaluate("async({workflow,layout})=>{const mod=await import('/static/js/h3SeedHunter.js');mod[workflow==='scout'?'showH3SeedScout':'showH3SeedHunter']({layout});}", {'workflow': workflow, 'layout': layout})
                if layout == 'pipeline':
                    await panel.get_by_role('button', name='Show all settings', exact=True).click()
                await batch.get_by_role('button', name='Queue 2 videos', exact=True).click()
                await page.wait_for_function("()=>document.querySelectorAll('.video-batch-row[data-status=queued]').length===2")
                assert len(submissions) == 2
                for index, submission in enumerate(submissions):
                    assert submission['server_inputs']['reference_videos'][0]['path'] == f'clip{index + 1}.mp4'
                    assert submission['server_inputs']['reference_audio'][0]['root'] == ('e' if index == 0 else 'f')
                    assert len(submission['server_inputs']['reference_audio']) == 1
                    assert submission['server_inputs']['reference_audio'][0]['path']=='voice.mka'
                    assert submission['auto'] is True
                    assert submission['config'].get('reference_video_ranges', []) == []
                    if workflow=='hunter':assert submission['config']['seed_hunter']['preview_count']==3
                assert submissions[0]['local_files'] == ['own-face.png']
                assert submissions[1]['server_inputs']['reference_images'][0]['root'] == 'f'
                assert {'video', 'image', 'audio'} <= set(kinds)
                assert not errors, errors
            finally:
                await browser.close()

    asyncio.run(exercise())
