"""Both ends of classic and Pipeline job results navigate the same bounded page."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
import subprocess
from urllib.parse import parse_qs, urlparse

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_top_and_bottom_job_pages_in_all_classic_and_pipeline_workflows(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    ffmpeg = pytest.importorskip('imageio_ffmpeg').get_ffmpeg_exe()
    clip = tmp_path / 'clip.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=purple:s=160x90:d=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(clip)], check=True)
    video = clip.read_bytes()
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ['model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler']]
    inventory = {'runtime_ready': True, 'components': components, 'gpus': [
        {'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}], 'seed_hunter': {
            'features': {'seed_scout': True, 'guides': True, 'force_audio': True}, 'defaults': {}},
        'workflows': [{'id': 'h3', 'label': 'H3 head swap', 'family': 'h3', 'ready': True,
                       'slots': [], 'controls': [], 'defaults': {}}]}
    errors, writes, media_requests, job_requests = [], [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1365, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)

            async def route(handler):
                req = handler.request
                url = urlparse(req.url)
                path, query = url.path, parse_qs(url.query)
                if req.method != 'GET':
                    writes.append((req.method, path))
                    await handler.fulfill(status=405, json={'detail': 'Read-only fixture'})
                elif path == '/':
                    await handler.fulfill(content_type='text/html', body='''<html><head>
                      <meta charset="utf-8"><title>Job pagination QA</title>
                      <meta name="viewport" content="width=device-width,initial-scale=1">
                      <link rel="stylesheet" href="/static/style.css"></head><body></body></html>''')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript'
                                         if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                elif path.endswith('/jobs'):
                    job_requests.append(query)
                    number = int(query.get('page', ['1'])[0])
                    hunter = query.get('recipe') == ['seed_hunter']
                    scout = query.get('stage') == ['scout']
                    jobs = []
                    for index in range((number - 1) * 10, min(number * 10, 23)):
                        job = {'id': f'job-{index}', 'status': 'completed', 'prompt': 'Fixture prompt'}
                        if hunter:
                            job.update(recipe='seed_hunter', seed_hunter={'stage': 'scout' if scout else 'single'})
                        jobs.append(job)
                    await handler.fulfill(json={'jobs': jobs, 'queue': {'paused': False},
                        'pagination': {'page': number, 'total': 23, 'total_pages': 3, 'before': 12345},
                        'counts': {'completed': 23}})
                elif path.endswith('/video'):
                    media_requests.append(path)
                    await handler.fulfill(body=video, content_type='video/mp4')
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('http://job-pages.test/')
                assert await page.title() == 'Job pagination QA'
                assert page.url == 'http://job-pages.test/'
                workflows = [('h3Video.js', 'showH3Video'), ('h3SeedHunter.js', 'showH3SeedHunter'),
                             ('h3SeedHunter.js', 'showH3SeedScout'), ('bfsVideo.js', 'showBfsVideo')]
                for layout in ['classic', 'pipeline']:
                    for module, entry in workflows:
                        await page.set_viewport_size({'width': 1365, 'height': 1000})
                        media_start = len(media_requests)
                        await page.evaluate('''async ({module,entry,layout}) => {
                          window.workflow = (await import('/static/js/' + module))[entry]({layout});
                        }''', {'module': module, 'entry': entry, 'layout': layout})
                        dialog = page.get_by_role('dialog')
                        if layout == 'pipeline':
                            await dialog.locator('[data-pipeline-stage=review]').click()
                        top = dialog.get_by_role('navigation', name='Job history pages', exact=True)
                        bottom = dialog.get_by_role('navigation', name='Job history pages below results', exact=True)
                        await pw.expect(top).to_contain_text('Page 1 of 3')
                        await pw.expect(bottom).to_contain_text('Page 1 of 3')
                        assert await bottom.evaluate('n=>n.previousElementSibling.matches(".h3-video-jobs")')
                        assert await top.evaluate('n=>n.nextElementSibling.matches(".h3-video-jobs")')
                        assert len(media_requests) == media_start
                        await bottom.scroll_into_view_if_needed()
                        assert await bottom.is_visible()
                        await bottom.get_by_role('button', name='Next page of jobs').click()
                        await pw.expect(top).to_contain_text('Page 2 of 3')
                        await pw.expect(bottom).to_contain_text('Page 2 of 3')
                        assert job_requests[-1]['page'] == ['2'] and job_requests[-1]['page_size'] == ['10']
                        assert job_requests[-1]['before'] == ['12345']
                        assert await dialog.locator('.h3-video-jobs > *').count() == 10
                        assert len(media_requests) == media_start
                        if entry == 'showH3Video':
                            await dialog.get_by_role('button', name='Play video', exact=True).first.click()
                            await page.wait_for_function('()=>document.querySelector(".h3-video-jobs video[src]")?.readyState>=2')
                            await page.evaluate('window.playing=document.querySelector(".h3-video-jobs video[src]")')
                            await dialog.get_by_role('button', name='Refresh jobs', exact=True).click()
                            await pw.expect(top).to_contain_text('Page 1 of 3')
                            # Explicit refresh resets to the newest page; subsequent polling must
                            # preserve a mounted player on that page rather than reload every card.
                            await dialog.get_by_role('button', name='Play video', exact=True).first.click()
                            await page.wait_for_function('()=>document.querySelector(".h3-video-jobs video[src]")?.readyState>=2')
                            await page.evaluate('window.playing=document.querySelector(".h3-video-jobs video[src]")')
                            await dialog.get_by_role('button', name='Refresh jobs', exact=True).click()
                            await pw.expect(top).to_have_attribute('aria-busy', 'false')
                            assert await page.evaluate('window.playing===document.querySelector(".h3-video-jobs video[src]")')
                            requested_media = len(media_requests)
                            await page.wait_for_event('response', predicate=lambda response:
                                urlparse(response.url).path == '/api/video/h3/jobs', timeout=5000)
                            await pw.expect(top).to_have_attribute('aria-busy', 'false')
                            assert await page.evaluate('window.playing===document.querySelector(".h3-video-jobs video[src]")')
                            assert len(media_requests) == requested_media
                            await bottom.scroll_into_view_if_needed()
                            await page.screenshot(path=str(tmp_path / f'job-pages-{layout}-desktop.png'))
                        else:
                            await top.get_by_role('button', name='Previous page of jobs').click()
                            await pw.expect(bottom).to_contain_text('Page 1 of 3')
                        await page.set_viewport_size({'width': 390, 'height': 844})
                        await bottom.scroll_into_view_if_needed()
                        assert await bottom.evaluate('n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth}')
                        await bottom.get_by_role('button', name='Next page of jobs').click()
                        await pw.expect(top).to_contain_text('Page 2 of 3')
                        await pw.expect(bottom).to_contain_text('Page 2 of 3')
                        await bottom.scroll_into_view_if_needed()
                        if entry == 'showH3SeedScout':
                            await page.screenshot(path=str(tmp_path / f'job-pages-{layout}-mobile.png'))
                        await dialog.get_by_role('button', name='Close', exact=True).click()
                assert not errors, errors
                assert not writes, writes
            finally:
                await browser.close()
    asyncio.run(exercise())
