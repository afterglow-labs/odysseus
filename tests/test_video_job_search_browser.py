"""Search drives visible history and exact bulk selection without live job writes."""
import asyncio
import math
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('scout,layout,width', [(False, 'classic', 1440), (True, 'pipeline', 430)])
def test_search_selects_matching_jobs_across_pages_and_preserves_pending_retry(tmp_path, scout, layout, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    config = dict(recipe='seed_hunter', prompt='Keep the harbor scenery.', mode='ref2va', model='model', encoder='encoder',
                  video_vae='video_vae', audio_vae='audio_vae', latent_upscaler='upscaler', gpu='GPU-5090', vae_gpu='',
                  width=480, height=864, frames=107, steps=20, seed=714, sampler='euler', scheduler='simple',
                  shift_video=12, shift_audio=3, reference_size='max', loras=[],
                  seed_hunter=dict(stage='scout' if scout else 'preview', preview_count=3, scout_count=3,
                                   scout_step=4, scout_mode='scout', final_frames=294))
    components = [dict(id=role, role=role, name=role, variant='ref2va')
                  for role in ['model', 'encoder', 'video_vae', 'audio_vae']]
    components.append(dict(id='upscaler', role='latent_upscaler', name='H3 learned upscaler'))
    inventory = dict(runtime_ready=True, components=components, gpus=[dict(id='GPU-5090', name='RTX 5090', nvfp4=True)],
                     seed_hunter=dict(defaults=config, scout_defaults=config, features={key: True for key in
                     ['auto_refine', 'latent_upscaler', 'scout_upscale', 'seed_scout', 'separate_final_length', 'rife']}))

    def job(index, source, status='completed', stage=None):
        result = dict(id=f'{index:032x}', status=status, recipe='seed_hunter', prompt='A calm landscape.',
                      source_name=source, seed_hunter={'stage': stage or ('scout' if scout else 'preview'), 'candidates': []})
        if scout:
            result['scout'] = {'mode': 'scout', 'seeds': [714, 715, 716], 'previews': []}
        return result

    harbor = [job(i, f'Harbor clip {i}.mkv') for i in range(60)]
    coast = [job(70 + i, f'Coast clip {i}.mkv') for i in range(2)]
    rows = harbor + [job(80, 'Harbor active.mkv', status='running')] + coast
    requests, submissions, errors, console_errors, unexpected = [], [], [], [], []

    async def run():
        delayed = asyncio.Event()
        release_delayed = asyncio.Event()
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            page.set_default_timeout(10000)
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)

            async def route(handler):
                request = handler.request
                url = urlparse(request.url)
                path, query = url.path, parse_qs(url.query)
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='''<html><head><title>Job search QA</title>
                    <meta name="viewport" content="width=device-width,initial-scale=1">
                    <link rel="stylesheet" href="/static/style.css"></head><body></body></html>''')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=inventory)
                elif path.endswith('/prompt-enhancer'):
                    await handler.fulfill(json={'models': []})
                elif path.endswith('/prompt-history'):
                    await handler.fulfill(json={'prompts': []})
                elif path.endswith('/seed-hunter'):
                    await handler.fulfill(json={'config': config, 'seed_hunter': harbor[0]['seed_hunter']})
                elif path.endswith('/auto-refine'):
                    submissions.append(request.post_data_json)
                    if len(submissions) == 1:
                        await handler.fulfill(status=503, json={'detail': 'Response lost after acceptance'})
                    else:
                        await handler.fulfill(json={'jobs': {item['id']: [] for item in harbor}, 'created': 180, 'skipped': 0, 'errors': {}, 'output_folder': '/fixture/projects/Harbor'})
                elif path.endswith('/jobs') and request.method == 'GET':
                    requests.append(query)
                    search = query.get('q', [''])[0]
                    page_size = int(query.get('page_size', ['10'])[0])
                    if search == 'delayed' and page_size == 50:
                        delayed.set()
                        await release_delayed.wait()
                    filtered = rows if search in ['', 'legacy'] else harbor if search == 'delayed' else [item for item in rows if all(term.lower() in (item['source_name'] + ' ' + item['prompt']).lower() for term in search.split())]
                    total_pages = max(1, math.ceil(len(filtered) / page_size))
                    number = min(int(query.get('page', ['1'])[0]), total_pages)
                    result = {'jobs': filtered[(number - 1) * page_size:number * page_size], 'queue': {'paused': False},
                              'counts': {'running': 1, 'queued': 567},
                              'pagination': {'page': number, 'total': len(filtered), 'total_pages': total_pages, 'before': 98765}}
                    if search and search != 'legacy':
                        result['search_query'] = search
                    await handler.fulfill(json=result)
                else:
                    unexpected.append((request.method, path))
                    await handler.fulfill(status=404, json={'detail': 'Fixture route missing'})

            await page.route('**/*', route)
            try:
                await page.goto('https://job-search.test/')
                assert await page.title() == 'Job search QA' and page.url == 'https://job-search.test/'
                await page.evaluate("async value => (await import('/static/js/h3SeedHunter.js')).showH3SeedHunter(value)", {'scout': scout, 'layout': layout})
                await page.get_by_text(('H3 Seed Scout' if scout else 'H3 Seed Hunter') + ' is ready.', exact=True).wait_for()
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=review]').click()
                search = page.get_by_role('searchbox', name='Search jobs', exact=True)
                pager = page.get_by_role('navigation', name='Job history pages', exact=True)
                cards = page.locator('.h3-video-jobs > article')
                await pw.expect(cards).to_have_count(10)
                await search.fill('  Harbor  ')
                await pw.expect(pager).to_contain_text('of 61 jobs')
                assert await cards.evaluate_all('(nodes) => nodes.map(node => node.dataset.jobId)') == [item['id'] for item in harbor[:10]]
                await pager.get_by_role('button', name='Next page of jobs').click()
                await pw.expect(pager).to_contain_text('Page 2 of 7')
                assert requests[-1]['q'] == ['Harbor'] and requests[-1]['before'] == ['98765']
                await page.get_by_role('button', name='Select all matching preview jobs', exact=True).click()
                await page.get_by_role('button', name='Refine selected jobs (60)…', exact=True).wait_for()
                all_pages = [query for query in requests if query.get('page_size') == ['50'] and query.get('q') == ['Harbor']]
                assert [query['page'] for query in all_pages] == [['1'], ['2']]
                assert all_pages[1]['before'] == ['98765']
                assert all(('group_refinements' not in query) if scout else query.get('group_refinements') == ['true'] for query in all_pages)
                await search.scroll_into_view_if_needed()
                assert await search.evaluate('n => { const r=n.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }')
                await page.screenshot(path=str(tmp_path / f'job-search-{width}.png'))
                await page.get_by_role('button', name='Refine selected jobs (60)…', exact=True).click()
                await page.locator('#h3-auto-project').fill('Harbor project')
                await page.get_by_role('button', name='Queue bulk refinements', exact=True).click()
                await page.get_by_text('Response lost after acceptance', exact=False).wait_for()
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=review]').click()
                await search.fill('Coast')
                await pw.expect(cards).to_have_count(2)
                await pw.expect(page.get_by_role('button', name='Refine selected jobs (0)…', exact=True)).to_be_disabled()
                await page.get_by_role('button', name='Retry bulk refinement submission', exact=True).last.click()
                await page.get_by_text('180 refinements queued', exact=False).wait_for()
                assert len(submissions) == 2 and submissions[0] == submissions[1]
                assert set(submissions[0]['jobs']) == {item['id'] for item in harbor}
                assert submissions[0]['settings']['project'] == 'Harbor project'
                await search.fill('delayed')
                await pw.expect(pager).to_contain_text('of 60 jobs')
                await page.get_by_role('button', name='Select all matching preview jobs', exact=True).click()
                await asyncio.wait_for(delayed.wait(), timeout=10)
                await search.fill('Coast')
                release_delayed.set()
                await pw.expect(cards).to_have_count(2)
                await pw.expect(page.get_by_role('button', name='Refine selected jobs (0)…', exact=True)).to_be_disabled()
                await page.get_by_role('button', name='Select all matching preview jobs', exact=True).click()
                await page.get_by_role('button', name='Refine selected jobs (2)…', exact=True).click()
                if layout == 'pipeline':
                    await page.locator('[data-pipeline-stage=review]').click()
                await search.fill('missing fixture')
                await pw.expect(page.locator('.h3-seed-hunter-final')).to_be_hidden()
                await pw.expect(pager).to_contain_text('0 jobs')
                await pw.expect(cards).to_have_count(0)
                await search.fill('legacy')
                await page.get_by_text('Could not refresh jobs: Update the Odysseus server', exact=False).wait_for()
                await pw.expect(cards).to_have_count(0)
                await page.get_by_role('button', name='Select all matching preview jobs', exact=True).click()
                await page.get_by_text('Update the Odysseus server before using job search.', exact=False).first.wait_for()
                await pw.expect(page.get_by_role('button', name='Refine selected jobs (0)…', exact=True)).to_be_disabled()
                assert not errors, errors
                # The only console error is the deliberately lost first POST response.
                assert all('503' in message for message in console_errors), console_errors
                assert not unexpected, unexpected
            finally:
                release_delayed.set()
                await browser.close()
    asyncio.run(run())
