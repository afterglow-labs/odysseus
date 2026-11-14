"""Shared desktop/Linux picker previews, keyboard behavior and mobile layout."""
import asyncio
import io
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('family', ['h3', 'bfs'])
@pytest.mark.parametrize('viewport', [dict(width=1440, height=1000), dict(width=390, height=844), dict(width=844, height=390)])
def test_server_images_preview_select_retry_and_cleanup(tmp_path, family, viewport):
    playwright = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome/Chromium required')
    source = Image.new('RGB', (640, 360), '#297b9c')
    ImageDraw.Draw(source).ellipse((430, 30, 530, 130), fill='#f2d675')
    encoded = io.BytesIO(); source.save(encoded, 'JPEG')
    name = 'Boat + <blue> & sky.png'
    selection = dict(root='f', path='Images/' + name, size=8000, mtime_ns='1791523456789012345')
    calls, errors = [], []
    fail_detail = True

    async def run():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            context = await browser.new_context(viewport=viewport)
            await context.add_cookies([dict(name='odysseus_session', value='synthetic-session', url='http://server-picker.test')])
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                nonlocal fail_detail
                req = handler.request; url = urlparse(req.url); query = parse_qs(url.query)
                assert req.method == 'GET', 'Previewing must never upload or queue media'
                if url.path == '/':
                    return await handler.fulfill(content_type='text/html', body='''<html><head><title>Odysseus server images</title><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body><button id="open">Open images</button><div id="chosen"></div></body></html>''')
                if url.path.startswith('/static/'):
                    file = ROOT / url.path.lstrip('/')
                    return await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                assert 'odysseus_session=synthetic-session' in req.headers.get('cookie', '')
                if url.path.endswith('/server-file-roots'):
                    return await handler.fulfill(json={'roots': [dict(id='f', name='F:', available=True)]})
                if url.path.endswith('/server-files'):
                    assert query['kind'] == ['image']
                    entries = [dict(kind='image', name=name, size=8000, selection=selection)]
                    return await handler.fulfill(json=dict(root='f', path='Images', parent_path='', entries=entries, total=1, has_more=False))
                if url.path.endswith('/server-files/image'):
                    assert url.path.startswith(f'/api/video/{family}/')
                    assert {k: query[k][0] for k in selection} == {k: str(v) for k, v in selection.items()}
                    calls.append(query['detail'][0])
                    if query['detail'] == ['true'] and fail_detail:
                        fail_detail = False
                        return await handler.fulfill(status=503, json={'detail': 'Synthetic preview outage. Try again.'})
                    return await handler.fulfill(body=encoded.getvalue(), content_type='image/jpeg', headers={'Cache-Control': 'private, no-store'})
                pytest.fail('Unexpected request: ' + url.path)

            await page.route('**/*', route)
            try:
                await page.goto('http://server-picker.test/')
                assert await page.title() == 'Odysseus server images'
                await page.evaluate('''async family => {
                  window.urls = new Set();
                  const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
                  URL.createObjectURL = blob => { const url = create(blob); urls.add(url); return url; };
                  URL.revokeObjectURL = url => { urls.delete(url); revoke(url); };
                  const {showVideoServerFiles} = await import('/static/js/videoServerFiles.js');
                  const {createServerImageButton} = await import('/static/js/serverImagePreview.js');
                  document.querySelector('#open').onclick = () => {
                    window.picker = showVideoServerFiles({family, kind:'image', multiple:family==='h3', onSelect:files => {
                      window.chosen = files;
                      document.querySelector('#chosen').replaceChildren(createServerImageButton(files[0].serverFile, family));
                    }}, document.querySelector('#open'));
                  };
                }''', family)
                await page.get_by_role('button', name='Open images', exact=True).click()
                picker = page.get_by_role('dialog', name='Server files', exact=True)
                await picker.locator('.video-server-image-thumbnail img').wait_for()
                await page.wait_for_function("document.querySelector('.video-server-image-thumbnail img')?.naturalWidth > 0")
                assert calls == ['false'], 'Only thumbnails load while browsing'
                assert await picker.locator('.video-server-image-thumbnail small').is_hidden()
                await picker.screenshot(path=str(tmp_path / 'browser.png'))
                preview_button = picker.get_by_role('button', name='Preview ' + name, exact=True)
                await preview_button.click()
                preview = page.get_by_role('dialog', name='Image preview', exact=True)
                await preview.get_by_text('Synthetic preview outage. Try again.').wait_for()
                assert await picker.evaluate('(node) => node.inert')
                assert await preview.get_by_role('button', name='Select image', exact=True).is_enabled()
                await preview.get_by_role('button', name='Try again', exact=True).click()
                await preview.locator('img:not([hidden])').wait_for()
                await preview.screenshot(path=str(tmp_path / 'preview.png'))
                assert await preview.evaluate('(node) => node.scrollWidth <= node.clientWidth')
                box = await preview.bounding_box()
                assert box['y'] >= 0 and box['y'] + box['height'] <= viewport['height'] + 1
                await page.keyboard.press('Escape')
                assert await preview.count() == 0
                assert await preview_button.evaluate('(node) => node === document.activeElement')
                assert not await picker.evaluate('(node) => node.inert')
                await preview_button.click()
                await preview.get_by_role('button', name='Select image', exact=True).click()
                choice = picker.get_by_role('checkbox' if family == 'h3' else 'radio')
                assert await choice.is_checked()
                await picker.locator('.video-server-files-selected summary').click()
                tray = picker.locator('.video-server-files-selected')
                await tray.get_by_role('button', name='Preview ' + name, exact=True).click()
                await preview.get_by_role('button', name='Deselect image', exact=True).click()
                assert not await choice.is_checked()
                await choice.check()
                await picker.get_by_role('button', name='Use 1 image' if family == 'h3' else 'Use image', exact=True).click()
                assert await page.evaluate('window.chosen[0].serverFile') == selection
                assert await page.evaluate('window.chosen[0] instanceof Blob') is False
                chosen_button = page.locator('#chosen').get_by_role('button', name='Preview ' + name, exact=True)
                assert await chosen_button.evaluate('(button) => button.clientHeight >= button.querySelector(".video-server-image-thumbnail").offsetHeight')
                await page.locator('#chosen').get_by_role('button', name='Preview ' + name, exact=True).click()
                await preview.locator('img:not([hidden])').wait_for()
                assert await preview.get_by_role('button', name='Select image', exact=True).count() == 0
                await preview.get_by_role('button', name='Done', exact=True).click()
                assert await page.evaluate('window.urls.size') == 0
                assert await page.evaluate("import('/static/js/escMenuStack.js').then(m => m._openMenuCount())") == 0
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert not errors, errors
            finally:
                await browser.close()
    asyncio.run(run())
