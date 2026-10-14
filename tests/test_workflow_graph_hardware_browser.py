"""Existing drafts can choose actual GPUs and resume only their own node previews."""
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

from src.workflow_graph import catalog, compile_graph, new_document

ROOT = Path(__file__).resolve().parents[1]


def test_stale_gpu_hub_upscaler_and_in_node_scout_selection(tmp_path):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome/Chromium required')
    inventory = {'components': [{'id': role, 'role': role, 'name': role+'.safetensors'} for role in ('model','encoder','video_vae','audio_vae','latent_upscaler')],
                 'gpus': [{'id':'GPU-4090','name':'NVIDIA GeForce RTX 4090','index':0}, {'id':'GPU-5090','name':'NVIDIA GeForce RTX 5090','index':1}]}
    cat = catalog(); cat['inventory'] = {'h3':inventory,'bfs':{}}
    document = new_document('Saved Scout draft', 'seed_scout')
    for node in document['nodes']:
        if node['type'].startswith('component.'): node['params']['component']=node['type'].split('.')[1]
        if node['type']=='input.prompt': node['params']['text']='Keep the scene and audio exactly as supplied.'
        if node['type']=='h3.seed_scout': node['params'].update(gpu='0',scout_count=3)
    run = {'id':'chosen-run','status':'running','document':None,'outputs':[],'steps':[
        {'id':'generate','node_type':'h3.seed_scout','status':'running','job_id':'correct-scout-job','job':{'scout':{
            'status':'waiting_for_selection','previews':[{'seed':seed,'url':f'/api/test/preview-{seed}.webp'} for seed in (11,22,33)]}}}]}
    unrelated = {'id':'unrelated-run','status':'running','title':'Another workflow','outputs':[],'steps':[
        {'id':'generate','job_id':'wrong-job','job':{'scout':{'status':'waiting_for_selection','previews':[{'seed':999,'url':'/api/test/wrong.webp'}]}}}]}
    submissions, selections, errors, lookups = [], [], [], []

    async def run_test():
        async with pw.async_playwright() as p:
            browser=await p.chromium.launch(executable_path=chrome,headless=True,args=['--no-sandbox'])
            page=await browser.new_page(viewport={'width':1500,'height':1150});page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(handler):
                req=handler.request; path=urlparse(req.url).path
                if path=='/': await handler.fulfill(content_type='text/html',body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    source=ROOT/path.lstrip('/');await handler.fulfill(body=source.read_bytes(),content_type='text/javascript' if source.suffix=='.js' else mimetypes.guess_type(source.name)[0])
                elif path.endswith('/catalog'): lookups.append('catalog');await handler.fulfill(json=cat)
                elif path.endswith('/validate'):
                    doc=json.loads(req.post_data)['document'];result=compile_graph(doc,inventory=inventory)
                    if next(n for n in doc['nodes'] if n['id']=='generate')['params']['gpu'] not in ('GPU-4090','GPU-5090'):
                        result['valid']=False;result['issues'].append({'severity':'error','node_id':'generate','field':'gpu','message':'GPU 0 is unavailable; choose an available GPU.'})
                    await handler.fulfill(json=result)
                elif path.endswith('/configure'):
                    doc=json.loads(req.post_data)['document'];next(n for n in doc['nodes'] if n['id']=='generate')['params']['gpu']='GPU-5090'
                    note={'severity':'info','node_id':'generate','field':'gpu','message':'Selected compatible NVIDIA GeForce RTX 5090.'};doc.setdefault('metadata',{})['setup_notes']=[note]
                    await handler.fulfill(json={'document':doc,'setup_notes':[note]})
                elif path.endswith('/runs') and req.method=='POST':
                    message=BytesParser(policy=default).parsebytes(('Content-Type: '+req.headers['content-type']+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+req.post_data_buffer)
                    fields={part.get_param('name',header='content-disposition'):part.get_payload(decode=True) for part in message.iter_parts()}
                    submissions.append({'document':json.loads(fields['document']),'bindings':json.loads(fields['bindings'])});run['document']=submissions[-1]['document']
                    await handler.fulfill(json={'id':run['id'],'status':'running'})
                elif path.endswith('/runs'): await handler.fulfill(json={'runs':[unrelated,run] if run['document'] else []})
                elif path.endswith('/runs/chosen-run'): lookups.append('run');await handler.fulfill(json=run)
                elif path.endswith('/runs/unrelated-run'):
                    await handler.fulfill(json={**unrelated,'document':{'nodes':[{'id':'generate','type':'h3.generate'}],'edges':[]}})
                elif path.endswith('/correct-scout-job/scout/select'):
                    selections.append(json.loads(req.post_data));run['steps'][0]['job']['scout'].update(status='running',selected_seed=selections[-1]['seed']);await handler.fulfill(json={'status':'selected'})
                elif path.startswith('/api/test/preview-'):
                    await handler.fulfill(content_type='image/gif',body=bytes.fromhex('47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b'))
                elif path=='/api/test/wrong.webp':
                    await handler.fulfill(content_type='image/gif',body=bytes.fromhex('47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b'))
                else: pytest.fail(f'Unexpected request {req.method} {path}')
            await page.route('**/*',route)
            try:
                await page.goto('http://scout-hardware.test/')
                await page.evaluate("doc=>localStorage.setItem('odysseus-workflow-graph-draft-v1',JSON.stringify({document:doc,view:'pipeline'}))",document)
                await page.evaluate("async()=>{window.builder=(await import('/static/js/workflowGraphEditor.js')).showWorkflowGraphEditor();}")
                await page.wait_for_function('!!builder.store')
                panel=page.get_by_role('dialog',name='Workflow builder');hardware=panel.locator('.wg-hardware')
                assert await hardware.get_attribute('open') is not None
                gpu=hardware.get_by_role('combobox',name='Model GPU',exact=True)
                assert await gpu.input_value()=='0'
                assert 'Unavailable saved device: 0' in await gpu.locator('option:checked').inner_text()
                await panel.get_by_role('button',name='Check workflow',exact=True).click()
                issue=panel.locator('.wg-validation-body p').filter(has_text='GPU 0 is unavailable')
                await issue.get_by_role('button',name='Inspect',exact=True).click()
                await page.wait_for_function("document.activeElement.matches('.wg-hardware [data-graph-param=gpu]')")
                assert await gpu.evaluate('el=>document.activeElement===el')
                await gpu.select_option('GPU-5090')
                await hardware.get_by_role('combobox',name='VAE GPU',exact=True).select_option('GPU-4090')
                await hardware.get_by_role('button',name='Refresh devices',exact=True).click()
                await panel.get_by_text('Detected devices refreshed.',exact=True).wait_for()
                assert lookups.count('catalog')==2
                assert await gpu.input_value()=='GPU-5090'
                # The direct setup control creates a real component and edge, undoable together.
                await panel.locator('.wg-pipeline [data-graph-node="setup"]').click()
                upscaler=panel.locator('.wg-inspector').get_by_role('combobox',name='Latent upscaler',exact=True)
                await upscaler.select_option(''); await upscaler.select_option('latent_upscaler')
                assert await page.evaluate("builder.store.document.edges.some(e=>e.target.node==='setup' && e.target.port==='latent_upscaler')")
                await panel.get_by_role('button',name='Undo',exact=True).click()
                assert not await page.evaluate("builder.store.document.edges.some(e=>e.target.node==='setup' && e.target.port==='latent_upscaler')")
                await upscaler.select_option('latent_upscaler')
                await panel.locator('.wg-pipeline [data-graph-node="references"]').click()
                refs=panel.locator('[data-reference-port="reference_videos"]')
                await refs.locator('input[type=file]').set_input_files({'name':'source.mp4','mimeType':'video/mp4','buffer':b'source'})
                source_ids=await page.evaluate("builder.store.document.nodes.filter(n=>n.type==='input.video').map(n=>n.id)")
                # Explicit repair keeps this draft's media and unrelated settings.
                await page.evaluate("builder.store.parameter('generate','gpu','legacy-missing')")
                await hardware.get_by_role('button',name='Repair setup',exact=True).click()
                await panel.get_by_text('Setup checked against installed components',exact=False).wait_for()
                assert await gpu.input_value()=='GPU-5090'
                assert await page.evaluate("builder.store.document.nodes.filter(n=>n.type==='input.video').map(n=>n.id)")==source_ids
                assert await page.evaluate('Object.keys(builder.bindings).length')==1
                assert 'Keep the scene' in await page.evaluate("builder.store.document.nodes.find(n=>n.type==='input.prompt').params.text")
                assert 'No connected inputs' not in await panel.locator('.wg-pipeline').inner_text()
                await panel.get_by_role('button',name='Nodes',exact=True).click()
                scout=panel.locator('.wg-node[data-graph-node="generate"]')
                assert await scout.locator('[data-preview-slot]').count()==3
                assert await scout.locator('img,video').count()==0
                await panel.get_by_role('button',name='Queue workflow',exact=True).click()
                await scout.locator('img').nth(2).wait_for()
                assert await scout.locator('img').count()==3
                assert '999' not in await scout.inner_text()
                assert submissions[0]['document']['nodes'][next(i for i,n in enumerate(submissions[0]['document']['nodes']) if n['id']=='generate')]['params']['gpu']=='GPU-5090'
                assert len(submissions[0]['bindings'])==1
                await page.evaluate("window.previewImages=[...document.querySelectorAll('.wg-node[data-graph-node=generate] img')]")
                await scout.locator('[data-preview-slot="2"]').get_by_role('button',name='Continue with this seed',exact=True).click()
                await scout.get_by_text('Selected · continuing saved latent',exact=True).wait_for()
                assert selections==[{'seed':22}]
                assert await page.evaluate("previewImages.every((img,index)=>img===document.querySelectorAll('.wg-node[data-graph-node=generate] img')[index])")
                await panel.get_by_role('button',name='Fit',exact=True).click()
                await scout.screenshot(path=str(tmp_path/'seed-scout-node-previews.png'))
                await page.set_viewport_size({'width':390,'height':844})
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                if await hardware.get_attribute('open') is None:
                    await hardware.locator('summary').click()
                assert await gpu.is_visible()
                await panel.screenshot(path=str(tmp_path/'hardware-mobile.png'))
                assert not errors,errors
            finally: await browser.close()
    asyncio.run(run_test())
