"""A large server source submits its selected range, without browser uploading it."""
import asyncio
import json
import mimetypes
import shutil
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout,width', [('classic', 1440), ('pipeline', 390)])
def test_large_server_video_section_reaches_h3_and_hunter_submission(tmp_path, layout, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Browser plugin unavailable; Chrome required')
    defaults = dict(mode='ref2va', model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                    gpu='GPU-5090', vae_gpu='', width=640, height=384, frames=124, steps=20, seed=42,
                    sampler='euler', scheduler='simple', shift_video=12, shift_audio=3, reference_size='match', loras=[])
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')]
    inventory = dict(runtime_ready=True, components=components, defaults=defaults,
                     gpus=[{'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}],
                     batch_jobs=True, batch_auto_video_length=True, server_video_files=True,
                     seed_hunter={'defaults': defaults, 'scout_defaults': defaults,
                                  'features': {'seed_scout': True, 'separate_final_length': True}})
    selection = {'root': 'e', 'path': 'large-source.mkv', 'size': 4 * 1024 ** 3, 'mtime_ns': '1780000000123456789'}
    submissions, errors, media_requests = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 1000 if width > 400 else 844})
            page.set_default_timeout(6000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                path = urlparse(request.url).path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><title>Large video section QA</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path == '/api/workflows/prompt-history':
                    await handler.fulfill(json={'prompts': [], 'total': 0, 'next_offset': None})
                elif path.endswith('/server-file-roots'):
                    await handler.fulfill(json={'roots': [{'id': 'e', 'name': 'E:', 'available': True}, {'id': 'f', 'name': 'F:', 'available': True}]})
                elif path.endswith('/server-files'):
                    await handler.fulfill(json={'root': 'e', 'path': '', 'parent_path': None, 'total': 1, 'has_more': False,
                                                'entries': [{'name': selection['path'], 'path': selection['path'], 'kind': 'video', 'size': selection['size'], 'selection': selection}]})
                elif path.endswith('/server-files/metadata'):
                    await handler.fulfill(json={'duration_seconds': 600, 'source_frames': 14400})
                elif path.endswith(('/server-files/video', '/server-files/preview')):
                    media_requests.append(path)
                    await handler.fulfill(status=400, json={'detail': 'Media bytes should not be requested until preview is opened'})
                elif path.endswith('/jobs') and request.method == 'GET':
                    await handler.fulfill(json={'jobs': [], 'queue': {'paused': False}, 'pagination': {'page': 1, 'total': 0, 'total_pages': 1}, 'counts': {}})
                elif path.endswith('/jobs') and request.method == 'POST':
                    payload = request.post_data_buffer
                    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + payload)
                    parts = list(message.iter_parts())
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True) for part in parts if not part.get_filename()}
                    submissions.append({'config': json.loads(fields['config']), 'server_inputs': json.loads(fields['server_inputs']),
                                        'auto': fields.get('auto_video_length') == b'true',
                                        'files': [part.get_filename() for part in parts if part.get_filename()], 'request_bytes': len(payload)})
                    await handler.fulfill(json={'id': f'fixture-{len(submissions)}', 'status': 'queued'})
                else:
                    assert request.method == 'GET', f'Unexpected write: {request.method} {path}'
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('https://large-section.test/')
                assert await page.title() == 'Large video section QA'
                for kind, module, function, title, prompt_id, input_id, submit in [
                    ('h3', 'h3Video', 'showH3Video', 'MiniMax H3 Video', 'h3-prompt', 'h3-reference_videos', 'Generate video'),
                    ('hunter', 'h3SeedHunter', 'showH3SeedHunter', 'H3 Seed Hunter', 'seed-hunter-prompt', 'seed-hunter-input-reference_videos', 'Generate previews'),
                    ('scout', 'h3SeedHunter', 'showH3SeedScout', 'H3 Seed Scout', 'seed-hunter-prompt', 'seed-hunter-input-reference_videos', 'Start seed scout'),
                ]:
                    await page.evaluate("async v=>window.workflow=(await import('/static/js/'+v.module+'.js'))[v.function]({layout:v.layout})", {'module': module, 'function': function, 'layout': layout})
                    ready = 'Components ready on this Odysseus server.' if kind == 'h3' else title + ' is ready.'
                    await page.get_by_text(ready, exact=True).wait_for()
                    panel = page.get_by_role('dialog', name=title, exact=True)
                    if layout == 'pipeline':
                        await panel.get_by_role('button', name='Show all settings', exact=True).click()
                    await page.locator('#' + prompt_id).fill('Use only this selected source section.')
                    await page.locator('#' + input_id).locator('..').get_by_role('button', name='Server files', exact=True).click()
                    picker = page.get_by_role('dialog', name='Server files', exact=True)
                    await picker.get_by_role('checkbox', name='large-source.mkv', exact=False).check()
                    assert '4.00 GB' in await picker.inner_text()
                    await picker.get_by_role('button', name='Use 1 video', exact=True).click()
                    await panel.get_by_role('textbox', name='Start at (seconds or time) for large-source.mkv', exact=True).fill('00:23')
                    await panel.get_by_role('spinbutton', name='Length (seconds) for large-source.mkv', exact=True).fill('4')
                    assert 'Use 0:23.00 → 0:27.00' in await panel.inner_text()
                    count = len(submissions)
                    async with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/api/video/h3/jobs')):
                        await panel.get_by_role('button', name=submit, exact=True).click()
                    assert len(submissions) == count + 1
                    sent = submissions[-1]
                    assert sent['config']['reference_video_ranges'] == [{'start_seconds': 23, 'duration_seconds': 4}]
                    assert sent['server_inputs']['reference_videos'] == [{**selection, 'index': 0}]
                    assert sent['files'] == [] and sent['request_bytes'] < 10000
                    if kind == 'hunter':
                        assert sent['config']['seed_hunter']['preview_count'] == 3
                    if kind != 'h3':
                        assert sent['config']['seed_hunter']['stage'] == ('scout' if kind == 'scout' else 'preview')
                    if kind == 'hunter':
                        await panel.get_by_role('textbox', name='Start at (seconds or time) for large-source.mkv', exact=True).scroll_into_view_if_needed()
                        await page.screenshot(path=str(tmp_path / f'large-selected-section-{layout}.png'))
                    await page.evaluate('workflow.close()')
                assert not media_requests, 'Choosing a range must not prefetch the entire 4 GiB source'
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(exercise())
