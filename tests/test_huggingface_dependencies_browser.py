"""Exercise the real dependency UI and install handler without installing packages."""

import asyncio
import json
import mimetypes
from pathlib import Path
import re
import shutil
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_huggingface_update_discovery_and_captured_environment(tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome/Chromium required")
    # Browser plugin not available. Keep the real coordinator/dialog/actions,
    # with unrelated Cookbook modules stubbed and every API request intercepted.
    source = (ROOT / "static/js/cookbook.js").read_text()
    imports = {}
    for statement, name in re.findall(r"import\s+([\s\S]*?)\s+from\s+'\./([^']+)'", source):
        statement = statement.strip()
        if statement.startswith("{"):
            imports[name] = [item.strip() for item in statement.strip("{}").split(",") if item.strip()]
        elif not statement.startswith("*"):
            imports[name] = ["default"]
    real = {"cookbook.js", "cookbookMaintenance.js", "cookbookDependencyHealth.js",
            "cookbook-deps-recipes.js", "escMenuStack.js", "toolWindowZOrder.js"}
    submissions, errors = [], []

    def stub(name):
        values = {key: "()=>{}" for key in imports.get(name, [])}
        if name == "ui.js":
            values["default"] = "{esc:v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('\"','&quot;'),showToast:v=>window.toasts.push(v)}"
        elif name == "spinner.js":
            values["default"] = "{createWhirlpool:()=>({element:document.createElement('span')})}"
        elif name == "providers.js":
            values["providerLogo"] = "()=>''"
        elif name == "cookbook-hwfit.js":
            values["_cachedModelIds"] = "new Set()"
            values["_hwfitCache"] = "{}"
        elif name == "cookbookRunning.js":
            values["_addTask"] = "(...args)=>window.startedTasks.push(args)"
        return "\n".join(f"export default {value};" if key == "default" else f"export const {key}={value};"
                         for key, value in values.items())

    async def run():
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": 1365, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)

            async def route(handler):
                request = handler.request
                parsed = urlparse(request.url)
                path, query = parsed.path, parse_qs(parsed.query)
                remote = query.get("host", [""])[0]
                executable = "/opt/remote env/bin/python3" if remote else "/home/corey/projects/odysseus/venv/bin/python"
                if path == "/":
                    await handler.fulfill(content_type="text/html", body="""<!doctype html><html><head>
                      <meta name="viewport" content="width=device-width,initial-scale=1"><title>Dependencies fixture</title>
                      <link rel="stylesheet" href="/static/style.css"></head><body>
                      <main data-backend-group="Dependencies" style="width:calc(100% - 32px);max-width:900px;margin:24px auto;">
                        <h1>Dependencies</h1><label>Server <select id="hwfit-deps-server"><option value="local">Local</option><option value="remote-box">Remote box</option></select></label>
                        <button id="cookbook-check-dependencies">Check dependencies</button>
                        <button id="cookbook-manage-packages">Manage packages</button>
                        <p id="cookbook-deps-check-status"></p><div id="cookbook-deps-list"></div>
                      </main><script>window.toasts=[];window.startedTasks=[];</script></body></html>""")
                elif path == "/api/cookbook/packages":
                    await handler.fulfill(json={"packages": [{
                        "name": "huggingface_hub", "pip": "huggingface_hub", "category": "Tools", "target": "remote",
                        "installed": True, "installed_version": "1.33.0", "python_executable": executable,
                        "desc": "Hugging Face downloads and hf CLI. Update selects the latest compatible version.",
                        "status_note": "Installed dependencies currently require Hugging Face Hub <2.",
                        "install_supported": True,
                    }]})
                elif path == "/api/cookbook/environment-packages":
                    checked = query.get("check_updates", ["false"])[0] == "true"
                    await handler.fulfill(json={"python_version": "3.14.0", "executable": executable,
                                               "updates_checked": checked, "packages": [
                        {"name": "huggingface-hub", "version": "1.33.0", **({"latest_version": "2.2.0", "update_available": True} if checked else {})},
                        {"name": "pip", "version": "26.2"},
                    ]})
                elif path == "/api/model/serve":
                    assert request.method == "POST"
                    submissions.append(json.loads(request.post_data))
                    await handler.fulfill(json={"ok": True, "session_id": f"fixture-hf-{len(submissions)}"})
                elif path.startswith("/static/js/"):
                    name = path.rsplit("/", 1)[-1]
                    body = (ROOT / path.lstrip("/")).read_text() if name in real else stub(name)
                    if name == "cookbook.js":
                        body += "\nexport { _fetchDependencies };"
                    await handler.fulfill(content_type="text/javascript", body=body)
                elif path.startswith("/static/"):
                    asset = ROOT / path.lstrip("/")
                    await handler.fulfill(body=asset.read_bytes(), content_type=mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
                elif path == "/favicon.ico":
                    await handler.fulfill(status=204)
                else:
                    pytest.fail("Unexpected request: " + path)

            await page.route("**/*", route)
            try:
                await page.goto("http://huggingface-dependencies.test/")
                await page.evaluate("""async()=>{
                  window.app=await import('/static/js/cookbook.js');
                  Object.assign(app._envState,{hostPlatform:'linux',env:'none',remoteHost:'',envPath:'',
                    servers:[{host:'remote-box',name:'Remote box',port:'2224',env:'venv',envPath:'/opt/remote env',platform:'linux'}]});
                  await app._fetchDependencies();
                }""")
                assert await page.title() == "Dependencies fixture"
                row = page.locator('.cookbook-dep-row[data-pkg-name="huggingface_hub"]')
                await row.get_by_text("Hugging Face Hub / hf CLI", exact=True).wait_for()
                assert "Installed: 1.33.0" in await row.inner_text()
                assert "/home/corey/projects/odysseus/venv/bin/python" in await row.inner_text()
                update = row.get_by_role("button", name="Update Hugging Face Hub / hf CLI", exact=True)
                assert await update.inner_text() == "Update Hugging Face"
                assert "latest compatible" in await update.get_attribute("title")
                assert await update.evaluate("el=>el.scrollWidth<=el.clientWidth")
                await page.screenshot(path=str(tmp_path / "hf-dependencies-desktop.png"))
                await update.click()
                await page.wait_for_function("window.startedTasks.length===1")
                assert submissions[-1]["repo_id"] == "huggingface_hub"
                assert not submissions[-1].get("remote_host")
                assert "pip install -U" in submissions[-1]["cmd"]
                assert submissions[-1]["cmd"].endswith("'huggingface_hub'")

                await page.get_by_role("button", name="Manage packages", exact=True).click()
                dialog = page.get_by_role("dialog", name="Manage packages", exact=True)
                await dialog.get_by_text("Python 3.14.0 · /home/corey/projects/odysseus/venv/bin/python", exact=True).wait_for()
                for alias in ("hf", "hf-cli", "huggingface_hub", "Hugging Face"):
                    await dialog.get_by_role("searchbox", name="Search installed packages").fill(alias)
                    assert await dialog.locator("[data-package]").count() == 1
                    assert await dialog.get_by_role("button", name="Update Hugging Face Hub / hf CLI", exact=True).is_enabled()
                await dialog.get_by_role("button", name="Check for updates", exact=True).click()
                await dialog.get_by_text("Installed: 1.33.0 · Latest release: 2.2.0 (compatibility checked on update)", exact=True).wait_for()
                assert "1.33.0 → 2.2.0" not in await dialog.inner_text()
                await page.set_viewport_size({"width": 390, "height": 844})
                await dialog.screenshot(path=str(tmp_path / "hf-packages-mobile.png"))
                assert await dialog.evaluate("el=>el.scrollWidth<=el.clientWidth")
                await dialog.get_by_role("button", name="Close", exact=True).click()
                await page.screenshot(path=str(tmp_path / "hf-dependencies-mobile.png"))
                assert await row.evaluate("el=>el.scrollWidth<=el.clientWidth")

                # The catalog now describes the selected remote interpreter.
                await page.locator("#hwfit-deps-server").select_option("remote-box")
                await page.evaluate("app._fetchDependencies()")
                assert "/opt/remote env/bin/python3" in await row.inner_text()
                await page.get_by_role("button", name="Manage packages", exact=True).click()
                await dialog.get_by_text("Python 3.14.0 · /opt/remote env/bin/python3", exact=True).wait_for()
                await dialog.get_by_role("searchbox", name="Search installed packages").fill("hf-cli")
                # Changing the underlying selector cannot redirect an open dialog.
                await page.evaluate("document.getElementById('hwfit-deps-server').value='local';app._envState.remoteHost='' ")
                await dialog.get_by_role("button", name="Update Hugging Face Hub / hf CLI", exact=True).click()
                await page.wait_for_function("window.startedTasks.length===2")
                assert submissions[-1]["remote_host"] == "remote-box"
                assert submissions[-1]["ssh_port"] == "2224"
                assert submissions[-1]["repo_id"] == "huggingface-hub"
                assert submissions[-1]["cmd"] == "'/opt/remote env/bin/python3' -m pip install -U 'huggingface-hub'"
                assert await dialog.get_by_role("button", name="Update Hugging Face Hub / hf CLI", exact=True).is_disabled()
                assert "Progress is in Cookbook → Active" in await dialog.get_by_role("status").inner_text()
                assert not errors
                for filename in ("hf-dependencies-desktop.png", "hf-packages-mobile.png", "hf-dependencies-mobile.png"):
                    shutil.copyfile(tmp_path / filename, Path("/tmp") / f"odysseus-{filename}")
            finally:
                await browser.close()

    asyncio.run(run())
