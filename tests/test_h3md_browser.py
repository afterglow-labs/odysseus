"""H3MD real-browser selection, shot editing, submission and draft persistence."""
import asyncio, json, mimetypes, shutil
from pathlib import Path
from urllib.parse import urlparse
from email.parser import BytesParser
from email.policy import default
import pytest
from src.h3md import capabilities
from scripts.h3md_controls import COMPONENTS,LORAS
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize("recipe", ["h3md", "pinkcherry"])
@pytest.mark.parametrize("layout,width", [("classic",390),("pipeline",1440)])
def test_h3md_two_pass_workflow(layout,width,recipe):
    pw=pytest.importorskip("playwright.async_api")
    chrome=shutil.which("google-chrome") or shutil.which("chromium")
    if not chrome: pytest.skip("Chrome required")
    components=[dict(id=name,name=name,role=role,variant="ref2va" if role=="model" else "shared") for role,name in COMPONENTS.items()]
    components += [dict(id=name,name=name,role="lora",variant="shared") for name,low,high in LORAS[:2]]
    capability=capabilities(ROOT,components);capability["runtime_ready"]=True
    if recipe == 'pinkcherry':
        from src.h3_pinkcherry import capabilities as pink_capabilities
        from scripts.h3_pinkcherry_controls import COMPONENTS as PINK_COMPONENTS
        components=[dict(id=name,name=name,role=role,variant='fl2va' if role=='lora' else 'ref2va' if role=='model' else 'shared') for role,name in PINK_COMPONENTS.items()]
        capability=pink_capabilities(components)
    defaults=dict(gpu="GPU-fixture",vae_gpu="",prompt="",**capability["defaults"])
    inventory=dict(defaults=defaults,components=components,gpus=[dict(id="GPU-fixture",name="RTX 5090",nvfp4=True)],runtime_ready=True,batch_jobs=True,h3md=capability,lora_stack=True,max_loras=8)
    inventory[recipe]=capability
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
                    jobs=[dict(id="a"*32,recipe=recipe,status="completed",revision=0,created_at=123,config=requests[0])] if requests else []
                    await handler.fulfill(json=dict(jobs=jobs,queue={},pagination=dict(page=1,total=len(jobs),total_pages=1)))
                else:await handler.fulfill(json=dict(models=[],prompts=[],jobs=[],queue={},targets=[]))
            await page.route("**/*",route)
            try:
                await page.goto("https://h3md.test/")
                await page.evaluate("async options=>(await import('/static/js/h3Video.js')).showH3Video(options)",dict(layout=layout,recipe=recipe))
                await page.get_by_text("Components ready on this Odysseus server.",exact=True).wait_for()
                if layout=="pipeline": await page.get_by_role("button",name="Show all settings",exact=True).click()
                if recipe == 'h3md':
                    assert await page.get_by_role("heading",name="H3MD Director",exact=True).count()==1
                    assert await page.get_by_role('combobox',name='LoRA 1',exact=True).input_value()==LORAS[0][0]
                    assert await page.get_by_role('combobox',name='LoRA 2',exact=True).input_value()==LORAS[1][0]
                    await page.get_by_label('Shot 1 prompt',exact=True).fill('A red boat on calm water.')
                    await page.get_by_role('button',name='Add shot',exact=True).click()
                    await page.get_by_label('Shot 2 prompt',exact=True).fill('The boat passes a pebble.')
                    await page.get_by_label('Steps before upscaling',exact=True).fill('5')
                    await page.get_by_label('Steps before upscaling',exact=True).press('Tab')
                    await page.get_by_label('Final-pass LoRA 2 strength',exact=True).fill('0.7')
                    await page.get_by_label('Final-pass LoRA 2 strength',exact=True).press('Tab')
                    await page.screenshot(path=f'/tmp/{recipe}-{layout}-{width}.png')
                    if recipe=='h3md':
                        assert await page.locator('.h3md-controls').evaluate("n=>n.scrollWidth <= n.clientWidth + 1"), await page.locator('.h3md-controls').evaluate("n=>[n,...n.querySelectorAll('*')].filter(c=>c.scrollWidth>c.clientWidth+1).map(c=>[c.tagName,c.className,c.clientWidth,c.scrollWidth])")
                else:
                    assert await page.get_by_role('heading',name='PinkCherry',exact=True).count()==1
                    assert await page.get_by_role('combobox',name='LoRA 1',exact=True).input_value()==PINK_COMPONENTS['lora']
                    await page.locator('textarea').first.fill('A red boat on calm water.')
                    import base64
                    await page.locator('#h3-reference_images').set_input_files(dict(name='reference.png',mimeType='image/png',buffer=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/S90AAAAASUVORK5CYII=')))
                    await page.get_by_role('button',name='PinkCherry · 4 steps',exact=True).click()
                    assert await page.locator('textarea').first.input_value()=='A red boat on calm water.'
                    await page.get_by_role('button',name='PinkCherry · 12 steps',exact=True).click()
                    await page.screenshot(path=f'/tmp/{recipe}-{layout}-{width}.png')
                await page.locator('button[type="submit"]').filter(has_text='Generate').first.click()
                await page.wait_for_timeout(200)
                assert len(requests)==1,(errors,await page.locator('.h3-video-error').all_text_contents(),await page.locator(':invalid').evaluate_all("nodes=>nodes.map(n=>[n.name,n.value,n.validationMessage])"))
                sent=requests[0]
                if recipe == 'h3md':
                    assert sent['recipe']=='h3md' and sent['h3md']['transition_step']==5
                    assert sent['h3md']['high_res_strengths']==[1,0.7]
                    assert len(sent['h3md']['shots'])==2 and sent['h3md']['shots'][1]['overlap']==39
                    assert sent['latent_upscaler']==COMPONENTS['latent_upscaler']
                    assert sent['encoder']==COMPONENTS['encoder']
                    assert len(sent['loras'])==2
                else:
                    assert sent['recipe']=='pinkcherry'
                    assert [sent[k] for k in ['steps','frames','width','height']]==[12,294,672,1216]
                    assert sent['loras']==[dict(id=PINK_COMPONENTS['lora'],strength=1)]
                    assert sent['encoder']==PINK_COMPONENTS['encoder']
                assert not errors
            finally: await browser.close()
    asyncio.run(exercise())
