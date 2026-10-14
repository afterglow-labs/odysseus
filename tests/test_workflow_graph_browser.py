"""Read-only browser integration using real installed metadata and isolated routes.

No route contacts the application server, installs packages, or starts a render.
The small synthetic package adds deterministic cases alongside installed nodes.
"""
import asyncio
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

from src.comfy_node_manifest import convert_object_info, load_manifests
from src.workflow_graph import new_document, node_registry

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_controls_and_run_previews(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('A local Chrome/Chromium browser is required')
    manifests = load_manifests()
    fixture = convert_object_info({
        'SchemaFixtureSink': {
            'display_name': 'Schema fixture', 'category': 'Tests',
            'input': {
                'required': {
                    'text': ['STRING', {'default': '', 'multiline': True}],
                    'filename': ['STRING', {'default': 'result', 'multiline': False}],
                    'choice': [[False, True], {'default': False}],
                    'image': [['example.png'], {'image_upload': True}],
                },
                'optional': {'upstream': ['STRING', {'forceInput': True}]},
                'hidden': {'unique_id': 'UNIQUE_ID'},
            },
            'is_input_list': True, 'output': ['IMAGE'], 'output_is_list': [True], 'output_node': True,
        }
    })
    catalog = {'node_types': list(node_registry([*manifests, fixture]).values()), 'templates': [],
               'inventory': {'h3': {'gpus': [{'id': '1', 'name': 'RTX 5090'}]}, 'bfs': {}}}
    document = new_document('Metadata control check', 'blank')
    document['nodes'] = [{'id': 'fixture', 'type': 'comfy.SchemaFixtureSink', 'params': {}}]
    runs = [
        {'id': 'zip', 'status': 'completed', 'title': 'Package run', 'steps': [],
         'outputs': [{'id': 'zip', 'name': 'results.zip', 'status': 'completed', 'url': '/api/test/results'}]},
        {'id': 'media', 'status': 'running', 'title': 'Preview run', 'steps': [
            {'id': 'scout', 'status': 'running', 'job_id': 'scout', 'job': {'scout': {
                'status': 'waiting_for_selection', 'previews': [{'seed': 7, 'url': '/api/test/preview.webp'}]}}}],
         'outputs': [{'id': 'video', 'name': 'preview.mp4', 'status': 'completed', 'url': '/api/test/video'}]},
    ]
    errors = []

    async def exercise():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1500, 'height': 1100})
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                path = urlparse(request.url).path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body></body></html>')
                elif path.startswith('/static/'):
                    source = ROOT / path.lstrip('/')
                    await handler.fulfill(body=source.read_bytes(), content_type='text/javascript' if source.suffix == '.js' else mimetypes.guess_type(source.name)[0])
                elif path.endswith('/catalog'):
                    await handler.fulfill(json=catalog)
                elif path.endswith('/validate'):
                    await handler.fulfill(json={'valid': False, 'issues': [], 'plan': []})
                elif path.endswith('/packages'):
                    await handler.fulfill(json={'packages': manifests, 'candidates': []})
                elif path.endswith('/runs'):
                    assert request.method == 'GET', 'This UI check must never submit a render'
                    await handler.fulfill(json={'runs': runs})
                elif '/runs/' in path:
                    await handler.fulfill(json=next(row for row in runs if row['id'] == path.split('/')[-1]))
                elif path.endswith('preview.webp'):
                    await handler.fulfill(content_type='image/gif', body=bytes.fromhex('47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b'))
                elif path.startswith('/api/test/'):
                    await handler.fulfill(body=b'')
                else:
                    pytest.fail(f'Unexpected browser request: {request.method} {path}')

            await page.route('**/*', route)
            try:
                await page.goto('http://odysseus-workflow.test/')
                await page.evaluate("async document => { window.builder = (await import('/static/js/workflowGraphEditor.js')).showWorkflowGraphEditor({document}); }", document)
                panel = page.get_by_role('dialog', name='Workflow builder')
                await panel.get_by_role('heading', name='Schema fixture', exact=True).wait_for()
                assert await panel.locator('textarea[data-graph-param="text"]').count() == 1
                assert await panel.locator('input[data-graph-param="filename"]').count() == 1
                await panel.get_by_role('combobox', name='Choice', exact=True).select_option('true')
                assert await page.evaluate('builder.store.document.nodes[0].params.choice === true')
                assert await panel.get_by_role('combobox', name='Execution device', exact=True).input_value() == 'cpu'
                assert await panel.get_by_text('upstream · TEXT list · optional', exact=True).count() == 1
                assert await panel.get_by_text('This package declares a connection-only input.', exact=False).count() == 1
                await panel.get_by_text('Package input rules and schema', exact=True).click()
                assert 'UNIQUE_ID' in await panel.locator('.wg-inspector pre').inner_text()
                assert await panel.locator('.wg-inspector .wg-media-input input[type=file]').count() == 1
                # All installed declarations are offered without package-specific hardcoding.
                await panel.locator('.wg-main-toolbar').get_by_role('button', name='+ Add operation', exact=True).click()
                assert await panel.locator('[data-operation-type]').count() == len(catalog['node_types'])
                if 'comfy.SaveImage' in {row['type'] for row in catalog['node_types']}:
                    await panel.locator('[data-operation-type="comfy.SaveImage"]').click()
                    assert await panel.locator('input[data-graph-param="filename_prefix"]').count() == 1
                else:
                    await panel.get_by_role('button', name='Back to workflow', exact=True).click()
                await panel.locator('.wg-runs summary').click()
                await panel.locator('.wg-run').filter(has_text='Package run').get_by_role('button', name='Details', exact=True).click()
                zip_card = panel.locator('.wg-run').filter(has_text='Package run')
                await zip_card.get_by_role('link', name='Download results ZIP', exact=True).wait_for()
                assert await zip_card.get_by_role('link', name='Download results ZIP', exact=True).count() == 1
                assert await zip_card.get_by_role('button', name='Play output', exact=True).count() == 0
                media_card = panel.locator('.wg-run').filter(has_text='Preview run')
                await media_card.get_by_role('button', name='Play output', exact=True).click()
                await page.evaluate('window.retainedPlayer = document.querySelector(".wg-output video"); window.retainedScout = document.querySelector(".wg-scout img");')
                runs[1]['steps'][0]['status'] = 'waiting_for_selection'
                await panel.get_by_role('button', name='Refresh runs', exact=True).click()
                await page.wait_for_timeout(100)
                assert await page.evaluate('retainedPlayer === document.querySelector(".wg-output video") && retainedScout === document.querySelector(".wg-scout img")')
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
                await page.screenshot(path=str(tmp_path / 'workflow-metadata-mobile.png'))
            finally:
                await browser.close()

    asyncio.run(exercise())
