"""Exercise every catalog entry in the actual shared picker and help dialog."""
import asyncio
import json
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width', [390, 1440])
def test_all_loras_show_readonly_help_including_empty_profiles(width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')
    catalog = json.loads((ROOT / 'scripts/h3_lora_help.json').read_text())

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': width, 'height': 850})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            async def route(handler):
                path = urlparse(handler.request.url).path
                if path.startswith('/static/'):
                    source = ROOT / path.lstrip('/')
                    await handler.fulfill(body=source.read_bytes(), content_type='text/css' if source.suffix == '.css' else 'text/javascript')
                else:
                    await handler.fulfill(content_type='text/html', body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/h3-video.css"></head><body><textarea id="draft">Keep my draft</textarea></body></html>')
            await page.route('**/*', route)
            try:
                await page.goto('http://fixture.test/')
                await page.evaluate('''async () => {
                    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {window.copied = value;}}});
                    const {createH3LoraEditor} = await import('/static/js/h3Loras.js');
                    window.edits = 0;
                    window.editor = createH3LoraEditor({onChange: () => window.edits++});
                    document.body.append(window.editor.node);
                }''')
                for name, help in catalog.items():
                    await page.evaluate('''({name, help}) => {
                        editor.setInventory([{id:'adapter', name, role:'lora', lora_profile:{}, lora_help:help}]);
                        editor.setValue([{id:'adapter', strength:.6}]);
                    }''', {'name': name, 'help': help})
                    await page.get_by_role('button', name='About this LoRA', exact=True).click()
                    dialog = page.get_by_role('dialog', name='LoRA help', exact=True)
                    await dialog.wait_for(state='visible')
                    assert await dialog.get_by_text(help['summary'], exact=True).count() == 1
                    assert await dialog.get_by_text(help['trigger'], exact=True).count() == 1
                    assert await dialog.get_by_role('link').count() == len(help['sources'])
                    if help['trigger_words']:
                        await dialog.get_by_role('button', name='Copy trigger', exact=True).click()
                        assert await page.evaluate('window.copied') == help['trigger_words']
                    bounds = await dialog.bounding_box()
                    assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width
                    assert await dialog.evaluate('e => e.scrollWidth <= e.clientWidth')
                    await dialog.get_by_role('button', name='Close', exact=True).click()
                    assert await page.evaluate('editor.getValue()') == [{'id': 'adapter', 'strength': .6}]
                assert await page.evaluate('window.edits') == 0
                assert await page.locator('#draft').input_value() == 'Keep my draft'
                assert not errors
            finally:
                await browser.close()
    asyncio.run(exercise())
