"""The copyable example stays out of the draft on both desktop and phone widths."""
import asyncio
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('width', [390, 1440])
def test_lora_example_is_selectable_copyable_and_never_edits_the_draft(width):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')

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
                example = 'subject_definitions:\n<Subject 1> follows <Picture 1>.\n\n' + 'A camera follows the character.\n' * 100
                await page.evaluate('''async text => {
                    Object.defineProperty(navigator, 'clipboard', {value: {writeText: async value => {window.copied = value;}}});
                    const {createH3LoraEditor} = await import('/static/js/h3Loras.js');
                    const editor = createH3LoraEditor({components:[{id:'lora',name:'Third-person View',role:'lora',lora_profile:{prompt_example:text}}],value:[{id:'lora',strength:.7}]});
                    document.body.append(editor.node);
                }''', example)
                assert await page.locator('dialog').count() == 0
                await page.get_by_role('button', name='Example prompt', exact=True).click()
                dialog = page.get_by_role('dialog', name='LoRA example prompt')
                await dialog.wait_for(state='visible')
                content = dialog.get_by_role('textbox', name='Example prompt text')
                assert await content.input_value() == example
                assert await content.evaluate('e => e.readOnly')
                await content.select_text()
                assert await content.evaluate('e => e.selectionEnd - e.selectionStart') == len(example)
                await dialog.get_by_role('button', name='Copy prompt', exact=True).click()
                assert await page.evaluate('window.copied') == example
                await page.keyboard.press('Escape')
                assert await dialog.is_visible()
                bounds = await dialog.bounding_box()
                assert 0 <= bounds['x'] and bounds['x'] + bounds['width'] <= width
                await dialog.get_by_role('button', name='Close', exact=True).click()
                assert await page.locator('dialog').count() == 0
                assert await page.locator('#draft').input_value() == 'Keep my draft'
                assert not errors
            finally:
                await browser.close()
    asyncio.run(exercise())
