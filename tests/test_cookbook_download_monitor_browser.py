"""Real task cards must update/advance downloads without clicking Start now."""

import asyncio
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", [
    "collapsed", "hidden", "return-to-active", "terminal-snapshot", "terminal-exit", "terminal-stale-progress",
    "pending-launch", "preflight", "preflight-gone", "long-capture",
])
def test_download_monitor_independent_of_card_visibility(tmp_path, scenario):
    playwright = pytest.importorskip("playwright.async_api")
    chrome = shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome:
        pytest.skip("Chrome/Chromium required")
    captures, launches, kills, errors = [], [], [], []
    live, server_tasks = [], []
    preflight = "Downloading 'model.gguf' to '/fixture/snapshots/revision/model.gguf'\nDownload complete\nFetching 1 files: 100%|1/1\nDownloading bytes: 0.00B"
    snapshots = {"output": "Downloading bytes: 5 MB / 20 MB"}
    if scenario in ("terminal-snapshot", "terminal-stale-progress"):
        snapshots["output"] = "Fetching 1 files: 0%|0/1\nDOWNLOAD_OK"
    elif scenario == "terminal-exit":
        snapshots["output"] = "Fetching model\n=== Process exited with code 0 ==="
    elif scenario.startswith("preflight"):
        snapshots["output"] = preflight
    elif scenario == "long-capture":
        snapshots["output"] = "\n".join(f"model-file-{i:03}: Loading detailed transfer information" for i in range(500))
    shared_env = {"servers": [{"host": "", "name": "Local"}], "remoteHost": "", "localHfCacheDir": "/fixture/models"}

    async def run():
        launch_started, release_launch = asyncio.Event(), asyncio.Event()
        async with playwright.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=["--no-sandbox"])
            page = await browser.new_page(viewport={"width": 390 if scenario == "collapsed" else 1280, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))
            if scenario == "preflight-gone":
                # Reach the real five-capture-failure path without 15s of sleeps.
                await page.add_init_script("const timeout=window.setTimeout; window.setTimeout=(f,ms,...args)=>timeout(f,ms===3000?1:ms,...args)")

            async def route(handler):
                request = handler.request
                path = urlparse(request.url).path
                if path == "/":
                    await handler.fulfill(content_type="text/html", body="""<!doctype html><html><head>
                      <meta name="viewport" content="width=device-width,initial-scale=1"><title>Download monitor fixture</title>
                      <link rel="stylesheet" href="/static/style.css"></head><body>
                      <div id="cookbook-modal"><div class="cookbook-body"><div class="cookbook-tabs">
                        <button class="cookbook-tab active" data-backend="Search">Download</button>
                        <button class="cookbook-tab" data-backend="Running">Active</button>
                      </div><div class="cookbook-group" data-backend-group="Search"></div></div></div>
                      <script>window.toasts=[];</script></body></html>""")
                elif path == "/api/cookbook/state":
                    await handler.fulfill(json={"env": shared_env, "tasks": server_tasks, "presets": []})
                elif path == "/api/cookbook/tasks/status":
                    await handler.fulfill(json={"tasks": live})
                elif path == "/api/model/download":
                    launches.append(json.loads(request.post_data))
                    launch_started.set()
                    if scenario == "pending-launch":
                        await release_launch.wait()
                    await handler.fulfill(json={"ok": True, "session_id": "cookbook-second"})
                elif path == "/api/shell/exec":
                    command = json.loads(request.post_data)["command"]
                    if "capture-pane" in command:
                        captures.append(command)
                    if "kill-session" in command:
                        kills.append(command)
                    gone = scenario == "preflight-gone"
                    await handler.fulfill(json={"exit_code": 1 if gone else 0, "stdout": "" if gone else snapshots["output"], "stderr": ""})
                elif path == "/static/js/ui.js":
                    await handler.fulfill(content_type="text/javascript", body="export default {showToast:v=>window.toasts.push(v),showError:v=>window.toasts.push(v)};")
                elif path == "/static/js/cookbook-diagnosis.js":
                    await handler.fulfill(content_type="text/javascript", body="export const _diagnose=()=>null,_showDiagnosis=()=>{},_clearDiagnosis=()=>{};")
                elif path == "/static/js/cookbookGpu.js":
                    await handler.fulfill(content_type="text/javascript", body="export const clearGpuMemory=()=>{};")
                elif path.startswith("/static/"):
                    source = ROOT / path.lstrip("/")
                    body = source.read_bytes()
                    if path.endswith("/cookbookRunning.js"):
                        body += b"\nexport { _pollBackgroundStatus, _stopBackgroundMonitor };"
                    await handler.fulfill(body=body, content_type="text/javascript" if source.suffix == ".js" else mimetypes.guess_type(source.name)[0])
                elif path == "/favicon.ico":
                    await handler.fulfill(status=204)
                else:
                    pytest.fail("Unexpected request: " + path)

            await page.route("**/*", route)
            try:
                await page.goto("http://download-monitor.test/")
                await page.evaluate("""async env=>{
                  window.monitor=await import('/static/js/cookbookRunning.js');
                  const noop=()=>{};
                  monitor.initRunning({_envState:env,_isWindows:()=>false,_getPort:()=>'',_getPlatform:()=> 'linux',
                    _serverByVal:()=>null,_loadPresets:()=>[],_savePresets:noop,modelLogo:()=>'',esc:v=>String(v??''),
                    _sshPrefix:()=>'',_copyText:async()=>{},_refreshDependencies:noop});
                  await monitor._syncFromServer();
                  window.showTab=tab=>{
                    document.querySelectorAll('.cookbook-tab').forEach(n=>n.classList.toggle('active',n.dataset.backend===tab));
                    document.querySelectorAll('.cookbook-group').forEach(n=>n.classList.toggle('hidden',n.dataset.backendGroup!==tab));
                    monitor._renderRunningTab();
                  };
                }""", shared_env)
                first = {"id": "cookbook-first", "sessionId": "cookbook-first", "name": "First download", "type": "download", "status": "running", "output": "Downloading bytes: 0 B", "remoteHost": "", "payload": {"repo_id": "fixture/first"}}
                if scenario == "preflight-gone":
                    first["output"] = preflight
                queue = [{"id": f"queue-{name}", "sessionId": f"queue-{name}", "name": f"{name} download", "type": "download", "status": "queued", "output": "", "remoteHost": "", "payload": {"repo_id": f"fixture/{name}"}}
                         for name in ("second", "third")]
                tasks = [first, *queue] if scenario in ("collapsed", "hidden", "pending-launch", "preflight", "preflight-gone") else [first]
                await page.evaluate("tasks=>localStorage.setItem('cookbook-tasks',JSON.stringify(tasks))", tasks)
                await page.evaluate("showTab('Search')")
                if scenario == "terminal-stale-progress":
                    await page.evaluate("snapshot=>{const card=document.querySelector('[data-task-id=\"cookbook-first\"]');card._lastProgress='0|'+snapshot;card._lastProgressTime=Date.now()-3600000;}", snapshots["output"])

                if scenario == "pending-launch":
                    server_tasks.extend(queue)
                    live.extend([{"session_id": "cookbook-first", "type": "download", "status": "completed", "output_tail": "DOWNLOAD_OK"},
                                 *[{"session_id": row["sessionId"], "type": "download", "status": "crashed", "exit_code": 1} for row in queue]])
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    await asyncio.wait_for(launch_started.wait(), 3)
                    await page.evaluate("showTab('Running');monitor._processQueue()")
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    stored = await page.evaluate("monitor._loadTasks()")
                    assert next(row for row in stored if row["sessionId"] == "queue-second")["status"] == "running"
                    assert next(row for row in stored if row["sessionId"] == "queue-third")["status"] == "queued"
                    assert len(launches) == 1, "In-flight launch must continue to occupy its host's queue slot"
                    assert not any("queue-" in command for command in captures), "Placeholders are not shell sessions"
                    release_launch.set()
                    await page.wait_for_function("monitor._loadTasks().some(t=>t.sessionId==='cookbook-second')")
                    await page.evaluate("async()=>{await monitor._pollBackgroundStatus();monitor._processQueue();}")
                    await page.evaluate("monitor._syncFromServer()")
                    stored = await page.evaluate("monitor._loadTasks()")
                    assert not any(row["sessionId"] == "queue-second" for row in stored), "Stale saved queue entries must stay retired"
                    assert len([row for row in stored if row["sessionId"] == "cookbook-second"]) == 1
                    assert [body["repo_id"] for body in launches] == ["fixture/second"]
                elif scenario in ("collapsed", "hidden"):
                    if scenario == "collapsed":
                        # Preserve an existing card's explicitly collapsed state.
                        await page.evaluate("document.querySelector('[data-task-id=\"cookbook-first\"] .cookbook-task-header').click()")
                        await page.evaluate("showTab('Running')")
                        assert await page.locator('[data-task-id="cookbook-first"] .cookbook-output-wrap').evaluate("el=>el.classList.contains('cookbook-task-collapsed')")
                    live.extend([{"session_id": "cookbook-first", "type": "download", "status": "completed", "output_tail": "DOWNLOAD_OK"},
                                 # Older servers probe queue placeholders as absent tmux sessions.
                                 *[{"session_id": row["sessionId"], "type": "download", "status": "stopped"} for row in queue]])
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    await page.wait_for_function("monitor._loadTasks().some(t=>t.sessionId==='cookbook-second')")
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    await page.evaluate("monitor._processQueue()")
                    stored = await page.evaluate("monitor._loadTasks()")
                    assert stored[0]["status"] == "done"
                    assert "DOWNLOAD_OK" in stored[0]["output"]
                    assert next(row for row in stored if row["sessionId"] == "queue-third")["status"] == "queued"
                    assert not any(row["sessionId"] == "queue-second" for row in stored)
                    assert [body["repo_id"] for body in launches] == ["fixture/second"]
                    assert not any('cookbook-first ' in command for command in captures), "Collapsed and hidden cards must use background status, not live log capture"
                elif scenario == "return-to-active":
                    assert not captures
                    await page.evaluate("showTab('Running')")
                    await page.wait_for_function("document.querySelector('.cookbook-output-pre').textContent.includes('5 MB')")
                    assert len(captures) == 1
                    await page.evaluate("showTab('Running');showTab('Running');showTab('Running')")
                    assert len(captures) == 1, "Rerendering must not create duplicate pollers"
                    await page.evaluate("showTab('Search')")
                    await page.wait_for_function("!document.querySelector('[data-task-id=\"cookbook-first\"]')._abort", timeout=5000)
                    await page.evaluate("showTab('Running')")
                    await page.wait_for_function("document.querySelector('[data-task-id=\"cookbook-first\"]')._abort!==null")
                    await page.wait_for_timeout(150)
                    assert len(captures) == 2, "Returning to an existing card must resume capture"
                    assert await page.locator('.cookbook-task').count() == 1
                elif scenario in ("preflight", "preflight-gone"):
                    await page.evaluate("showTab('Running')")
                    if scenario == "preflight-gone":
                        await page.wait_for_function("monitor._loadTasks()[0].status==='crashed'")
                        assert len([command for command in captures if 'cookbook-first ' in command]) == 5
                    else:
                        await page.wait_for_function("document.querySelector('[data-task-id=\"cookbook-first\"] .cookbook-output-pre').textContent.includes('snapshots/revision')")
                        await page.wait_for_timeout(100)
                        assert (await page.evaluate("monitor._loadTasks()[0].status")) == "running"
                        assert not launches, "Preflight 100% and resolved paths cannot release the next queued download"
                    assert not kills, "Preflight progress cannot kill the still-downloading session"
                elif scenario == "long-capture":
                    await page.evaluate("showTab('Running')")
                    await page.wait_for_function("document.querySelector('.cookbook-output-pre').textContent.includes('model-file-499')")
                    assert await page.locator('.cookbook-output-pre').inner_text() == snapshots["output"]
                    assert len(snapshots["output"]) > 5000
                    assert (await page.evaluate("monitor._loadTasks()[0].output")) == snapshots["output"][-5000:]
                    await page.wait_for_function("document.querySelector('.cookbook-output-pre').textContent.includes('model-file-000')")
                    assert not launches and not kills
                else:
                    await page.evaluate("showTab('Running')")
                    await page.wait_for_function("monitor._loadTasks()[0].status==='done'")
                    proof = "=== Process exited with code 0 ===" if scenario == "terminal-exit" else "DOWNLOAD_OK"
                    assert proof in (await page.evaluate("monitor._loadTasks()[0].output"))
                    assert await page.locator('.cookbook-task-status').inner_text() == "finished"
                    assert await page.locator('#running-count').inner_text() == "0"
                    assert not launches
                    # A slow older status response must not erase verified
                    # completion and downgrade the next poll to crashed.
                    live.append({"session_id": "cookbook-first", "type": "download", "status": "stopped", "output_tail": "Preflight: 0.00B"})
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    await page.evaluate("monitor._pollBackgroundStatus()")
                    assert (await page.evaluate("monitor._loadTasks()[0].status")) == "done"
                    assert proof in (await page.evaluate("monitor._loadTasks()[0].output"))
                await page.screenshot(path=str(tmp_path / f"download-monitor-{scenario}.png"))
                assert not errors, errors
            finally:
                await browser.close()

    asyncio.run(run())
