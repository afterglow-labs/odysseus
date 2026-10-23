"""Real queue forms expose and persist prompt edits, including refinements.

Browser plugin unavailable: use the existing Playwright/Chrome installation.
All routes and media are fixtures; no production jobs are submitted.
"""
import asyncio
from copy import deepcopy
from email.parser import BytesParser
from email.policy import default
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('layout', ['classic', 'pipeline'])
def test_queued_refinement_and_batch_prompt_can_be_edited_and_reopened(tmp_path, layout):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    config = dict(recipe='seed_hunter', prompt='A ball bounces.', mode='ref2va',
                  model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                  latent_upscaler='latent_upscaler', gpu='GPU-5090', vae_gpu='', width=960, height=544,
                  frames=73, steps=6, seed=42, sampler='er_sde', scheduler='beta', shift_video=12,
                  shift_audio=3, reference_size='max', loras=[], seed_hunter={'stage': 'final', 'preview_count': 1})
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler')]
    inventory = dict(runtime_ready=True, components=components, gpus=[{'id': 'GPU-5090', 'name': 'RTX 5090'}],
                     seed_hunter={'defaults': config, 'features': {'latent_upscaler': True,
                         'refinement_prompt_edit': True, 'separate_final_length': True}})
    queue = dict(paused=True, queued=2, total_queued=2, running=0, total_running=0)
    jobs = [dict(id='final-job', revision=0, recipe='seed_hunter', status='queued', prompt=config['prompt'],
                 seed_hunter={'stage': 'final', 'candidates': []}, config=deepcopy(config)),
            dict(id='preview-job', revision=0, recipe='seed_hunter', status='queued', prompt='Other original prompt',
                 seed_hunter={'stage': 'preview', 'candidates': []}, config={**deepcopy(config),
                    'prompt': 'Other original prompt', 'seed_hunter': {'stage': 'preview', 'preview_count': 3}})]
    inputs = {'reference_videos': [{'index': 0, 'name': 'reference.mp4', 'size': 100}]}
    patches, errors = [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.set_default_timeout(7000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                path = urlparse(request.url).path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><title>Queue prompt QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path.endswith('/gpus'):
                    await handler.fulfill(json={'gpus': inventory['gpus']})
                elif path.endswith('/prompt-history'):
                    await handler.fulfill(json={'prompts': []})
                elif path.endswith('/jobs/final-job/edit'):
                    await handler.fulfill(json={**jobs[0], 'inputs': inputs})
                elif path.endswith('/jobs/final-job') and request.method == 'PATCH':
                    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.post_data_buffer)
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True) for part in message.iter_parts()}
                    patches.append(fields)
                    jobs[0]['config'] = json.loads(fields['config'])
                    jobs[0]['prompt'] = jobs[0]['config']['prompt']
                    jobs[0]['revision'] += 1
                    await handler.fulfill(json=jobs[0])
                elif path.endswith('/queue/jobs'):
                    await handler.fulfill(json={'jobs': jobs, 'queue': queue})
                elif path.endswith('/queue/edit'):
                    body = request.post_data_json
                    assert body['patch'] == {'prompt': 'A green ball in both jobs.'}
                    assert body['jobs'] == [{'id': 'final-job', 'revision': 1}, {'id': 'preview-job', 'revision': 0}]
                    for job in jobs:
                        job['config'].update(body['patch'])
                        job['prompt'] = job['config']['prompt']
                        job['revision'] += 1
                    await handler.fulfill(json={'succeeded': 2, 'queue': queue})
                elif path.endswith('/jobs'):
                    await handler.fulfill(json={'jobs': jobs, 'queue': queue, 'pagination': {'page': 1, 'total': 2, 'total_pages': 1}})
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('https://hunter-prompt.test/')
                await page.evaluate("async layout=>(await import('/static/js/h3SeedHunter.js')).showH3SeedHunter({layout})", layout)
                await page.get_by_text('H3 Seed Hunter is ready.', exact=True).wait_for()
                assert page.url == 'https://hunter-prompt.test/' and await page.title() == 'Queue prompt QA'
                panel = page.get_by_role('dialog', name='H3 Seed Hunter', exact=True)
                async def open_edit():
                    if layout == 'pipeline':
                        await panel.locator('[data-pipeline-stage=review]').click()
                    await panel.get_by_role('button', name='Edit queued job', exact=True).first.click()
                await open_edit()
                prompt = page.locator('#seed-hunter-prompt')
                await pw.expect(prompt).to_be_enabled()
                await pw.expect(prompt).to_be_visible()
                assert await prompt.input_value() == 'A ball bounces.'
                await prompt.fill('A purple ball bounces.')
                await panel.get_by_role('button', name='Save queued job', exact=True).click()
                await page.get_by_text('Changes saved to the queued job.', exact=False).wait_for()
                assert json.loads(patches[0]['retain_inputs'])['reference_videos'] == [0]
                assert json.loads(patches[0]['config'])['prompt'] == 'A purple ball bounces.'
                await open_edit()
                await pw.expect(prompt).to_have_value('A purple ball bounces.')
                await prompt.scroll_into_view_if_needed()
                await page.screenshot(path=str(tmp_path / f'{layout}-refinement-prompt.png'))
                await panel.get_by_role('button', name='Cancel edit', exact=True).click()
                await panel.get_by_role('button', name='Edit queued parameters', exact=True).click()
                editor = panel.locator('.video-queue-editor')
                await editor.get_by_role('checkbox', name='Change Prompt', exact=True).check()
                await editor.get_by_role('textbox', name='New Prompt', exact=True).fill('A green ball in both jobs.')
                await editor.locator('.video-queue-approval input').check()
                await editor.locator('[data-queue-action="apply"]').click()
                await pw.expect(editor).to_be_hidden()
                assert [job['prompt'] for job in jobs] == ['A green ball in both jobs.'] * 2
                await open_edit()
                await pw.expect(prompt).to_have_value('A green ball in both jobs.')
                assert not errors
            finally:
                (tmp_path / 'page.html').write_text(await page.content())
                (tmp_path / 'errors.json').write_text(json.dumps(errors))
                await browser.close()

    asyncio.run(exercise())
