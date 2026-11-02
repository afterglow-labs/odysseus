"""Use the actual desktop and mobile-width editors with an isolated API fixture."""
import asyncio
import base64
from email.parser import BytesParser
from email.policy import default
import json
import mimetypes
from pathlib import Path
import shutil
from urllib.parse import urlparse

import pytest
from src.h3_director import capabilities
from scripts.h3_director_controls import WORKFLOW_COMPONENTS

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("layout,width", [("classic",1440),("classic",390),("pipeline",1440),("pipeline",390)])
def test_director_queues_its_timeline_modes_and_independent_candidates(layout,width):
    pw=pytest.importorskip("playwright.async_api")
    chrome=shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome: pytest.skip("Chrome required")
    components=[dict(id=name,name=name,role="model" if field=="model_fl2va" else field,
                     variant="ref2va" if field=="model" else "fl2va" if field=="model_fl2va" else "shared")
                for field,name in WORKFLOW_COMPONENTS.items()]
    defaults=dict(mode="ref2va",gpu="GPU-fixture",vae_gpu="",seed=42,prompt="",reference_size="match",lora_scale=1,
                  **{role:next(c["id"] for c in components if c["role"]==role) for role in ["model","encoder","video_vae","audio_vae"]})
    inventory=dict(defaults=defaults,base_defaults=defaults,components=components,gpus=[dict(id="GPU-fixture",name="RTX 5090",nvfp4=True)],
                   runtime_ready=True,batch_jobs=True,director=capabilities(components),lora_stack=True)
    requests,reruns,errors=[],[],[]
    async def exercise():
        async with pw.async_playwright() as p:
            browser=await p.chromium.launch(executable_path=chrome,headless=True,args=["--no-sandbox"])
            page=await browser.new_page(viewport=dict(width=width,height=1000))
            page.on("pageerror",lambda error:errors.append(str(error)))
            async def route(handler):
                request=handler.request;path=urlparse(request.url).path
                if path=="/": await handler.fulfill(content_type="text/html",body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body></body></html>')
                elif path.startswith("/static/"):
                    file=ROOT/path.lstrip("/");await handler.fulfill(body=file.read_bytes(),content_type="text/javascript" if file.suffix==".js" else mimetypes.guess_type(file.name)[0])
                elif path.endswith("/inventory"): await handler.fulfill(json=inventory)
                elif path=="/api/video/h3/jobs" and request.method=="POST":
                    msg=BytesParser(policy=default).parsebytes(b"Content-Type: "+request.headers["content-type"].encode()+b"\r\n\r\n"+request.post_data_buffer)
                    config=json.loads(next(part.get_payload(decode=True) for part in msg.iter_parts() if part.get_param("name",header="content-disposition")=="config"))
                    requests.append(config);await handler.fulfill(json=dict(id="a"*32,status="queued"))
                elif path=="/api/video/h3/queue/rerun":
                    body=request.post_data_json;reruns.append(body)
                    await handler.fulfill(json=dict(request_id=body["request_id"],succeeded=1,failed=0,jobs=[dict(id="b"*32,source_id="a"*32,status="queued")]))
                elif path.endswith("/jobs"):
                    jobs=[dict(id="a"*32,recipe="director",status="completed",revision=0,created_at=123,config=requests[0],
                               director=dict(stage="generation",candidates=[]))] if requests else []
                    await handler.fulfill(json=dict(jobs=jobs,queue={},pagination=dict(page=1,total=len(jobs),total_pages=1)))
                else:await handler.fulfill(json=dict(models=[],prompts=[],jobs=[],queue={},targets=[]))
            await page.route("**/*",route)
            try:
                await page.goto("https://director.test/")
                await page.evaluate("async layout=>(await import('/static/js/h3Director.js')).showH3Director({layout})",layout)
                await page.get_by_text("Components ready on this Odysseus server.",exact=True).wait_for()
                if layout=="pipeline": await page.get_by_role("button",name="Show all settings",exact=True).click()
                await page.get_by_label("Cut 1 prompt",exact=True).fill("Keep this direction.")
                await page.locator('#h3-steps').fill('7')
                await page.get_by_role('button',name='Apply Director website workflow',exact=True).click()
                assert await page.get_by_label("Cut 1 prompt",exact=True).input_value()=="Keep this direction."
                assert await page.locator('#h3-steps').input_value()=='16'
                output=ROOT/"outputs/qa";output.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output/f"director-website-{layout}-{width}.png"))
                assert await page.locator("#h3-mode option[value=t2va]").count()==0
                await page.get_by_label("Total seconds",exact=True).fill("25")
                await page.get_by_label("Cut 1 prompt",exact=True).fill("A paper boat crosses the pond.")
                await page.get_by_role("button",name="Add cut",exact=True).click()
                await page.get_by_label("Cut 2 prompt",exact=True).fill("Close-up of the ripples.")
                await page.get_by_label("Candidate 3",exact=True).check()
                await page.get_by_label("Hybrid continuation",exact=True).check()
                await page.get_by_label("Continuation checkpoint",exact=True).select_option(components[1]["id"])
                if layout == 'classic' and width == 1440:
                    pixel = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
                    await page.locator('#h3-reference_images').set_input_files([dict(name=f'boat-{i}.png',mimeType='image/png',buffer=pixel) for i in range(3)])
                    await page.get_by_text('Reference direction', exact=True).click()
                    await page.get_by_label('Image 2 name', exact=True).fill('The retained boat')
                    await page.get_by_label('Location reference image', exact=True).select_option('2')
                    await page.get_by_text('Reference direction', exact=True).click()
                    await page.get_by_label('Image 3 description', exact=True).fill('The retained location')
                    await page.get_by_role('button', name='Remove boat-0.png', exact=True).click()
                    await page.get_by_text('Reference direction', exact=True).click()
                    assert await page.get_by_label('Image 1 name', exact=True).input_value() == 'The retained boat'
                    assert await page.get_by_label('Location reference image', exact=True).input_value() == '1'
                    assert await page.get_by_label('Image 2 description', exact=True).input_value() == 'The retained location'
                output=ROOT/"outputs/qa";output.mkdir(parents=True,exist_ok=True)
                await page.get_by_label("Total seconds",exact=True).scroll_into_view_if_needed()
                await page.screenshot(path=str(output/f"director-{layout}-{width}.png"))
                await page.locator('button[type="submit"]').filter(has_text="Generate").first.click()
                await page.wait_for_timeout(250)
                assert len(requests)==1,(errors,await page.locator('.h3-video-error').all_text_contents(),await page.locator(':invalid').evaluate_all("nodes=>nodes.map(n=>[n.name,n.value,n.validationMessage])"),await page.locator('.h3-video-dialog').inner_text())
                saved=requests[0];assert saved["recipe"]=="director" and "seed_hunter" not in saved
                assert saved["loras"]==[{"id":WORKFLOW_COMPONENTS["lora"],"strength":1}]
                assert saved["encoder"]==WORKFLOW_COMPONENTS["encoder"]
                assert (saved["steps"],saved["sampler"],saved["scheduler"])==(16,"euler","beta")
                assert saved["director"]["duration_seconds"]==25 and len(saved["director"]["segments"])==2
                assert saved["director"]["candidate_3"] and not saved["director"]["candidate_2"]
                assert saved["director"]["hybrid_continuation"] and saved["model_fl2va"]==components[1]["id"]
                if layout == 'classic' and width == 1440:
                    assert saved['director']['characters'][0]['image_index'] == 0
                    assert saved['director']['background']['image_index'] == 1
                if layout=="pipeline":
                    await page.get_by_role("button",name="Focus one stage",exact=True).click()
                    await page.locator('[data-pipeline-stage="review"]').click()
                assert await page.get_by_role("button",name="Edit & rerun",exact=True).count(), (errors, await page.locator('.h3-video-dialog').inner_text())
                await page.get_by_role("button",name="Edit & rerun",exact=True).click()
                editor=page.locator('.video-queue-editor')
                assert await editor.get_by_label("Total seconds",exact=True).input_value()=="25"
                await editor.get_by_label("Total seconds",exact=True).fill("31")
                await editor.get_by_label("Cut 2 prompt",exact=True).fill("The boat reaches the bank.")
                await editor.get_by_label("Candidate 3",exact=True).uncheck()
                await editor.get_by_label("Candidate 2",exact=True).check()
                await editor.get_by_role("button",name="Queue edited copy",exact=True).click()
                await page.wait_for_timeout(250)
                assert len(reruns)==1,(errors,await page.locator('.h3-video-error').all_text_contents())
                changed=reruns[0]["patch"]["director"]
                assert changed["duration_seconds"]==31 and changed["segments"][1]["prompt"]=="The boat reaches the bank."
                assert changed["candidate_2"] and not changed["candidate_3"]
                if layout=="pipeline": await page.get_by_role("button",name="Show all settings",exact=True).click()
                assert await page.get_by_role("spinbutton",name="Total seconds",exact=True).input_value()=="25"
                await page.locator('#h3-model').select_option(components[1]["id"])
                assert await page.locator('#h3-mode').input_value()=="fl2va"
                assert not errors
            finally: await browser.close()
    asyncio.run(exercise())


def test_director_refinement_validation_and_lost_receipt_survive_reopening():
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Chrome required')

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport=dict(width=390, height=900))
            async def route(handler):
                path = urlparse(handler.request.url).path
                if path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                else:
                    await handler.fulfill(content_type='text/html', body='<meta charset="utf-8"><meta name="viewport" content="width=device-width"><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"><div id="candidates"></div>')
            await page.route('**/*', route)
            try:
                await page.goto('https://director.test/')
                await page.evaluate('''async () => {
                    const {renderDirectorCandidates} = await import('/static/js/h3Director.js');
                    window.requests = [];
                    window.render = () => {
                        const container = document.querySelector('#candidates');
                        container.replaceChildren(); delete container.dataset.candidates;
                        renderDirectorCandidates(container, {id:'a'.repeat(32), director:{refinement_defaults:{width:768,height:1376,steps:20,denoise:.2,sampler:'euler',scheduler:'beta'},candidates:[{index:3,seed:42,can_refine:true}]}}, async (_, options) => {
                            requests.push(JSON.parse(options.body));
                            if(requests.length===1)throw Object.assign(new Error('Invalid refinement setting'),{status:400});
                            if(requests.length===2)throw Object.assign(new Error('Response lost after acceptance'),{status:503});
                            return {id:'b'.repeat(32)};
                        }, async () => { throw new Error('History temporarily unavailable'); });
                    }; render();
                }''')
                await page.get_by_text('Refine candidate', exact=True).click()
                steps = page.get_by_label('Refinement steps', exact=True)
                await steps.fill('0')
                await page.get_by_role('button', name='Queue refinement', exact=True).click()
                assert await page.evaluate('requests.length') == 0
                await steps.fill('20')
                await page.get_by_role('button', name='Queue refinement', exact=True).click()
                await page.get_by_text('Invalid refinement setting', exact=True).wait_for()
                assert await steps.is_enabled()
                await steps.fill('8')
                await page.get_by_role('button', name='Queue refinement', exact=True).click()
                await page.get_by_text('Response lost after acceptance', exact=True).wait_for()
                assert await steps.is_disabled()
                await page.evaluate('render()')
                await page.get_by_text('Refine candidate', exact=True).click()
                assert await steps.input_value() == '8'
                assert await steps.is_disabled()
                await page.get_by_role('button', name='Retry same submission', exact=True).click()
                await page.get_by_text('Refinement queued. Refresh history to see the job.', exact=True).wait_for()
                assert await page.get_by_role('button', name='Refinement queued', exact=True).is_disabled()
                requests = await page.evaluate('requests')
                assert len(requests) == 3 and requests[1] == requests[2]
                assert requests[0]['request_id'] != requests[1]['request_id']
                output = ROOT / 'outputs/qa'; output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output/'director-refinement-390.png'))
            finally:
                await browser.close()
    asyncio.run(exercise())
