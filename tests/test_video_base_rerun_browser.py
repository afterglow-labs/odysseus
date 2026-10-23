"""Browser plugin not available; isolated Playwright routes exercise real forms."""
import asyncio
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout,width', [('classic', 1440), ('pipeline', 1440), ('classic', 430)])
def test_shared_base_form_reprocesses_multiple_cards_without_editor_or_new_uploads(tmp_path, layout, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    config = dict(recipe='seed_hunter', prompt='Saved original prompt', mode='ref2va',
                  model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                  latent_upscaler='latent_upscaler', gpu='GPU-5090', vae_gpu='', width=960, height=544,
                  frames=107, steps=20, seed=42, sampler='er_sde', scheduler='beta', shift_video=12,
                  shift_audio=3, reference_size='max', loras=[], seed_hunter={'stage': 'preview', 'preview_count': 3})
    inventory = dict(runtime_ready=True, components=[{'id': r, 'role': r, 'name': r, 'variant': 'ref2va'}
                     for r in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')],
                     gpus=[{'id': 'GPU-5090', 'name': 'RTX 5090'}],
                     seed_hunter={'defaults': config, 'features': {'latent_upscaler': True, 'separate_final_length': True}})
    jobs = [dict(id=c * 32, status='completed', revision=0, recipe='seed_hunter', prompt='Saved original prompt',
                 seed_hunter={'stage': 'preview', 'candidates': []}) for c in ('a', 'b')]
    queue = dict(paused=True, queued=0, total_queued=0, running=0, total_running=0)
    requests, errors = [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route(handler):
                path = urlparse(handler.request.url).path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><title>Base reprocess QA</title><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path.endswith('/prompt-history'):
                    await handler.fulfill(json={'prompts': []})
                elif path.endswith('/queue/rerun'):
                    body = handler.request.post_data_json
                    requests.append(body)
                    queue['queued'] += 1
                    await handler.fulfill(json={'jobs': [{'id': str(len(requests)) * 32, 'source_id': body['jobs'][0]['id'], 'status': 'queued'}],
                                                'succeeded': 1, 'failed': 0, 'request_id': body['request_id'], 'queue': queue})
                elif path.endswith('/jobs'):
                    await handler.fulfill(json={'jobs': jobs, 'queue': queue, 'pagination': {'page': 1, 'total': 2, 'total_pages': 1}})
                else:
                    await handler.fulfill(status=404, json={'detail': 'Not a fixture route'})
            await page.route('**/*', route)
            try:
                await page.goto('https://base-rerun.test/')
                await page.evaluate("async layout=>(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})", layout)
                await page.get_by_text('H3 Seed Hunter is ready.', exact=True).wait_for()
                assert await page.title() == 'Base reprocess QA' and page.url == 'https://base-rerun.test/'
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=prepare]').click()
                await page.locator('#seed-hunter-prompt').fill('Updated shared base prompt')
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=review]').click()
                cards = [page.locator(f'article[data-job-id="{job["id"]}"]') for job in jobs]
                for index, card in enumerate(cards):
                    await card.get_by_role('button', name='Reprocess with base settings', exact=True).click()
                    await card.get_by_text('and the base settings above', exact=False).wait_for()
                    assert len(requests) == index + 1
                    assert requests[-1]['jobs'][0]['id'] == jobs[index]['id']
                    assert requests[-1]['base_config']['prompt'] == 'Updated shared base prompt'
                    assert requests[-1]['base_config']['frames'] == 107
                    assert requests[-1]['base_config']['seed_hunter']['preview_count'] == 3
                    assert 'patch' not in requests[-1]
                assert requests[0]['request_id'] != requests[1]['request_id']
                await cards[0].get_by_role('button', name='Reprocess with original settings', exact=True).click()
                await cards[0].get_by_text('saved inputs, prompt and settings', exact=False).wait_for()
                assert requests[-1]['patch'] == {} and 'base_config' not in requests[-1]
                assert await page.locator('.video-queue-editor:visible').count() == 0
                assert await page.locator('#seed-hunter-prompt').input_value() == 'Updated shared base prompt'
                await cards[0].scroll_into_view_if_needed()
                await page.screenshot(path=str(tmp_path / f'{layout}-{width}-base-actions.png'))
                assert not errors
            finally:
                (tmp_path / 'page.html').write_text(await page.content())
                (tmp_path / 'requests.json').write_text(json.dumps(requests))
                await browser.close()
    asyncio.run(exercise())
