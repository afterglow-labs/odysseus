"""Original inputs play separately from generated previews, only when requested."""
import asyncio
import base64
import mimetypes
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('workflow,layout', [('hunter', 'classic'), ('hunter', 'pipeline'), ('scout', 'classic'), ('scout', 'pipeline')])
def test_original_video_players_are_lazy_stable_and_job_scoped(tmp_path, workflow, layout):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required; Browser plugin unavailable')
    ffmpeg = pytest.importorskip('imageio_ffmpeg').get_ffmpeg_exe()
    video = tmp_path / 'neutral-video.mp4'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=c=purple:s=160x90:d=2', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(video)], check=True)
    video_bytes = video.read_bytes()
    image = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')
    stage = 'scout' if workflow == 'scout' else 'preview'
    inputs = [{'id': f'{field}:{index}', 'field': field, 'index': index, 'name': name,
               'url': f'/api/video/h3/jobs/job-1/input-videos/{field}/{index}/preview?revision=1',
               'playback': 'selected_section' if section else 'original', 'start_seconds': 1 if section else 0,
               'duration_seconds': 2 if section else None, 'revision': 1}
              for field, index, name, section in [('source_video', 0, 'original-guide.mp4', False),
                 ('reference_videos', 0, 'first-reference-with-a-long-original-filename.mkv', False),
                 ('reference_videos', 1, 'second-reference.mov', True)]]
    main = {'id': 'job-1', 'status': 'completed', 'recipe': 'seed_hunter', 'prompt': 'Neutral fixture prompt.',
            'seed_hunter': {'stage': stage}, 'input_videos': inputs}
    if workflow == 'hunter':
        main['seed_hunter']['candidates'] = [{'index': n, 'seed': n * 1000, 'can_refine': True} for n in (1, 2, 3)]
    else:
        main['scout'] = {'status': 'completed', 'scout_step': 3, 'total_steps': 20, 'seeds': [1, 2, 3],
                        'previews': [{'seed': n, 'url': f'/api/video/h3/jobs/job-1/scout/previews/{n}'} for n in (1, 2, 3)]}
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')]
    inventory = {'runtime_ready': True, 'components': components, 'gpus': [{'id': 'GPU-5090', 'name': 'RTX 5090'}],
                 'seed_hunter': {'features': {'seed_scout': True}, 'defaults': {}}}
    media_requests, writes, errors = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.set_default_timeout(7000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                req = handler.request
                url = urlparse(req.url)
                path, query = url.path, parse_qs(url.query)
                if req.method not in ('GET', 'HEAD'):
                    writes.append(path)
                    await handler.fulfill(status=405, json={'detail': 'Read-only fixture'})
                elif path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><title>Original video QA</title></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path == '/api/video/h3/jobs':
                    number = int(query.get('page', ['1'])[0])
                    jobs = [main, {'id': 'image-only', 'status': 'queued', 'seed_hunter': {'stage': stage}, 'input_videos': []},
                            {'id': 'legacy-job', 'status': 'queued', 'seed_hunter': {'stage': stage}}] if number == 1 else [
                                {'id': 'other-page', 'status': 'queued', 'seed_hunter': {'stage': stage}}]
                    await handler.fulfill(json={'jobs': jobs, 'pagination': {'page': number, 'total': 11, 'total_pages': 2, 'before': 12345}, 'counts': {'completed': 1}, 'queue': {'paused': False}})
                elif '/input-videos/' in path or path.endswith('/video'):
                    media_requests.append(url.path + ('?' + url.query if url.query else ''))
                    await handler.fulfill(body=video_bytes, content_type='video/mp4')
                elif '/scout/previews/' in path:
                    await handler.fulfill(body=image, content_type='image/gif')
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('https://original-video.test/')
                await page.evaluate("async({workflow,layout})=>{const m=await import('/static/js/h3SeedHunter.js');m[workflow==='scout'?'showH3SeedScout':'showH3SeedHunter']({layout});}", {'workflow': workflow, 'layout': layout})
                panel = page.get_by_role('dialog', name='H3 Seed Scout' if workflow == 'scout' else 'H3 Seed Hunter', exact=True)
                await panel.get_by_text(('H3 Seed Scout' if workflow == 'scout' else 'H3 Seed Hunter') + ' is ready.', exact=True).wait_for()
                if layout == 'pipeline':
                    await panel.locator('[data-pipeline-stage=review]').click()
                card = panel.locator('[data-job-id="job-1"]')
                originals = card.locator('.h3-video-originals')
                await originals.wait_for()
                assert await originals.locator('.h3-video-original-name').all_text_contents() == [item['name'] for item in inputs]
                assert await originals.evaluate('(n)=>!!(n.compareDocumentPosition(n.parentElement.querySelector(".h3-seed-hunter-candidates"))&Node.DOCUMENT_POSITION_FOLLOWING)')
                assert 'Original source video' in await originals.inner_text()
                assert 'Reference <Video 1>' in await originals.inner_text()
                assert 'Reference <Video 2> · 0:01.00–0:03.00' in await originals.inner_text()
                assert await panel.locator('[data-job-id="image-only"] .h3-video-originals').is_hidden()
                assert await panel.locator('[data-job-id="legacy-job"] .h3-video-originals').is_hidden()
                assert await originals.locator('video[src]').count() == 0
                assert media_requests == []
                await originals.get_by_role('button', name='Play original video', exact=True).first.click()
                original = originals.locator('video').first
                await page.wait_for_function('()=>document.querySelector(".h3-video-original video[src]")?.readyState>=2')
                assert await original.get_attribute('src') == inputs[0]['url']
                await page.evaluate('window.originalPlayer=document.querySelector(".h3-video-original video[src]")')
                if workflow == 'hunter':
                    await card.get_by_role('button', name='Play preview 1', exact=True).click()
                    await page.wait_for_function('()=>document.querySelector(".h3-seed-hunter-candidate video[src]")?.readyState>=2')
                    assert '/seed-hunter/candidates/1/video' in await card.locator('.h3-seed-hunter-candidate video[src]').get_attribute('src')
                    assert await original.get_attribute('src') == inputs[0]['url']
                else:
                    assert await card.locator('.h3-scout-choice img').count() == 3
                await panel.get_by_role('button', name='Refresh jobs', exact=True).click()
                await pw.expect(panel.get_by_role('navigation', name='Job history pages', exact=True)).to_have_attribute('aria-busy', 'false')
                count = len(media_requests)
                await page.wait_for_event('response', predicate=lambda response: urlparse(response.url).path == '/api/video/h3/jobs', timeout=5000)
                await pw.expect(panel.get_by_role('navigation', name='Job history pages', exact=True)).to_have_attribute('aria-busy', 'false')
                assert await page.evaluate('window.originalPlayer===document.querySelector(".h3-video-original video[src]")')
                assert len(media_requests) == count
                await originals.scroll_into_view_if_needed()
                await page.screenshot(path=str(tmp_path / 'original-video-desktop.png'))
                await page.set_viewport_size({'width': 391, 'height': 844})
                await originals.locator('.h3-video-original').first.scroll_into_view_if_needed()
                assert await originals.evaluate('(n)=>n.getBoundingClientRect().right<=innerWidth&&n.scrollWidth<=n.clientWidth+2')
                await page.screenshot(path=str(tmp_path / 'original-video-mobile.png'))
                # A real attachment revision releases only the changed original.
                inputs[0]['revision'] = 2
                inputs[0]['url'] = inputs[0]['url'].replace('revision=1', 'revision=2')
                await panel.get_by_role('button', name='Refresh jobs', exact=True).click()
                await pw.expect(panel.get_by_role('navigation', name='Job history pages', exact=True)).to_have_attribute('aria-busy', 'false')
                assert await page.evaluate('!window.originalPlayer.getAttribute("src")&&window.originalPlayer.paused&&!window.originalPlayer.isConnected')
                assert await originals.locator('video[src]').count() == 0
                assert not any('revision=2' in url for url in media_requests)
                await originals.get_by_role('button', name='Play selected source section', exact=True).click()
                await page.wait_for_function('()=>document.querySelectorAll(".h3-video-original video")[2].readyState>=2')
                await page.evaluate('window.sectionPlayer=document.querySelectorAll(".h3-video-original video")[2]')
                bottom = panel.get_by_role('navigation', name='Job history pages below results', exact=True)
                await bottom.get_by_role('button', name='Next page of jobs').click()
                await pw.expect(bottom).to_contain_text('Page 2 of 2')
                assert await page.evaluate('!window.sectionPlayer.getAttribute("src")&&window.sectionPlayer.paused&&!window.sectionPlayer.isConnected')
                assert await panel.locator('.h3-video-original').count() == 0
                await bottom.get_by_role('button', name='Previous page of jobs').click()
                await pw.expect(bottom).to_contain_text('Page 1 of 2')
                await originals.get_by_role('button', name='Play original video', exact=True).first.click()
                await page.wait_for_function('()=>document.querySelector(".h3-video-original video[src]")?.readyState>=2')
                await page.evaluate('window.closePlayer=document.querySelector(".h3-video-original video[src]")')
                await panel.get_by_role('button', name='Close', exact=True).click()
                assert await page.evaluate('!window.closePlayer.getAttribute("src")&&window.closePlayer.paused&&!window.closePlayer.isConnected')
                assert not errors, errors
                assert not writes, writes
            finally:
                await browser.close()
    asyncio.run(exercise())
