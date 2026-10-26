"""Collapsed batch masters, in-place Retry, filters and fixed activity UI."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_batch_master_retry_and_active_bar_in_all_workflows(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')

    async def run():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
            batch = 'b' * 32; rows = []; calls = []; stage = 'preview'
            inventory = dict(runtime_ready=True, components=[dict(id=role, role=role, name=role, variant='ref2va') for role in
                ['model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler']], gpus=[dict(id='GPU-5090', name='RTX 5090', nvfp4=True)],
                seed_hunter=dict(features=dict(seed_scout=True), defaults={}), workflows=[dict(id='h3', label='BFS', family='h3', slots=[], controls=[], defaults={})])

            async def route(handler):
                url = urlparse(handler.request.url); path = url.path; query = parse_qs(url.query)
                q = dict(paused=False, total_running=1, total_queued=sum(r['status']=='queued' for r in rows),
                         current_jobs=[dict(id='c'*32, source_name='Current render.mp4', phase='sampling', step=3, total_steps=20)])
                master = dict(id='batch-'+batch, batch=dict(id=batch, name='Submitted batch', count=2), batch_count=2,
                              status='queued' if any(r['status']=='queued' for r in rows) else 'failed',
                              counts={s:sum(r['status']==s for r in rows) for s in ['queued','failed']})
                if path == '/':
                    return await handler.fulfill(content_type='text/html', body='<html><head><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                if path.startswith('/static/'):
                    f = ROOT / path.lstrip('/'); return await handler.fulfill(body=f.read_bytes(), content_type='text/javascript' if f.suffix=='.js' else mimetypes.guess_type(f.name)[0])
                if path.endswith('/inventory'): return await handler.fulfill(json=inventory)
                if path.endswith('/gpus'): return await handler.fulfill(json={'gpus': inventory['gpus']})
                if path.endswith('/prompt-enhancer'): return await handler.fulfill(json={'models':[]})
                if path == '/api/workflows/prompt-history': return await handler.fulfill(json={'prompts':[], 'has_more':False})
                if path.endswith('/jobs'):
                    calls.append(query)
                    visible = [master] if query.get('active_only')!=['true'] or master['status']=='queued' else []
                    return await handler.fulfill(json=dict(jobs=visible, queue=q, counts=master['counts'], pagination=dict(page=1,total=len(visible),total_pages=1,before=100)))
                if '/batches/' in path:
                    if path.endswith('/actions'):
                        assert handler.request.post_data_json['action'] == 'rerun_all'
                        changed = sum(r['status'] not in {'queued', 'running'} for r in rows)
                        for row in rows: row.update(status='queued', error=None)
                        return await handler.fulfill(json=dict(succeeded=len(rows), failed=0, queued_again=changed, skipped_active=len(rows)-changed))
                    visible = rows if query.get('active_only')!=['true'] else [r for r in rows if r['status']=='queued']
                    return await handler.fulfill(json=dict(batch=master,jobs=visible,queue=q,pagination=dict(page=1,total=len(visible),total_pages=1)))
                if path.endswith('/queue/rerun'):
                    data=handler.request.post_data_json; source=data['jobs'][0]['id']
                    row=next(r for r in rows if r['id']==source); row.update(status='queued',error=None,revision=1)
                    return await handler.fulfill(json=dict(succeeded=1,failed=0,request_id=data['request_id'],queue=q,
                        jobs=[dict(id=source,source_id=source,retried=True,status='queued')]))
                return await handler.fulfill(status=404,json={'detail':path})

            await page.route('**/*', route)
            await page.goto('https://batch-history.test/')
            for module, entry, stage in [('h3Video','showH3Video','single'), ('h3SeedHunter','showH3SeedHunter','preview'),
                                         ('h3SeedHunter','showH3SeedScout','scout'), ('bfsVideo','showBfsVideo','single')]:
                rows[:] = [dict(id=f'{i:032x}', status='failed', revision=0, error='Synthetic failure', source_name=f'video-{i}.mp4',
                               recipe='seed_hunter', seed_hunter=dict(stage=stage,candidates=[])) for i in [1,2]]
                await page.evaluate('localStorage.clear()')
                await page.evaluate('async ([m,e]) => { window.workflow = (await import(`/static/js/${m}.js`))[e](); }', [module,entry])
                dialog = page.get_by_role('dialog'); master = dialog.locator('.video-batch-master')
                await pw.expect(master).to_have_count(1)
                await pw.expect(dialog.locator('.h3-video-jobs > *')).to_have_count(1)
                await pw.expect(dialog.locator('[data-job-id]')).to_have_count(0)
                await master.locator('.video-batch-heading').click()
                await pw.expect(master.locator('[data-job-id]')).to_have_count(2)
                first=master.locator('[data-job-id]').first
                await first.get_by_role('button', name='Retry', exact=True).click()
                await pw.expect(first).to_contain_text('queued', ignore_case=True)
                await pw.expect(master.locator('[data-job-id]')).to_have_count(2)
                active=dialog.get_by_role('checkbox',name='Active only',exact=True)
                await active.check()
                await pw.expect(master.locator('[data-job-id]')).to_have_count(1)
                bar=dialog.locator('.video-queue-activity')
                await pw.expect(bar).to_contain_text('Step 3/20')
                top=(await bar.bounding_box())['y']
                await dialog.locator('.h3-video-scroll').evaluate('n => n.scrollTop = n.scrollHeight')
                assert (await bar.bounding_box())['y']==top
                # Polling preserves the mounted card, its place and the filter.
                await page.evaluate('window.card=document.querySelector("[data-job-id]")')
                await page.wait_for_timeout(3200)
                assert await page.evaluate('window.card===document.querySelector("[data-job-id]")')
                assert calls[-1]['active_only']==['true']
                await active.uncheck(); await pw.expect(master.locator('[data-job-id]')).to_have_count(2)
                bar_height = (await bar.bounding_box())['height']
                await master.get_by_role('button', name='Rerun all', exact=True).click()
                assert (await bar.bounding_box())['height'] == bar_height
                await pw.expect(master.locator('.video-batch-notice')).to_have_text('1 queued again in this batch · 1 already queued or running')
                await pw.expect(master.locator('.video-batch-notice')).to_be_visible()
                await pw.expect(master.locator('[data-job-id]')).to_have_count(2)
                for child in await master.locator('[data-job-id]').all():
                    await pw.expect(child).to_contain_text('queued', ignore_case=True)
                await master.get_by_role('button', name='Rerun all', exact=True).press('Enter')
                await pw.expect(master.locator('.video-batch-notice')).to_have_text('0 queued again in this batch · 2 already queued or running')
                await pw.expect(dialog.locator('.video-batch-master')).to_have_count(1)
                await page.screenshot(path=str(tmp_path/f'{module}-{stage}-batch.png'))
                await dialog.get_by_role('button',name='Close',exact=True).click()
            assert not errors, errors
            await browser.close()
    asyncio.run(run())
