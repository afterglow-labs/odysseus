"""Destructive history actions are explicit, scoped, visible and usable in every layout."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_history_and_queued_delete_in_all_classic_and_pipeline_workflows(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ['model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler']]
    inventory = {'runtime_ready': True, 'components': components,
                 'gpus': [{'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}],
                 'seed_hunter': {'features': {'seed_scout': True}, 'defaults': {}},
                 'workflows': [{'id': 'h3', 'label': 'H3 head swap', 'family': 'h3', 'ready': True,
                                'slots': [], 'controls': [], 'defaults': {}}]}
    calls, errors, state = [], [], {}
    release_chunk = asyncio.Event()
    release_queue = asyncio.Event()

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1365, 'height': 1000})
            page.set_default_timeout(7000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                req = handler.request
                url = urlparse(req.url)
                path, query = url.path, parse_qs(url.query)
                if req.method == 'POST':
                    body = req.post_data_json
                    calls.append((path, body))
                    if path.endswith('/history/delete-preview'):
                        await handler.fulfill(json={'snapshot_id': 'fixture-snapshot', 'count': 23,
                            'counts': {'completed': 20, 'failed': 2, 'stopped': 1}, 'preserved_active': 3})
                    elif path.endswith('/history/delete'):
                        state['chunks'] += 1
                        if state['chunks'] == 1:
                            await handler.fulfill(json={'snapshot_id': 'fixture-snapshot', 'done': False,
                                'deleted': 10, 'processed': 10, 'pending': 13, 'total': 23, 'skipped': 0, 'errors': [], 'preserved_active': 3})
                        else:
                            await release_chunk.wait()
                            state['finished'] = 0
                            await handler.fulfill(json={'snapshot_id': 'fixture-snapshot', 'done': True,
                                'deleted': 23, 'processed': 23, 'pending': 0, 'total': 23,
                                'errors': [], 'skipped': 0, 'preserved_active': 3})
                    elif path.endswith('/queue/delete'):
                        await release_queue.wait()
                        state['queued'] = 0
                        await handler.fulfill(json={'deleted': 2, 'succeeded': 2, 'queue': queue()})
                    else:
                        await handler.fulfill(status=405, json={'detail': 'Unexpected fixture mutation'})
                    return
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='''<html><head>
                      <meta charset="utf-8"><title>History deletion QA</title>
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
                    number = int(query.get('page', ['1'])[0])
                    total = state['finished'] + state['queued'] + 1
                    number = min(number, max(1, (total + 9) // 10))
                    jobs = []
                    for index in range((number - 1) * 10, min(number * 10, total)):
                        status = 'completed' if index < state['finished'] else 'queued' if index < total - 1 else 'running'
                        job = {'id': f'job-{index}', 'status': status, 'prompt': 'Neutral fixture prompt'}
                        if query.get('recipe') == ['seed_hunter']:
                            job.update(recipe='seed_hunter', seed_hunter={'stage': 'scout' if query.get('stage') == ['scout'] else 'single'})
                        jobs.append(job)
                    await handler.fulfill(json={'jobs': jobs, 'queue': queue(),
                        'pagination': {'page': number, 'total': total, 'total_pages': max(1, (total + 9) // 10), 'before': 12345},
                        'counts': {'completed': state['finished'], 'queued': state['queued'], 'running': 1}})
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            def queue():
                return {'paused': False, 'queued': state['queued'], 'running': 1,
                        'total_queued': state['queued'] + 3, 'total_running': 1}

            await page.route('**/*', route)
            try:
                await page.goto('http://history-delete.test/')
                # Model the application's capture-phase Escape arbiter. The confirmation
                # must consume Escape before this can dismiss the underlying draft.
                await page.evaluate('''() => { window.escapedParent=0; document.addEventListener('keydown',e=>{
                  if(e.key==='Escape'&&!e.defaultPrevented)window.escapedParent++;
                },true); }''')
                workflows = [('h3Video.js', 'showH3Video', {}),
                             ('h3SeedHunter.js', 'showH3SeedHunter', {'recipe': 'seed_hunter', 'stage': 'not_scout'}),
                             ('h3SeedHunter.js', 'showH3SeedScout', {'recipe': 'seed_hunter', 'stage': 'scout'}),
                             ('bfsVideo.js', 'showBfsVideo', {})]
                for layout in ['classic', 'pipeline']:
                    for module, entry, filters in workflows:
                        state.update(finished=23, queued=2, chunks=0)
                        release_chunk.clear()
                        release_queue.clear()
                        calls.clear()
                        await page.set_viewport_size({'width': 390 if layout == 'pipeline' else 1365,
                                                      'height': 844 if layout == 'pipeline' else 1000})
                        await page.evaluate('''async ({module,entry,layout}) => {
                          window.workflow = (await import('/static/js/' + module))[entry]({layout});
                        }''', {'module': module, 'entry': entry, 'layout': layout})
                        dialog = page.locator('.h3-video-dialog')
                        if layout == 'pipeline':
                            await dialog.locator('[data-pipeline-stage=review]').click()
                        top = dialog.get_by_role('navigation', name='Job history pages', exact=True)
                        await pw.expect(top).to_contain_text('Page 1 of 3')
                        history = dialog.locator('.video-history-actions')
                        await history.get_by_role('button', name='Delete all job history', exact=True).click()
                        confirmation = page.get_by_role('dialog', name='Delete all job history?', exact=True)
                        await pw.expect(confirmation).to_contain_text('23 finished jobs')
                        await pw.expect(confirmation).to_contain_text('20 completed, 2 failed, 1 stopped')
                        await pw.expect(confirmation).to_contain_text('3 queued or running jobs are kept')
                        assert calls[-1][1] == {'scope': 'workflow', **filters}
                        assert not any(path.endswith('/history/delete') for path, _ in calls)
                        await page.keyboard.press('Escape')
                        await pw.expect(confirmation).to_be_hidden()
                        assert await page.evaluate('window.escapedParent') == 0
                        assert await dialog.is_visible()
                        await history.get_by_role('button', name='Delete all job history', exact=True).click()
                        await pw.expect(confirmation).to_be_visible()
                        if entry == 'showH3SeedHunter':
                            await page.screenshot(path=str(tmp_path / f'history-delete-{layout}.png'))
                        assert await confirmation.evaluate('n=>{const r=n.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight}')
                        await confirmation.get_by_role('button', name='Delete 23 finished jobs permanently', exact=True).click()
                        await pw.expect(confirmation).to_contain_text('10 / 23 checked')
                        await pw.expect(confirmation.get_by_role('button', name='Deleting job history…', exact=True)).to_be_disabled()
                        await page.keyboard.press('Escape')
                        assert await confirmation.is_visible()
                        release_chunk.set()
                        await pw.expect(confirmation).to_be_hidden()
                        await pw.expect(history).to_contain_text('23 finished jobs deleted')
                        await pw.expect(top).to_contain_text('Page 1 of 1')
                        assert state['queued'] == 2
                        assert [body for path, body in calls if path.endswith('/history/delete')] == [
                            {'snapshot_id': 'fixture-snapshot', 'confirm': True}] * 2
                        assert await dialog.locator('.h3-video-jobs > *').count() == 3
                        # The old queued action now opens an unmistakable modal and
                        # visibly reports the pending request and actual deletion result.
                        queued = dialog.locator('.video-queue-panel')
                        await queued.get_by_role('button', name='Delete queued jobs', exact=True).click()
                        queued_confirmation = page.get_by_role('dialog', name='Delete queued jobs?', exact=True)
                        await pw.expect(queued_confirmation).to_contain_text('currently 2')
                        assert not any(path.endswith('/queue/delete') for path, _ in calls)
                        await queued_confirmation.get_by_role('button', name='Delete queued jobs permanently', exact=True).click()
                        await pw.expect(queued_confirmation.get_by_role('button', name='Deleting queued jobs…', exact=True)).to_be_disabled()
                        release_queue.set()
                        await pw.expect(queued_confirmation).to_be_hidden()
                        await pw.expect(queued).to_contain_text('2 jobs deleted')
                        assert calls[-1][1] == {'scope': 'family'}
                        await pw.expect(dialog.locator('.h3-video-jobs > *')).to_have_count(1)
                        await dialog.get_by_role('button', name='Close', exact=True).click()
                        assert await page.locator('.video-queue-confirmation').count() == 0
                assert not errors, errors
            finally:
                release_queue.set()
                release_chunk.set()
                await browser.close()
    asyncio.run(exercise())
