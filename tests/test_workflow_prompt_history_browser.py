"""Prompt-history selection uses real workflow fields without submitting jobs."""
import asyncio
import base64
import json
import mimetypes
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jcAAAAABJRU5ErkJggg==')


@pytest.mark.parametrize('layout,width', [('classic', 1440), ('pipeline', 390)])
def test_history_across_real_workflow_forms_and_rerun_editor(tmp_path, layout, width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Browser plugin unavailable; Chrome required')
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ['model', 'encoder', 'video_vae', 'audio_vae', 'latent_upscaler']]
    gpus = [{'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}]
    defaults = dict(mode='ref2va', model='model', encoder='encoder', video_vae='video_vae', audio_vae='audio_vae',
                    gpu='GPU-5090', vae_gpu='', width=640, height=384, frames=124, steps=20, seed=42,
                    sampler='euler', scheduler='simple', shift_video=12, shift_audio=3, reference_size='match', loras=[])
    inventory = dict(runtime_ready=True, components=components, gpus=gpus, defaults=defaults, batch_jobs=True,
                     seed_hunter={'defaults': defaults, 'scout_defaults': {**defaults, 'seed_hunter': {'stage': 'scout', 'scout_count': 3}}, 'features': {'seed_scout': True, 'separate_final_length': True}})
    bfs = dict(runtime_ready=True, components=components, gpus=gpus, defaults={'gpu': 'GPU-5090'},
               workflows=[{'id': 'h3', 'label': 'MiniMax H3 head swap', 'family': 'h3', 'ready': True,
                           'slots': [{'key': 'model', 'label': 'H3 model', 'required': True, 'component_ids': ['model'], 'default_id': 'model'}],
                           'controls': [{'key': 'steps', 'label': 'Steps', 'type': 'integer', 'min': 1, 'max': 100, 'default': 20}],
                           'defaults': {'prompt': 'BFS draft', 'gpu': 'GPU-5090'}}])
    saved = [{'id': 'shared', 'prompt': 'Keep the camera still.\nChange the jacket to purple.', 'workflow': 'h3'},
             {'id': 'duplicate', 'prompt': ' Keep the camera still.\r\nChange the jacket to purple. ', 'workflow': 'bfs'}]
    older = {'id': 'older', 'prompt': 'An older saved prompt.', 'workflow': 'seed_hunter'}
    posts, errors, history_jobs = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 1000 if width > 400 else 844})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.set_default_timeout(6000)

            async def route(handler):
                req = handler.request; url = urlparse(req.url); path = url.path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><title>Prompt history QA</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/'); await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path == '/api/workflows/prompt-history':
                    if req.method == 'POST':
                        body = json.loads(req.post_data); posts.append(body)
                        existing = next((entry for entry in saved if entry['prompt'].strip() == body['prompt'].strip()), None)
                        entry = existing or {'id': 'manual', 'prompt': body['prompt'].strip(), 'workflow': body['workflow']}
                        if not existing: saved.insert(0, entry)
                        await handler.fulfill(json={'entry': entry, 'created': not bool(existing)})
                    else:
                        offset = int(parse_qs(url.query).get('offset', ['0'])[0])
                        await handler.fulfill(json={'prompts': [older] if offset else saved, 'total': 4, 'next_offset': None if offset else 100})
                elif path.endswith('/inventory'):
                    await handler.fulfill(json=bfs if '/bfs/' in path else inventory)
                elif path.endswith('/prompt-enhancer'): await handler.fulfill(json={'models': []})
                elif path.endswith('/gpus'): await handler.fulfill(json={'gpus': gpus})
                elif path.endswith('/jobs') and req.method == 'GET':
                    rows = history_jobs if '/h3/' in path and 'recipe' not in parse_qs(url.query) else []
                    await handler.fulfill(json={'jobs': rows, 'queue': {'paused': False}, 'pagination': {'page': 1, 'total': len(rows), 'total_pages': 1}, 'counts': {'queued': len(rows)}})
                elif path == '/api/video/h3/jobs' and req.method == 'POST':
                    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + req.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + req.post_data_buffer)
                    config = json.loads(next(part.get_payload(decode=True) for part in message.iter_parts() if part.get_param('name', header='content-disposition') == 'config'))
                    assert config['prompt'] == 'Newly queued reusable prompt.'
                    entry = {'id': 'queued-prompt', 'prompt': config['prompt'], 'workflow': 'h3'}
                    saved.insert(0, entry)
                    job = {'id': 'queued-fixture', 'status': 'queued', 'prompt': config['prompt'], 'queue_position': 1}
                    history_jobs[:] = [job]
                    await handler.fulfill(json=job)
                elif path.endswith('/queue/jobs'):
                    await handler.fulfill(json={'jobs': [], 'queue': {'paused': False}})
                else:
                    assert req.method == 'GET', f'Unexpected write: {req.method} {path}'
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})

            await page.route('**/*', route)
            try:
                await page.goto('http://prompt-history.test/')
                assert await page.title() == 'Prompt history QA'
                for kind, module, fn, prompt_id, image_id, key, ready in [
                    ('h3', 'h3Video', 'showH3Video', 'h3-prompt', 'h3-reference_images', 'odysseus-h3-video-v1', 'Components ready on this Odysseus server.'),
                    ('seed_hunter', 'h3SeedHunter', 'showH3SeedHunter', 'seed-hunter-prompt', 'seed-hunter-input-reference_images', 'odysseus-h3-seed-hunter-v1', 'H3 Seed Hunter is ready.'),
                    ('seed_scout', 'h3SeedHunter', 'showH3SeedScout', 'seed-hunter-prompt', 'seed-hunter-input-reference_images', 'odysseus-h3-seed-scout-v1', 'H3 Seed Scout is ready.'),
                    ('bfs', 'bfsVideo', 'showBfsVideo', 'bfs-prompt', 'bfs-identity_image', 'odysseus-bfs-video-v1', None),
                ]:
                    await page.evaluate("async v=>window.workflow=(await import('/static/js/'+v.module+'.js'))[v.fn]({layout:v.layout})", {'module': module, 'fn': fn, 'layout': layout})
                    if ready: await page.get_by_text(ready, exact=True).wait_for()
                    else: await page.locator('#bfs-workflow:not(:disabled)').wait_for(state='attached')
                    if layout == 'pipeline': await page.get_by_role('button', name='Show all settings', exact=True).click()
                    prompt = page.locator('#' + prompt_id)
                    await prompt.fill('Draft stays until I choose.')
                    image = page.locator('#' + image_id)
                    await image.set_input_files({'name': 'reference.png', 'mimeType': 'image/png', 'buffer': PNG})
                    history = page.get_by_role('combobox', name='Prompt history', exact=True).first
                    await history.locator('option[value=shared]').wait_for(state='attached')
                    assert await prompt.input_value() == 'Draft stays until I choose.'
                    assert await history.locator('option[value=duplicate]').count() == 0
                    settings_script = "promptId=>Object.fromEntries([...document.querySelectorAll('input[id],select[id],textarea[id]')].filter(n=>n.id!==promptId&&n.type!=='file').map(n=>[n.id,n.type==='checkbox'?n.checked:n.value]))"
                    settings_before = await page.evaluate(settings_script, prompt_id)
                    await history.select_option('shared')
                    assert await prompt.input_value() == next(row['prompt'] for row in saved if row['id'] == 'shared')
                    assert await page.evaluate(settings_script, prompt_id) == settings_before
                    if kind == 'bfs':
                        assert 'reference.png' in await image.locator('..').locator('.bfs-video-file-name').inner_text()
                    else:
                        assert await page.get_by_role('button', name='Remove reference.png', exact=True).count() == 1
                    await page.get_by_role('button', name='Refresh prompt history', exact=True).first.click()
                    await history.locator('option[value=shared]').wait_for(state='attached')
                    assert 'purple' in await prompt.input_value()
                    await history.select_option('__more__')
                    await history.locator('option[value=older]').wait_for(state='attached')
                    await history.select_option('older')
                    assert await prompt.input_value() == older['prompt']
                    assert not posts, 'Choosing history must not save fragments or queue jobs'
                    if kind == 'h3':
                        await prompt.fill('Newly queued reusable prompt.')
                        await page.get_by_role('button', name='Generate video', exact=True).click()
                        await page.get_by_text('Job queued-fixture added to the queue.', exact=False).wait_for()
                        await history.locator('option[value=queued-prompt]').wait_for(state='attached')
                        assert await prompt.input_value() == 'Newly queued reusable prompt.'
                        await prompt.fill(older['prompt'])
                        # The same real picker is attached to the dynamically
                        # cloned bulk/rerun prompt; it never edits the draft.
                        await page.get_by_role('button', name='Run again with new parameters', exact=True).click()
                        editor = page.locator('.video-queue-editor')
                        extra = editor.get_by_role('combobox', name='Prompt history', exact=True)
                        await extra.locator('option[value=shared]').wait_for(state='attached')
                        assert await extra.is_disabled()
                        await editor.get_by_role('checkbox', name='Change Prompt', exact=True).check()
                        await extra.select_option('shared')
                        assert 'purple' in await editor.get_by_role('textbox', name='New Prompt', exact=True).input_value()
                        assert await prompt.input_value() == older['prompt']
                    if kind == 'bfs':
                        await prompt.fill('A deliberate reusable prompt.')
                        await page.get_by_role('button', name='Save current prompt', exact=True).first.click()
                        await page.get_by_text('Prompt saved for all workflows.', exact=True).wait_for()
                        await page.get_by_role('button', name='Save current prompt', exact=True).first.click()
                        await page.get_by_text('Prompt is already saved; no duplicate added.', exact=True).wait_for()
                        assert posts[-1]['workflow'] == 'bfs'
                    await history.scroll_into_view_if_needed()
                    await page.screenshot(path=str(tmp_path / f'prompt-history-{kind}-{layout}.png'))
                    assert await history.evaluate('(n)=>n.getBoundingClientRect().right<=innerWidth')
                    await page.evaluate('workflow.close()')
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('multiline', [True, False])
def test_graph_prompt_history_keeps_inflight_draft_and_respects_locked_fields(tmp_path, multiline):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome: pytest.skip('Browser plugin unavailable; Chrome required')
    errors, writes = [], []
    document = {'format': 'odysseus-workflow-graph', 'version': 1, 'title': 'Prompt fixture',
                'nodes': [{'id': 'prompt', 'type': 'input.prompt', 'position': {'x': 0, 'y': 0}, 'params': {'text': 'Original graph draft'}}], 'edges': []}

    async def exercise():
        loaded, release = asyncio.Event(), asyncio.Event()
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000}); page.set_default_timeout(6000)
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def route(handler):
                req = handler.request; path = urlparse(req.url).path
                if path == '/': await handler.fulfill(content_type='text/html', body='<html><head><title>Graph prompt QA</title><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT/path.lstrip('/'); await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path == '/api/workflows/prompt-history':
                    assert req.method == 'GET', 'Selecting or typing must not save history'
                    loaded.set(); await release.wait()
                    await handler.fulfill(json={'prompts': [{'id': 'old', 'prompt': 'A saved complete prompt.', 'workflow': 'bfs'}, {'id': 'formatted', 'prompt': 'First line.\nSecond line.', 'workflow': 'h3'}], 'total': 2, 'next_offset': None})
                elif path.endswith('/catalog'):
                    await handler.fulfill(json={'node_types': [{'type': 'input.prompt', 'label': 'Prompt', 'params': [{'id': 'text', 'name': 'text', 'label': 'Prompt', 'type': 'text', 'multiline': multiline}], 'inputs': [], 'outputs': [{'id': 'prompt', 'type': 'TEXT'}]}], 'templates': [], 'inventory': {'h3': {}}})
                elif path.endswith('/validate'): await handler.fulfill(json={'valid': True, 'issues': [], 'plan': [], 'outputs': []})
                elif path.endswith('/runs'): await handler.fulfill(json={'runs': []})
                else:
                    if req.method != 'GET': writes.append(path)
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})
            await page.route('**/*', route)
            try:
                await page.goto('http://prompt-graph.test/')
                await page.evaluate("async document=>window.graph=(await import('/static/js/workflowGraphEditor.js')).showWorkflowGraphEditor({document})", document)
                prompt = page.locator('[data-graph-param=text]')
                await prompt.fill('Typed while history is loading.')
                await asyncio.wait_for(loaded.wait(), 3); release.set()
                history = page.get_by_role('combobox', name='Prompt history', exact=True)
                await history.locator('option[value=old]').wait_for(state='attached')
                assert await prompt.input_value() == 'Typed while history is loading.'
                await history.select_option('old')
                await page.wait_for_function("JSON.parse(localStorage.getItem('odysseus-workflow-graph-draft-v1')).document.nodes[0].params.text==='A saved complete prompt.'")
                if not multiline:
                    await history.select_option('formatted')
                    await page.get_by_text('This field cannot keep the saved prompt’s formatting. Your draft was kept.', exact=True).wait_for()
                    assert await prompt.input_value() == 'A saved complete prompt.'
                    assert await page.evaluate("JSON.parse(localStorage.getItem('odysseus-workflow-graph-draft-v1')).document.nodes[0].params.text") == 'A saved complete prompt.'
                await prompt.evaluate('(n)=>n.maxLength=5')
                await history.select_option('old')
                await page.get_by_text('This prompt exceeds this workflow’s 5 character limit. Your draft was kept.', exact=True).wait_for()
                assert await prompt.input_value() == 'A saved complete prompt.'
                await prompt.evaluate('(n)=>n.removeAttribute("maxlength")')
                await prompt.evaluate('(n)=>n.readOnly=true')
                await page.wait_for_function("document.querySelector('.workflow-prompt-history select').disabled")
                assert await page.get_by_role('button', name='Save current prompt', exact=True).is_disabled()
                await prompt.evaluate('(n)=>n.readOnly=false')
                await page.wait_for_function("!document.querySelector('.workflow-prompt-history select').disabled")
                # A disabled ancestor fieldset is as immutable as disabled=true.
                await prompt.evaluate("n=>{const box=document.createElement('fieldset'); n.parentElement.before(box); box.append(n.parentElement); box.disabled=true;}")
                assert await prompt.is_disabled() and await history.is_disabled()
                assert not writes and not errors, (writes, errors)
                await page.screenshot(path=str(tmp_path/'prompt-history-graph.png'))
            finally: await browser.close()
    asyncio.run(exercise())
