"""Short preview length and source-matched final length stay separate in real forms."""
import asyncio
import json
import mimetypes
import shutil
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout,separate_final', [('classic', True), ('pipeline', True), ('classic', False)])
def test_preview_seconds_apply_immediately_and_final_length_is_independent(tmp_path, layout, separate_final):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required; Browser plugin unavailable')
    defaults = dict(mode='ref2va', model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                    latent_upscaler='latent_upscaler', gpu='GPU-5090', vae_gpu='', width=640, height=384,
                    frames=294, steps=20, seed=42, sampler='euler', scheduler='simple', shift_video=12, shift_audio=3,
                    reference_size='match', loras=[])
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')]
    inventory = dict(runtime_ready=True, components=components, gpus=[{'id': 'GPU-5090', 'name': 'RTX 5090'}],
                     batch_jobs=True, batch_auto_video_length=True, server_video_files=True,
                     seed_hunter={'defaults': defaults, 'features': {'latent_upscaler': True,
                         'separate_final_length': separate_final}})
    candidates = [{'index': index, 'seed': 42 + 1000 * (index - 1), 'can_refine': True, 'status': 'completed'}
                  for index in range(1, 4)]
    job = {'id': 'saved-previews', 'status': 'completed', 'prompt': 'Neutral saved preview.',
           'seed_hunter': {'stage': 'preview', 'candidates': candidates}}
    detail = {'config': {**defaults, 'frames': 107, 'recipe': 'seed_hunter',
                        'seed_hunter': {'stage': 'preview', 'preview_count': 3, 'final_frames': 311}},
              'refinement_defaults': {'frames': 311},
              'final_length': {'frames': 311, 'selected_seconds': 311 / 24, 'duration_seconds': 12.262,
                               'preview_frames': 107, 'target': 'final', 'regeneration_required': True}}
    submissions, finals, errors = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_default_timeout(7000)

            async def route(handler):
                request = handler.request
                url = urlparse(request.url)
                path, query = url.path, parse_qs(url.query)
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><title>Preview duration QA</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
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
                    await handler.fulfill(json={'roots': [{'id': 'f', 'name': 'F:', 'available': True}]})
                elif path.endswith('/server-files'):
                    entries = [{'name': name, 'path': name, 'kind': 'video', 'size': 1234,
                                'selection': {'root': 'f', 'path': name, 'size': 1234, 'mtime_ns': '1780000000123456789'}}
                               for name in ('clip1.mp4', 'clip2.mp4')]
                    await handler.fulfill(json={'root': 'f', 'path': '', 'parent_path': None, 'entries': entries, 'total': 2, 'has_more': False})
                elif path.endswith('/server-files/metadata'):
                    await handler.fulfill(json={'duration_seconds': 12.262, 'source_frames': 294})
                elif path.endswith('/jobs/saved-previews/seed-hunter'):
                    await handler.fulfill(json=detail)
                elif path.endswith('/seed-hunter/finalize'):
                    finals.append(json.loads(request.post_data))
                    await handler.fulfill(json={'id': 'final-job', 'status': 'queued'})
                elif path.endswith('/jobs') and request.method == 'GET':
                    await handler.fulfill(json={'jobs': [job], 'queue': {'paused': False}, 'pagination': {'page': 1, 'total': 1, 'total_pages': 1}, 'counts': {'completed': 1}})
                elif path.endswith('/jobs') and request.method == 'POST':
                    msg = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.post_data_buffer)
                    parts = list(msg.iter_parts())
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True) for part in parts if not part.get_filename()}
                    submissions.append({'config': json.loads(fields['config']), 'auto': fields.get('auto_video_length') == b'true',
                                        'server_inputs': json.loads(fields['server_inputs'])})
                    await handler.fulfill(json={'id': f'batch-{len(submissions)}', 'status': 'queued',
                        'batch_video_length': {'target': 'final', 'preview_frames': 107, 'duration_seconds': 12.262, 'selected_seconds': 311 / 24, 'frames': 311}})
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('https://hunter-duration.test/')
                key = 'odysseus-h3-seed-hunter-v1' + (':pipeline' if layout == 'pipeline' else '')
                await page.evaluate("({key,config})=>localStorage.setItem(key,JSON.stringify({config:{...config,prompt:'Keep the saved scene.',recipe:'seed_hunter',seed_hunter:{stage:'preview',preview_count:3,final_frames:null}}}))", {'key': key, 'config': defaults})
                await page.evaluate("async layout=>(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})", layout)
                await page.get_by_text('H3 Seed Hunter is ready.', exact=True).wait_for()
                panel = page.get_by_role('dialog', name='H3 Seed Hunter', exact=True)
                if layout == 'pipeline':
                    await panel.get_by_role('button', name='Show all settings', exact=True).click()
                seconds = panel.get_by_role('spinbutton', name='Preview seconds', exact=True)
                frames = page.locator('#seed-hunter-frames')
                assert await seconds.input_value() == '12.25'
                await seconds.fill('4')  # No Apply click.
                assert await frames.input_value() == '107'
                assert await page.locator('#seed-hunter-option-preview_count').input_value() == '3'
                batch = panel.locator('.video-batch-panel')
                await batch.locator('[data-batch-toggle]').check()
                automatic = batch.locator('[data-batch-auto-length]')
                assert 'Match final output length to each video' in await automatic.locator('..').inner_text()
                assert await automatic.is_checked()
                assert await frames.is_enabled()
                assert await seconds.is_enabled()
                await frames.fill('90')
                assert await seconds.input_value() == '3.75'
                assert await automatic.is_checked()
                await seconds.fill('4')
                await batch.get_by_role('button', name='Server files', exact=True).click()
                picker = page.get_by_role('dialog', name='Server files', exact=True)
                await picker.get_by_role('button', name='Select shown videos', exact=True).click()
                await picker.get_by_role('button', name='Use 2 videos', exact=True).click()
                assert await batch.locator('.video-batch-row').count() == 2
                assert await frames.input_value() == '107'
                await automatic.uncheck()
                assert await frames.input_value() == '107'
                await automatic.check()
                assert await batch.locator('.video-batch-row').count() == 2
                if not separate_final:
                    assert await batch.get_by_text('Update the Odysseus server', exact=False).is_visible()
                await batch.get_by_role('button', name='Queue 2 videos', exact=True).click()
                await page.wait_for_function("()=>document.querySelectorAll('.video-batch-row[data-status=queued]').length===2")
                assert len(submissions) == 2
                for index, submission in enumerate(submissions):
                    assert submission['config']['frames'] == 107
                    assert submission['config']['seed_hunter']['preview_count'] == 3
                    assert submission['config']['seed_hunter']['final_frames'] is None
                    assert submission['auto'] is separate_final
                    assert submission['server_inputs']['reference_videos'][0]['path'] == f'clip{index + 1}.mp4'
                assert '4.46s previews' in await batch.inner_text()
                await batch.locator('[data-batch-toggle]').uncheck()
                if layout == 'pipeline':
                    await panel.get_by_role('button', name='Focus one stage', exact=True).click()
                    await panel.locator('[data-pipeline-stage=review]').click()
                await panel.get_by_role('button', name='Use this preview', exact=True).nth(1).click()
                final = panel.locator('.h3-seed-hunter-final')
                await final.wait_for()
                final_frames = final.locator('#seed-hunter-final-frames')
                assert 'Final output length' in await final_frames.locator('..').inner_text()
                if separate_final:
                    assert await final_frames.input_value() == '311'
                    assert 'Preview: 4.46 seconds. Final output: 12.96 seconds.' in await final.inner_text()
                    assert 'selected seed is rendered at the final length' in await final.inner_text()
                    await final_frames.select_option('243')
                    assert await frames.input_value() == '107'
                    await final.get_by_role('button', name='Render selected preview', exact=True).click()
                    await page.wait_for_function("()=>document.querySelector('.h3-seed-hunter-final button[type=submit]').disabled===false")
                    assert len(finals) == 1, await final.locator('form').evaluate('(form)=>[...form.elements].filter(n=>n.validity&&!n.validity.valid).map(n=>({id:n.id,value:n.value,message:n.validationMessage}))')
                    assert finals[0]['candidate'] == 2
                    assert finals[0]['config']['frames'] == 243
                    assert finals[0]['config']['seed'] == candidates[1]['seed']
                    # Old jobs whose source cannot be probed require an explicit valid choice.
                    await final.get_by_role('button', name='Clear selection', exact=True).click()
                    detail['final_length'] = {'frames': None, 'requires_manual': True, 'error': 'Source duration unavailable.'}
                    if layout == 'pipeline':
                        await panel.locator('[data-pipeline-stage=review]').click()
                    await panel.get_by_role('button', name='Use this preview', exact=True).first.click()
                    await final.wait_for()
                    assert await final_frames.input_value() == ''
                    assert await final.get_by_role('button', name='Render selected preview', exact=True).is_disabled()
                    await final_frames.select_option('107')
                    assert await final.get_by_role('button', name='Render selected preview', exact=True).is_enabled()
                    assert 'Reuses the selected preview’s latent' in await final.inner_text()
                    await page.set_viewport_size({'width': 390, 'height': 844})
                    await final_frames.scroll_into_view_if_needed()
                    await page.screenshot(path=str(tmp_path / 'hunter-final-duration-mobile.png'))
                    assert await panel.evaluate('(node)=>node.getBoundingClientRect().right<=innerWidth')
                    await page.set_viewport_size({'width': 1440, 'height': 1000})
                    await page.screenshot(path=str(tmp_path / 'hunter-final-duration-desktop.png'))
                else:
                    assert await final_frames.is_hidden()
                assert not errors, errors
            finally:
                await browser.close()

    asyncio.run(exercise())
