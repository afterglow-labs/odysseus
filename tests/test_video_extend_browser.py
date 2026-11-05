"""Extending a result opens the real continuation form without queuing it."""
import asyncio
import mimetypes
import shutil
from pathlib import Path
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width', [1440, 390])
def test_extend_result_opens_isolated_continuation_draft(tmp_path, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    identity = 'a' * 32
    roles = ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')
    components = [dict(id=role, role=role, name=role + '.safetensors', variant='ref2va') for role in roles]
    config = dict(recipe='seed_hunter', mode='ref2va', prompt='Continue the saved scene.', width=864, height=480,
                  steps=8, frames=124, seed=42, sampler='euler', scheduler='simple', gpu='GPU-5090',
                  seed_hunter=dict(stage='continuation', source_start_seconds=0, source_end_seconds=None),
                  **{role: role for role in roles})
    inventory = dict(runtime_ready=True, components=components, server_video_files=True, gpus=[dict(id='GPU-5090', name='RTX 5090')],
                     seed_hunter=dict(features=dict(continuation=True), defaults={}))
    draft = dict(family='h3', config=config, uploads=dict(source_video=[dict(root='job-output', path=identity+'/0/completed.mp4', size=1234, mtime_ns='12345')]),
                 notice='Completed video attached in full. Continue a video is selected.')

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport=dict(width=width, height=1000))
            errors, writes = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route(handler):
                request = handler.request; path = urlparse(request.url).path
                if request.method not in ('GET', 'HEAD'):
                    writes.append(path); return await handler.fulfill(status=405, json=dict(detail='Read only'))
                if path == '/':
                    return await handler.fulfill(content_type='text/html', body='<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><body></body>')
                if path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    return await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                if path.endswith('/extend'): return await handler.fulfill(json=draft)
                if path.endswith('/inventory'): return await handler.fulfill(json=inventory)
                if path.endswith('/jobs'): return await handler.fulfill(json=dict(jobs=[], pagination=dict(page=1, total=0, total_pages=1), queue=dict(paused=False)))
                if path.endswith('/prompt-enhancer'): return await handler.fulfill(json=dict(models=[]))
                if path.endswith('/prompt-history'): return await handler.fulfill(json=dict(prompts=[]))
                if path.endswith('/gpus'): return await handler.fulfill(json=dict(gpus=inventory['gpus']))
                if path.endswith('/server-files/metadata'): return await handler.fulfill(json=dict(duration_seconds=60))
                return await handler.fulfill(status=404, json=dict(detail='Fixture not found'))
            await page.route('**/*', route)
            try:
                await page.goto('https://extend-video.test/')
                await page.evaluate("async id=>{localStorage.setItem('odysseus-h3-seed-hunter-v1','saved base draft');document.body.append((await import('/static/js/videoJobReuse.js')).createExtendVideoButton(id));}", identity)
                await page.get_by_role('button', name='Extend this video', exact=True).click()
                panel = page.get_by_role('dialog', name='H3 Seed Hunter', exact=True)
                await pw.expect(panel).to_contain_text('Completed video attached in full.')
                await pw.expect(panel.locator('#seed-hunter-option-stage')).to_have_value('continuation')
                await pw.expect(panel).to_contain_text('completed.mp4')
                await pw.expect(panel.locator('#seed-hunter-steps')).to_have_value('8')
                await panel.locator('#seed-hunter-steps').fill('10')
                assert await page.evaluate("localStorage.getItem('odysseus-h3-seed-hunter-v1')") == 'saved base draft'
                await page.screenshot(path=str(tmp_path / f'extend-video-{width}.png'))
                assert not writes and not errors
            finally:
                await browser.close()
    asyncio.run(exercise())
