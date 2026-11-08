"""Render the real image editor and exercise uploads at desktop/phone widths."""
import asyncio
from email.parser import BytesParser
from email.policy import default
import io
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

from PIL import Image
import pytest

from src import image_workflows as workflows

ROOT = Path(__file__).resolve().parents[1]
ADA = "GPU-11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("width", [1440, 390])
def test_image_editor_4090_mask_upload_and_retry_identity(width, monkeypatch):
    pw = pytest.importorskip("playwright.async_api")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome required")
    monkeypatch.setattr(workflows, "gpu_telemetry", lambda: {"gpus": [{"id": "GPU-22222222-2222-2222-2222-222222222222", "name": "RTX 5090", "memory_total_mib": 32768}, {"id": ADA, "name": "RTX 4090", "memory_total_mib": 24576}]})
    manager = workflows.ImageJobManager(defaults_file="/nonexistent-image-workflow-test-defaults")
    monkeypatch.setattr(manager, "components", lambda recipe: workflows.RECIPES[recipe]["components"])
    inventory = manager.inventory()
    uploads, errors, jobs = [], [], []
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), "blue").save(buffer, format="PNG")
    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": width, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            async def route(handler):
                req = handler.request
                path = urlparse(req.url).path
                if path == "/":
                    await handler.fulfill(content_type="text/html", body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith("/static/"):
                    file = ROOT / path.lstrip("/")
                    await handler.fulfill(body=file.read_bytes(), content_type="text/javascript" if file.suffix == ".js" else mimetypes.guess_type(file.name)[0])
                elif path.endswith("/inventory"):
                    await handler.fulfill(json=inventory)
                elif path.endswith("/jobs") and req.method == "POST":
                    msg = BytesParser(policy=default).parsebytes(b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.post_data_buffer)
                    parts = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True) for part in msg.iter_parts()}
                    cfg = json.loads(parts["config"])
                    uploads.append((req.headers["x-odysseus-submission-id"], cfg, parts))
                    if len(uploads) == 1:
                        await handler.fulfill(status=503, json={"detail": "Connection interrupted; retry the same submission"})
                    else:
                        jobs[:] = [{"id": "a" * 32, "status": "queued", "phase": "Waiting for GPU", "config": cfg}]
                        await handler.fulfill(json=jobs[0])
                elif path.endswith("/jobs"):
                    await handler.fulfill(json={"jobs": jobs})
                else:
                    await handler.fulfill(status=404, json={})
            await page.route("**/*", route)
            try:
                await page.goto("https://image-workflows.test/")
                await page.evaluate("async () => (await import('/static/js/imageWorkflows.js')).showImageWorkflows('lustify_inpaint')")
                assert await page.get_by_label("GPU", exact=True).input_value() == ADA
                await page.get_by_label("Prompt", exact=True).fill("A ceramic vase with flowers")
                await page.get_by_label("Source image", exact=True).set_input_files({"name": "source.png", "mimeType": "image/png", "buffer": buffer.getvalue()})
                await page.locator("canvas").first.wait_for(state="visible")
                await page.get_by_role("button", name="Paint entire image", exact=True).click()
                await page.get_by_role("button", name="Queue image", exact=True).click()
                await page.get_by_role("alert").filter(has_text="Connection interrupted").wait_for()
                await page.get_by_role("button", name="Queue image", exact=True).click()
                await page.get_by_text("queued · Waiting for GPU", exact=True).wait_for()
                assert len(uploads) == 2 and uploads[0][0] == uploads[1][0]
                cfg, parts = uploads[-1][1:]
                assert cfg["gpu"] == ADA and cfg["mode"] == "inpaint"
                with Image.open(io.BytesIO(parts["mask"])) as mask:
                    assert mask.convert("L").getextrema() == (255, 255)
                assert await page.get_by_text("No images queued for this workflow yet.").count() == 0
                await page.get_by_label("Image workflow", exact=True).select_option("snofs_krea")
                assert await page.get_by_label("GPU", exact=True).input_value() == ADA
                assert await page.get_by_label("Steps", exact=True).input_value() == "8"
                assert await page.get_by_label("CFG", exact=True).input_value() == "1"
                assert await page.get_by_label("Source image", exact=True).is_hidden()
                await page.get_by_label("Prompt", exact=True).fill("A wooden cabin beside an alpine lake")
                await page.locator('.h3-video-scroll').evaluate("node => node.scrollTop = 0")
                output = ROOT / "outputs/qa"; output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / f"image-workflows-{width}.png"))
                overflow = await page.locator('.h3-video-scroll').evaluate("node => node.scrollWidth > node.clientWidth + 1")
                assert not overflow
                await page.get_by_label("Image workflow", exact=True).select_option("qwen_rapid")
                assert await page.get_by_label("GPU", exact=True).input_value() == ADA
                assert await page.get_by_label("Steps", exact=True).input_value() == "4"
                await page.get_by_role("button", name="Close", exact=True).click()
                assert not errors
            finally:
                await browser.close()
    asyncio.run(exercise())
