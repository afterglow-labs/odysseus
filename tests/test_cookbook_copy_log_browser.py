"""The real task menu must copy the requested log history, not its short card tail."""

import asyncio
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("width,scenario", [(1280, "live"), (390, "live"), (1280, "saved"), (390, "short"), (1280, "windows")])
def test_copy_last_50_fetches_history_and_reports_actual_count(width, scenario):
    playwright = pytest.importorskip("playwright.async_api")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome/Chromium required")
    commands, errors = [], []
    secret = "hf_" + "A" * 26
    lines = [f"line {i:02d}: " + "detail " * 22 for i in range(70)]
    lines[-1] = f"final line, token={secret}"
    snapshot = "\r\n".join(lines) + "\r\n" * 30
    expected = "\n".join(lines[-50:]).replace(secret, "[redacted]")
    if scenario == "short":
        snapshot = "first line\nsecond line\n\n\n"
        expected = "first line\nsecond line"
    env = {"servers": [{"host": "", "name": "Local"}], "remoteHost": ""}

    async def run():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
            context = await browser.new_context(viewport={"width": width, "height": 900}, permissions=["clipboard-read", "clipboard-write"])
            page = await context.new_page()
            page.set_default_timeout(5000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(handler):
                path = urlparse(handler.request.url).path
                if path == "/":
                    await handler.fulfill(content_type="text/html", body='''<!doctype html><html><head>
                      <meta name="viewport" content="width=device-width,initial-scale=1"><title>Cookbook log copy</title>
                      <link rel="stylesheet" href="/static/style.css"></head><body>
                      <div id="cookbook-modal"><div class="cookbook-body"><div class="cookbook-tabs">
                        <button class="cookbook-tab active" data-backend="Running">Active</button>
                      </div></div></div><div id="copy-toast" role="status"></div>
                      <script>window.toasts=[];</script></body></html>''')
                elif path == "/api/cookbook/state":
                    await handler.fulfill(json={"env": env, "tasks": [], "presets": []})
                elif path == "/api/cookbook/tasks/status":
                    await handler.fulfill(json={"tasks": []})
                elif path == "/api/shell/exec":
                    command = json.loads(handler.request.post_data)["command"]
                    commands.append(command)
                    assert "capture-pane" in command or "Get-Content" in command
                    await handler.fulfill(json={"exit_code": 1 if scenario == "saved" else 0, "stdout": "" if scenario == "saved" else snapshot, "stderr": "session no longer exists" if scenario == "saved" else ""})
                elif path == "/static/js/ui.js":
                    await handler.fulfill(content_type="text/javascript", body="const show=v=>{window.toasts.push(v);document.querySelector('#copy-toast').textContent=v;};export default {showToast:show,showError:show};")
                elif path == "/static/js/cookbook-diagnosis.js":
                    await handler.fulfill(content_type="text/javascript", body="export const _diagnose=()=>null,_showDiagnosis=()=>{},_clearDiagnosis=()=>{};")
                elif path == "/static/js/cookbookGpu.js":
                    await handler.fulfill(content_type="text/javascript", body="export const clearGpuMemory=()=>{};")
                elif path.startswith("/static/"):
                    source = ROOT / path.lstrip("/")
                    await handler.fulfill(body=source.read_bytes(), content_type="text/javascript" if source.suffix == ".js" else mimetypes.guess_type(source.name)[0])
                elif path == "/favicon.ico":
                    await handler.fulfill(status=204)
                else:
                    pytest.fail("Unexpected request: " + path)

            await page.route("**/*", route)
            try:
                await page.goto("http://localhost/")
                await page.evaluate('''async ({env,windows})=>{
                  window.running=await import('/static/js/cookbookRunning.js');
                  const noop=()=>{};
                  running.initRunning({_envState:env,_isWindows:()=>windows,_getPort:()=>'',_getPlatform:()=>windows?'windows':'linux',
                    _serverByVal:()=>null,_loadPresets:()=>[],_savePresets:noop,modelLogo:()=>'',esc:v=>String(v??''),
                    _sshPrefix:()=>'',_copyText:text=>navigator.clipboard.writeText(text),_refreshDependencies:noop});
                  await running._syncFromServer();
                }''', {"env": env, "windows": scenario == "windows"})
                task = {"id": "cookbook-copy", "sessionId": "cookbook-copy", "name": "Orca download", "type": "download", "status": "done", "output": "old card line\nlatest card line", "remoteHost": "", "payload": {"repo_id": "fixture/orca"}}
                await page.evaluate("task=>{localStorage.setItem('cookbook-tasks',JSON.stringify([task]));running._renderRunningTab();}", task)
                await page.locator('.cookbook-tab[data-backend="Running"]').click()
                await page.evaluate("document.querySelector('.cookbook-group[data-backend-group=Running]').classList.remove('hidden')")
                if scenario == "saved":
                    # The already-rendered card is short; the saved task holds
                    # more history and must remain usable after tmux exits.
                    await page.evaluate("output=>{const tasks=JSON.parse(localStorage.getItem('cookbook-tasks'));tasks[0].output=output;localStorage.setItem('cookbook-tasks',JSON.stringify(tasks));}", snapshot)
                assert await page.title() == "Cookbook log copy"
                assert page.url == "http://localhost/"
                card = page.locator('[data-task-id="cookbook-copy"]')
                await card.locator('.cookbook-task-menu-btn').click()
                button = page.get_by_text('Copy last 50 lines', exact=True)
                await button.wait_for(state="visible")
                if scenario == "live":
                    await page.screenshot(path=f"/tmp/odysseus-copy-log-{width}.png")
                await button.click()
                await page.wait_for_function("window.toasts.some(t=>t.startsWith('Copied'))")
                copied = await page.evaluate("navigator.clipboard.readText()")
                assert copied == expected
                assert len(commands) == 1
                assert "-Tail 500" in commands[0] if scenario == "windows" else "capture-pane" in commands[0]
                assert secret not in copied
                count = 2 if scenario == "short" else 50
                assert str(count) in await page.locator('#copy-toast').inner_text()
                if scenario == "saved":
                    assert "saved" in await page.locator('#copy-toast').inner_text()
                assert not errors
            finally:
                await context.close()
                await browser.close()

    asyncio.run(run())
