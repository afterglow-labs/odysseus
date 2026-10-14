"""Metadata-only server picker navigation/selection with real desktop/mobile DOM."""
import asyncio
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_server_picker_navigation_selection_and_mobile(tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome/Chromium required')
    requests, errors = [], []

    def video(root, path, size=23456789):
        return {'kind': 'video', 'name': path.split('/')[-1], 'size': size,
                'selection': {'root': root, 'path': path, 'size': size, 'mtime_ns': '123'}}

    tree = {
        ('e', ''): [{'kind': 'directory', 'name': 'Films', 'path': 'Films'}, video('e', 'root-e.mp4')],
        ('e', 'Films'): [{'kind': 'directory', 'name': 'Season 01', 'path': 'Films/Season 01'},
                         *[video('e', f'Films/scene-{n:02}.mp4') for n in range(1, 5)],
                         video('e', 'Films/A very long reference video filename that should stay readable without hiding its full name.mp4', 2345678901)],
        ('e', 'Films/Season 01'): [video('e', 'Films/Season 01/episode.mp4')],
        ('f', ''): [{'kind': 'directory', 'name': 'Clips', 'path': 'Clips'}, video('f', 'root-f.mp4')],
        ('f', 'Clips'): [video('f', 'Clips/mobile.mp4')],
    }

    async def run():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                req = handler.request
                path = urlparse(req.url).path
                assert req.method == 'GET', 'File picker must never upload files or start jobs'
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body></body></html>')
                elif path.startswith('/static/'):
                    source = ROOT / path.lstrip('/')
                    await handler.fulfill(body=source.read_bytes(), content_type='text/javascript' if source.suffix == '.js' else mimetypes.guess_type(source.name)[0])
                elif path.endswith('/server-file-roots'):
                    await handler.fulfill(json={'roots': [{'id': root, 'name': root.upper()+':', 'available': True} for root in ('e', 'f')]})
                elif path.endswith('/server-files'):
                    query = parse_qs(urlparse(req.url).query)
                    root, folder = query['root'][0], query.get('path', [''])[0]
                    search, offset = query.get('search', [''])[0], int(query['offset'][0])
                    requests.append((root, folder, search, offset))
                    rows = [e for e in tree[(root, folder)] if search.lower() in e['name'].lower()]
                    batch = rows[offset:offset+4]
                    await handler.fulfill(json={'root': root, 'path': folder,
                                                'parent_path': folder.rsplit('/', 1)[0] if '/' in folder else '' if folder else None,
                                                'entries': batch, 'total': len(rows), 'has_more': offset+4 < len(rows),
                                                'next_offset': offset+4 if offset+4 < len(rows) else None})
                else:
                    pytest.fail('Unexpected request: '+path)

            await page.route('**/*', route)
            try:
                await page.goto('http://server-picker.test/')
                async def open_picker(multiple=True):
                    await page.evaluate("async multiple=>{window.chosen=null;window.picker=(await import('/static/js/videoServerFiles.js')).showVideoServerFiles({family:'h3',multiple,onSelect:files=>window.chosen=files});}", multiple)
                    await page.get_by_role('button', name='Open folder Films', exact=True).wait_for()
                await open_picker()
                panel = page.get_by_role('dialog', name='Server files', exact=True)
                crumbs = panel.get_by_role('navigation', name='Folder breadcrumb', exact=True)
                await panel.get_by_role('button', name='Open folder Films', exact=True).click()
                await panel.get_by_role('checkbox', name='scene-01.mp4', exact=False).check()
                await panel.get_by_role('button', name='Open folder Season 01', exact=True).click()
                await panel.get_by_role('checkbox', name='episode.mp4', exact=False).check()
                await panel.get_by_role('button', name='Up', exact=True).click()
                await panel.get_by_role('checkbox', name='scene-01.mp4', exact=False).wait_for()
                assert await panel.get_by_role('checkbox', name='scene-01.mp4', exact=False).is_checked()
                await crumbs.get_by_role('button', name='E:', exact=True).click()
                await panel.get_by_role('button', name='Open folder Films', exact=True).wait_for()
                await panel.get_by_role('button', name='Back', exact=True).click()
                await panel.get_by_role('button', name='Load more', exact=True).click()
                await panel.get_by_role('checkbox', name='scene-04.mp4', exact=False).wait_for()
                await panel.get_by_role('button', name='Select shown videos', exact=True).click()
                assert await panel.get_by_role('checkbox').count() == 5
                assert await panel.get_by_role('checkbox', checked=True).count() == 5
                await panel.get_by_role('button', name='Deselect shown videos', exact=True).click()
                assert await panel.get_by_role('checkbox', checked=True).count() == 0
                await panel.get_by_role('button', name='Select shown videos', exact=True).click()
                await panel.locator('.video-server-files-selected summary').click()
                await panel.get_by_role('button', name='Remove episode.mp4 from selection', exact=True).click()
                await page.wait_for_timeout(300)  # Wait for the existing details reveal animation.
                await panel.screenshot(path=str(tmp_path/'server-files-desktop.png'))
                for dimensions in ({'width':1000,'height':550}, {'width':844,'height':390}):
                    await page.set_viewport_size(dimensions)
                    await panel.screenshot(path=str(tmp_path/f"server-files-{dimensions['width']}x{dimensions['height']}.png"))
                    footer = await panel.locator('.video-server-files-footer').bounding_box()
                    assert footer['y'] + footer['height'] <= dimensions['height']
                    assert (await panel.locator('.video-server-files-browser').bounding_box())['height'] >= 50
                await page.set_viewport_size({'width':1440,'height':1000})
                await panel.get_by_role('combobox', name='Drive', exact=True).select_option('f')
                await panel.get_by_role('checkbox', name='root-f.mp4', exact=False).check()
                assert await panel.get_by_role('button', name='Use 6 videos', exact=True).is_enabled()
                await panel.get_by_role('button', name='Remove scene-01.mp4 from selection', exact=True).click()
                await panel.get_by_role('button', name='Use 5 videos', exact=True).click()
                chosen = await page.evaluate('window.chosen')
                assert len(chosen) == 5
                assert {f['serverFile']['root'] for f in chosen} == {'e', 'f'}
                assert not any(f['name'] in ('scene-01.mp4', 'episode.mp4') for f in chosen)
                # Mobile: actual folder and drive navigation, search, and selection.
                await page.set_viewport_size({'width':390,'height':844})
                await open_picker()
                await panel.get_by_role('button', name='Open folder Films', exact=True).click()
                await panel.get_by_role('searchbox', name='Search server folder', exact=True).fill('scene-02')
                await panel.get_by_text('1 of 1 items matching this search', exact=True).wait_for()
                await panel.get_by_role('checkbox', name='scene-02.mp4', exact=False).check()
                await panel.get_by_role('combobox', name='Drive', exact=True).select_option('f')
                await panel.get_by_role('button', name='Open folder Clips', exact=True).click()
                await panel.get_by_role('checkbox', name='mobile.mp4', exact=False).check()
                await panel.locator('.video-server-files-selected summary').click()
                assert await panel.get_by_role('button', name='Remove scene-02.mp4 from selection', exact=True).is_visible()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                footer = await panel.locator('.video-server-files-footer').bounding_box()
                assert footer['y'] + footer['height'] <= 844
                await page.wait_for_timeout(300)
                await panel.screenshot(path=str(tmp_path/'server-files-mobile.png'))
                await panel.get_by_role('button', name='Use 2 videos', exact=True).click()
                assert {f['name'] for f in await page.evaluate('window.chosen')} == {'scene-02.mp4','mobile.mp4'}
                # Single input still returns exactly one descriptor across drives.
                await open_picker(False)
                await panel.get_by_role('radio', name='root-e.mp4', exact=False).check()
                await panel.get_by_role('combobox', name='Drive', exact=True).select_option('f')
                await panel.get_by_role('radio', name='root-f.mp4', exact=False).check()
                await panel.get_by_role('button', name='Use video', exact=True).click()
                assert len(await page.evaluate('window.chosen')) == 1
                assert await page.evaluate('window.chosen[0].serverFile.root') == 'f'
                assert ('e','Films','',4) in requests
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(run())
