"""A real browser confirmation reaches production routes and removes fixture files."""
import asyncio
import mimetypes
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlparse

import pytest

from core import database
from routes import h3_video_routes
from src import h3_video as h3
from tests.test_h3_gpu_delete import db, gallery
from tests.test_video_history_delete import client_for, managers, save

ROOT = Path(__file__).resolve().parents[1]


def test_browser_delete_history_removes_real_job_files_and_gallery_only_after_confirmation(managers, db, tmp_path, monkeypatch):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    manager, bfs = managers
    components = [{'id': role, 'role': role, 'name': role + '.safetensors', 'variant': 'ref2va'}
                  for role in ('model', 'encoder', 'video_vae', 'audio_vae')]
    inventory = {'runtime_ready': True, 'components': components,
                 'gpus': [{'id': 'GPU-5090', 'name': 'RTX 5090', 'nvfp4': True}]}
    monkeypatch.setattr(manager, 'inventory', lambda: inventory)
    monkeypatch.setattr(h3_video_routes, 'gpu_telemetry', lambda: {'gpus': inventory['gpus']})
    async def no_enhancer(_owner):
        return {'models': []}
    monkeypatch.setattr(h3_video_routes, 'enhancer_status', no_enhancer)
    selected = []
    for index in range(12):
        directory, state = save(manager, status=('completed', 'failed', 'stopped')[index % 3])
        gallery_path = gallery(db, manager, state)
        (directory / 'candidates').mkdir()
        (directory / 'candidates' / 'preview.mp4').write_bytes(b'fixture-preview')
        selected.append((directory, state, gallery_path))
    external = tmp_path / 'original-library.mkv'
    external.write_bytes(b'Keep the original library file')
    os.link(external, selected[0][0] / 'source-reference.mkv')
    preserved = [save(manager, owner='other')[0], save(bfs)[0], save(manager, stage='preview')[0]]
    for status in ('queued', 'running'):
        directory, state = save(manager, status=status)
        # These are state-only fixtures, not render workers. Keep them inside the
        # manager's startup grace window throughout a slower browser startup.
        h3._write(directory / 'state.json', state | {'created_at': time.time() + 3600})
        preserved.append(directory)
    writes, failures = [], []

    async def exercise(client):
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1365, 'height': 1000})
            page.set_default_timeout(10000)
            page.on('pageerror', lambda error: failures.append(str(error)))
            async def route(handler):
                req = handler.request
                url = urlparse(req.url)
                if url.path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif url.path.startswith('/static/'):
                    file = ROOT / url.path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif url.path.startswith('/api/video/'):
                    if req.method == 'POST':
                        writes.append((url.path, req.post_data_json))
                        assert url.path.endswith(('/history/delete-preview', '/history/delete'))
                    response = await asyncio.to_thread(client.request, req.method,
                        url.path + ('?' + url.query if url.query else ''), content=req.post_data,
                        headers={'Content-Type': req.headers.get('content-type', 'application/json')})
                    if response.status_code >= 400:
                        failures.append(f'{url.path}: {response.status_code}: {response.text}')
                    await handler.fulfill(status=response.status_code, body=response.content,
                                          content_type=response.headers.get('content-type', 'application/json'))
                else:
                    await handler.fulfill(status=404, json={'detail': 'Fixture not found'})
            await page.route('**/*', route)
            try:
                await page.goto('http://history-api.test/')
                await page.evaluate("async()=>{(await import('/static/js/h3Video.js')).showH3Video();}")
                workflow = page.locator('.h3-video-dialog')
                history = workflow.locator('.video-history-actions')
                await history.get_by_role('button', name='Delete all job history', exact=True).click()
                confirmation = page.get_by_role('dialog', name='Delete all job history?', exact=True)
                await pw.expect(confirmation).to_contain_text('12 finished jobs')
                await pw.expect(confirmation).to_contain_text('2 queued or running jobs are kept')
                assert all(directory.exists() and output.exists() for directory, _, output in selected)
                assert [path for path, _ in writes] == ['/api/video/h3/history/delete-preview']
                await confirmation.get_by_role('button', name='Keep job history', exact=True).click()
                assert all(directory.exists() for directory, _, _ in selected)
                await history.get_by_role('button', name='Delete all job history', exact=True).click()
                await confirmation.get_by_role('button', name='Delete 12 finished jobs permanently', exact=True).click()
                await pw.expect(confirmation).to_be_hidden()
                await pw.expect(history).to_contain_text('12 finished jobs deleted')
                await pw.expect(workflow.get_by_role('navigation', name='Job history pages', exact=True)).to_contain_text('Page 1 of 1')
                assert all(not directory.exists() and not output.exists() for directory, _, output in selected)
                assert all(directory.exists() for directory in preserved)
                assert external.read_bytes() == b'Keep the original library file'
                with db() as session:
                    for _, state, _ in selected:
                        assert session.get(database.GalleryImage, state['id']) is None
                        assert session.get(database.GalleryAlbum, state['id']).cover_id is None
                requests = [body for path, body in writes if path.endswith('/history/delete')]
                assert len(requests) == 2 and requests[0] == requests[1]
                assert requests[0]['confirm'] is True
                await workflow.get_by_role('button', name='Close', exact=True).click()
                assert failures == []
            finally:
                await browser.close()
    with client_for(managers) as client:
        asyncio.run(exercise(client))
