"""Grouped results preserve preview selection, playback and child-only actions."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout', ['classic', 'pipeline'])
def test_refinements_stay_below_their_preview_without_replacing_playback(tmp_path, layout):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    ffmpeg = pytest.importorskip('imageio_ffmpeg').get_ffmpeg_exe()
    clip = tmp_path / 'clip.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=purple:s=160x90:d=20',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(clip)], check=True)
    candidates = [{'index': i, 'seed': i * 1000, 'can_refine': True} for i in (1, 2, 3)]
    refinements = [{'id': 'final-one', 'status': 'completed', 'url': '/api/video/h3/jobs/final-one/video',
                    'seed_hunter': {'stage': 'final', 'source_candidate': 1}},
                   {'id': 'final-three', 'status': 'queued', 'queue_position': 1,
                    'seed_hunter': {'stage': 'final', 'source_candidate': 3}}]
    job = {'id': 'source', 'status': 'completed', 'seed_hunter': {'stage': 'preview', 'candidates': candidates},
           'refinements': refinements}
    components = [{'id': r, 'role': r, 'name': r + '.safetensors', 'variant': 'ref2va'}
                  for r in ['model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler']]
    inventory = {'runtime_ready': True, 'components': components, 'gpus': [{'id': 'GPU-5090', 'name': 'RTX 5090'}],
                 'seed_hunter': {'defaults': {}, 'features': {'latent_upscaler': True}}}
    errors, writes, media_requests, grouped = [], [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1365, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route(handler):
                req = handler.request
                url = urlparse(req.url); path = url.path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif req.method == 'DELETE':
                    writes.append(path)
                    assert path == '/api/video/h3/jobs/final-three/record'
                    refinements[:] = [r for r in refinements if r['id'] != 'final-three']
                    await handler.fulfill(json={'deleted': True})
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/jobs'):
                    grouped.append(parse_qs(url.query).get('group_refinements'))
                    await handler.fulfill(json={'jobs': [job], 'queue': {}, 'counts': {'completed': 2, 'queued': 1},
                        'pagination': {'page': 1, 'page_size': 10, 'total': 1, 'total_pages': 1, 'before': 123}})
                elif path.endswith('/video'):
                    media_requests.append(path)
                    await handler.fulfill(body=clip.read_bytes(), content_type='video/mp4')
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                else:
                    await handler.fulfill(json={'models': [], 'prompts': []})
            await page.route('**/*', route)
            try:
                await page.goto('https://grouped.test/')
                await page.evaluate("async layout => (await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})", layout)
                await page.get_by_text('H3 Seed Hunter is ready.', exact=True).wait_for()
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=review]').click()
                parent = page.locator('[data-job-id=source]')
                assert await page.locator('.h3-video-job').count() == 1
                assert await parent.locator('.h3-seed-hunter-candidate').count() == 3
                assert await parent.locator('.h3-video-refinement').count() == 2
                assert grouped and all(value == ['true'] for value in grouped)
                assert not writes and not media_requests
                first = parent.locator('.h3-seed-hunter-candidate').nth(0)
                third = parent.locator('.h3-seed-hunter-candidate').nth(2)
                assert await first.locator('[data-refinement-id=final-one]').count() == 1
                assert await third.locator('[data-refinement-id=final-three]').count() == 1
                await first.get_by_role('button', name='Play refined video', exact=True).click()
                video = first.locator('[data-refinement-id=final-one] video')
                await page.wait_for_function("()=>document.querySelector('[data-refinement-id=final-one] video').currentTime > 0")
                await video.evaluate('(node)=>{window.originalPlayer=node}')
                refinements[1].update(status='failed', error='Synthetic render failure')
                await page.get_by_role('button', name='Refresh jobs', exact=True).click()
                await third.get_by_text('Synthetic render failure', exact=True).wait_for()
                assert await video.evaluate('(node)=>node===window.originalPlayer && !node.paused')
                assert await parent.get_by_role('button', name='Use this preview', exact=True).count() == 3
                await third.get_by_role('button', name='Delete refinement', exact=True).click()
                await third.get_by_role('button', name='Delete permanently', exact=True).click()
                await page.wait_for_function("()=>document.querySelectorAll('.h3-video-refinement').length===1")
                assert await parent.locator('.h3-seed-hunter-candidate').count() == 3
                assert await video.evaluate('(node)=>node===window.originalPlayer && !node.paused')
                assert writes == ['/api/video/h3/jobs/final-three/record']
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(exercise())
