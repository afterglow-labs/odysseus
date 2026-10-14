"""Fresh-template attachment flow, camera gestures, and immutable queue payload.

Real graph compiler, real UI, isolated HTTP fixtures; no production rendering.
"""
import asyncio
from email.parser import BytesParser
from email.policy import default
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

from src.workflow_graph import catalog, compile_graph, new_document

ROOT = Path(__file__).resolve().parents[1]


def test_fresh_scout_references_local_server_restore_and_canvas_pan(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('A local Chrome/Chromium browser is required')
    inventory = {'components': [{'id': role, 'role': role, 'name': role + '.safetensors'}
                               for role in ('model', 'encoder', 'video_vae', 'audio_vae')]}
    cat = catalog()
    cat['inventory'] = {'h3': inventory, 'bfs': {}}
    for template in cat['templates']:
        template['document'] = new_document(template['title'], template['id'])
        template['document'].setdefault('metadata', {})['setup_notes'] = [{
            'severity': 'info', 'code': 'component_matched', 'node_id': 'references',
            'field': 'component', 'message': 'Installed components selected. Attach your reference media.'}]
        for node in template['document']['nodes']:
            if node['type'].startswith('component.'):
                node['params']['component'] = node['type'].split('.')[1]
            if node['type'] == 'input.prompt':
                node['params']['text'] = 'Keep the source scene.'
    submissions, errors, saved, checked = [], [], {}, []

    async def run():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1500, 'height': 1050})
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                path = urlparse(request.url).path
                if path == '/':
                    await handler.fulfill(body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>', content_type='text/html')
                elif path.startswith('/static/'):
                    f = ROOT / path.lstrip('/')
                    await handler.fulfill(body=f.read_bytes(), content_type='text/javascript' if f.suffix == '.js' else mimetypes.guess_type(f.name)[0])
                elif path.endswith('/catalog'):
                    await handler.fulfill(json=cat)
                elif path.endswith('/validate'):
                    document = json.loads(request.post_data)['document']
                    result = compile_graph(document, inventory=inventory)
                    checked.append(result)
                    await handler.fulfill(json=result)
                elif path.endswith('/server-file-roots'):
                    await handler.fulfill(json={'roots': [{'id': root, 'name': root.upper()+':', 'available': True} for root in ('e', 'f')]})
                elif path.endswith('/server-files'):
                    root = parse_qs(urlparse(request.url).query)['root'][0]
                    selection = {'root': root, 'path': 'reference-'+root+'.mp4', 'size': 10, 'mtime_ns': '123'}
                    await handler.fulfill(json={'root': root, 'path': '', 'parent_path': None, 'total': 1, 'has_more': False,
                                               'entries': [{'kind': 'video', 'name': selection['path'], 'selection': selection, 'size': 10}]})
                elif path.endswith('/documents') and request.method == 'POST':
                    saved.update(id='saved', revision=1, document=json.loads(request.post_data)['document'])
                    await handler.fulfill(json=saved)
                elif path.endswith('/documents'):
                    await handler.fulfill(json={'documents': [{'id': 'saved', 'revision': 1, 'title': saved['document']['title']}]})
                elif path.endswith('/documents/saved'):
                    await handler.fulfill(json=saved)
                elif path.endswith('/runs') and request.method == 'POST':
                    message = BytesParser(policy=default).parsebytes(('Content-Type: '+request.headers['content-type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+request.post_data_buffer)
                    fields = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True) for part in message.iter_parts()}
                    submissions.append({'document': json.loads(fields['document']), 'bindings': json.loads(fields['bindings']), 'fields': fields})
                    await handler.fulfill(json={'id': 'fixture-run', 'status': 'queued'})
                elif path.endswith('/runs'):
                    await handler.fulfill(json={'runs': []})
                else:
                    pytest.fail(f'Unexpected request {request.method} {path}')

            await page.route('**/*', route)
            try:
                await page.goto('http://workflow-references.test/')
                async def open_editor():
                    await page.evaluate("async()=>{window.builder=(await import('/static/js/workflowGraphEditor.js')).showWorkflowGraphEditor();}")
                    await page.wait_for_function('!!window.builder.store')
                await open_editor()
                panel = page.get_by_role('dialog', name='Workflow builder')
                template = next(t for t in cat['templates'] if t['id'] == 'seed_scout')
                await panel.get_by_role('button', name=template['title'], exact=False).click()
                await panel.locator('.wg-setup-notes summary').click()
                assert await panel.get_by_text('Installed components selected. Attach your reference media.', exact=False).is_visible()
                await panel.locator('.wg-setup-notes').get_by_role('button', name='Inspect', exact=True).click()
                await panel.locator('.wg-pipeline [data-graph-node="references"]').click()
                refs = panel.locator('[data-reference-port="reference_videos"]')
                # A real file chooser opens directly on the fresh References bundle.
                async with page.expect_file_chooser() as chooser:
                    await refs.get_by_role('button', name='Add reference videos', exact=True).click()
                await (await chooser.value).set_files([{'name': name, 'mimeType': 'video/mp4', 'buffer': name.encode()} for name in ('local-one.mp4', 'local-two.mp4')])
                assert await refs.locator('.wg-reference-file').count() == 2
                await refs.get_by_role('button', name='Server files · E: / F:', exact=True).click()
                picker = page.get_by_role('dialog', name='Server files', exact=True)
                assert await picker.get_by_role('combobox', name='Drive', exact=True).locator('option').count() == 2
                await picker.get_by_role('checkbox', name='reference-e.mp4', exact=False).check()
                await picker.get_by_role('combobox', name='Drive', exact=True).select_option('f')
                await picker.get_by_role('checkbox', name='reference-f.mp4', exact=False).check()
                await picker.get_by_role('button', name='Use 2 videos', exact=True).click()
                assert await refs.locator('.wg-reference-file').count() == 4
                # Close/reopen in this tab retains File objects and server selections.
                await panel.get_by_role('button', name='Close', exact=True).click()
                await open_editor()
                assert await refs.locator('.wg-reference-file').count() == 4
                assert await refs.get_by_role('button', name='Attach again', exact=True).count() == 0
                # Drop and undo changes only that added reference, not the prior four.
                await refs.locator('.wg-media-input').evaluate("el=>{const dt=new DataTransfer();dt.items.add(new File(['x'],'drop.mp4',{type:'video/mp4'}));el.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:dt}));}")
                assert await refs.locator('.wg-reference-file').count() == 5
                await panel.get_by_role('button', name='Undo', exact=True).click()
                assert await refs.locator('.wg-reference-file').count() == 4
                await panel.get_by_role('button', name='Queue workflow', exact=True).click()
                await panel.get_by_text('Workflow queued', exact=False).wait_for()
                assert len(submissions) == 1
                submitted = submissions[0]
                edges = [e for e in submitted['document']['edges'] if e['target'] == {'node': 'references', 'port': 'reference_videos'}]
                assert len(edges) == 4
                assert {v[0]['server']['root'] for v in submitted['bindings'].values() if 'server' in v[0]} == {'e', 'f'}
                assert sum('upload' in v[0] for v in submitted['bindings'].values()) == 2
                compiled = compile_graph(submitted['document'], inventory=inventory)
                assert compiled['valid'], compiled
                assert len(compiled['plan']) == 1, 'Reference videos must not become separate batch jobs'
                # Duplicate a bound input: the new operation retains its File binding.
                source = edges[0]['source']['node']
                await panel.locator(f'.wg-pipeline [data-graph-node="{source}"]').click()
                await panel.get_by_role('button', name='Duplicate', exact=True).click()
                assert await panel.locator('.wg-inspector .wg-media-row').count() == 1
                async with page.expect_response(lambda response: urlparse(response.url).path.endswith('/runs') and response.request.method == 'POST'):
                    await panel.get_by_role('button', name='Queue workflow', exact=True).click()
                assert len(submissions) == 2
                assert len(submissions[-1]['bindings']) == 4, 'An unconnected duplicate must not upload another file'
                # Camera panning changes no node coordinates; node dragging does.
                await panel.get_by_role('button', name='Nodes', exact=True).click()
                await panel.get_by_role('button', name='Fit', exact=True).click()
                before = await page.evaluate('JSON.stringify(builder.store.document.nodes.map(n=>n.position))')
                viewport = panel.locator('.wg-viewport'); bounds = await viewport.bounding_box()
                await page.mouse.move(bounds['x']+30, bounds['y']+30); await page.mouse.down(); await page.mouse.move(bounds['x']+130, bounds['y']+90, steps=6); await page.mouse.up()
                assert 'translate(100px,60px)' in (await panel.locator('.wg-scene').get_attribute('style')).replace(' ', '')
                assert await page.evaluate('JSON.stringify(builder.store.document.nodes.map(n=>n.position))') == before
                await panel.get_by_role('button', name='Fit', exact=True).click()
                assert 'translate(0px,0px)' in (await panel.locator('.wg-scene').get_attribute('style')).replace(' ', '')
                original = await page.evaluate('builder.store.document.nodes.find(n=>n.id==="generate").position')
                head = panel.locator('.wg-node[data-graph-node="generate"] .wg-node-head')
                await head.scroll_into_view_if_needed(); node_box = await head.bounding_box()
                await page.mouse.move(node_box['x']+25, node_box['y']+15); await page.mouse.down(); await page.mouse.move(node_box['x']+75, node_box['y']+35, steps=5); await page.mouse.up()
                assert await page.evaluate('builder.store.document.nodes.find(n=>n.id==="generate").position') != original
                assert 'translate(0px,0px)' in (await panel.locator('.wg-scene').get_attribute('style')).replace(' ', '')
                await panel.get_by_role('button', name='Pipeline', exact=True).click()
                await panel.get_by_role('button', name='Save', exact=True).click()
                await panel.get_by_text('Workflow saved on this server.', exact=True).wait_for()
                await panel.get_by_role('button', name='Library', exact=True).click()
                await panel.get_by_text('Library workflows restore settings and connections; attach media again before queuing.', exact=False).wait_for()
                await panel.get_by_role('button', name=saved['document']['title'], exact=False).click()
                await panel.locator('.wg-pipeline [data-graph-node="references"]').click()
                assert await refs.get_by_role('button', name='Attach again', exact=True).count() == 4
                # Other native workflows receive the same direct media controls.
                await panel.locator('.wg-main-toolbar').get_by_role('button', name='+ Add operation', exact=True).click()
                await panel.locator('[data-operation-type="bfs.generate"]').click()
                assert await panel.locator('.wg-reference-slot').count() == 4
                source_slot = panel.locator('[data-reference-port="source_video"]')
                await source_slot.locator('input[type=file]').set_input_files({'name':'bfs-source.mp4','mimeType':'video/mp4','buffer':b'bfs'})
                assert await source_slot.locator('.wg-reference-file').count() == 1
                assert await panel.locator('[data-reference-port="identity_image"] summary').count() == 1
                assert await panel.locator('[data-reference-port="mask_video"] summary').count() == 1
                assert await panel.locator('[data-reference-port="last_frame"] summary').count() == 1
                await panel.screenshot(path=str(tmp_path/'reference-attachments-desktop.png'))
                await page.set_viewport_size({'width': 390, 'height': 844})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                await panel.locator('.wg-pipeline [data-graph-node="references"]').click()
                assert await panel.locator('.wg-footer details[open]').count() == 0
                async with page.expect_file_chooser() as mobile_chooser:
                    await refs.get_by_role('button', name='Add reference videos', exact=True).click()
                await (await mobile_chooser.value).set_files({'name':'mobile.mp4','mimeType':'video/mp4','buffer':b'mobile'})
                await refs.locator('.wg-media-input > .wg-button').filter(has_text='Server files').click()
                await picker.get_by_role('combobox', name='Drive', exact=True).select_option('f')
                await picker.get_by_role('checkbox', name='reference-f.mp4', exact=False).check()
                await picker.get_by_role('button', name='Use 1 video', exact=False).click()
                assert await refs.locator('.wg-reference-file').count() == 6
                await refs.get_by_role('button', name='Add reference videos', exact=True).scroll_into_view_if_needed()
                await panel.screenshot(path=str(tmp_path/'reference-attachments-mobile.png'))
                await panel.get_by_role('button', name='Nodes', exact=True).click()
                await panel.get_by_role('button', name='Fit', exact=True).click()
                await viewport.scroll_into_view_if_needed(); bounds = await viewport.bounding_box()
                cdp = await page.context.new_cdp_session(page)
                await cdp.send('Emulation.setTouchEmulationEnabled', {'enabled': True})
                await cdp.send('Input.dispatchTouchEvent', {'type':'touchStart','touchPoints':[{'x':bounds['x']+24,'y':bounds['y']+24}]})
                await cdp.send('Input.dispatchTouchEvent', {'type':'touchMove','touchPoints':[{'x':bounds['x']+84,'y':bounds['y']+64}]})
                await cdp.send('Input.dispatchTouchEvent', {'type':'touchEnd','touchPoints':[]})
                assert 'translate(60px,40px)' in (await panel.locator('.wg-scene').get_attribute('style')).replace(' ', '')
                # A full reload cannot retain File objects; the draft clearly says so.
                await page.reload()
                await open_editor()
                await panel.get_by_text('saved media inputs need files attached again.', exact=False).wait_for()
                await panel.get_by_role('button', name='Pipeline', exact=True).click()
                await panel.locator('.wg-pipeline [data-graph-node="references"]').click()
                assert await refs.get_by_role('button', name='Attach again', exact=True).count() == 5
                assert await panel.locator('.wg-setup-notes').is_visible()
                assert not errors, errors
            finally:
                await browser.close()

    asyncio.run(run())
