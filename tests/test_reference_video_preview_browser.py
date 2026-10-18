"""Real browser decoding of neutral local MKV fallback; no user media or jobs."""
import asyncio
import hashlib
import mimetypes
import shutil
import subprocess
import time
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
API = '/api/video/h3/local-previews'


async def serve_video(handler, data):
    import re
    match = re.fullmatch(r'bytes=(\d+)-(\d*)', handler.request.headers.get('range', ''))
    headers = {'Accept-Ranges': 'bytes'}
    if match:
        start = int(match[1]); end = min(int(match[2]) if match[2] else len(data)-1, len(data)-1)
        headers['Content-Range'] = f'bytes {start}-{end}/{len(data)}'
        await handler.fulfill(status=206, content_type='video/mp4', headers=headers, body=data[start:end+1])
    else:
        await handler.fulfill(content_type='video/mp4', headers=headers, body=data)


@pytest.fixture
def neutral_media(tmp_path):
    ffmpeg = pytest.importorskip('imageio_ffmpeg').get_ffmpeg_exe()
    mp4, mkv = tmp_path / 'neutral-preview.mp4', tmp_path / 'neutral-source.mkv'
    subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=24',
                    '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '6',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                    '-movflags', '+faststart', str(mp4)], check=True, timeout=30)
    subprocess.run([ffmpeg, '-v', 'error', '-i', str(mp4), '-map', '0:v', '-map', '0:a', '-map', '0:a',
                    '-c:v', 'copy', '-c:a', 'libvorbis', str(mkv)], check=True, timeout=30)
    return mp4.read_bytes(), mkv


def test_local_mkv_compatible_preview_playback_ranges_reuse_and_retry(tmp_path, neutral_media):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Browser plugin unavailable; Chrome required')
    mp4, mkv = neutral_media
    uploads, windows, deleted, errors = [], [], [], []
    failure = {'window': 0}

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 1100, 'height': 800})
            page.set_default_timeout(8000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                parsed = urlparse(request.url)
                path = parsed.path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Compatible reference preview QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body style="display:block"><input id="file" type="file" style="max-width:100%"><main id="editor" style="max-width:800px;margin:16px"></main></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path == API and request.method == 'POST':
                    message = BytesParser(policy=default).parsebytes(b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\nMIME-Version: 1.0\r\n\r\n' + request.post_data_buffer)
                    parts = list(message.iter_parts())
                    uploads.append(parts)
                    ident = str(len(uploads)) * 32
                    await handler.fulfill(status=201, json={'id': ident, 'metadata': {'duration_seconds': 600, 'browser_compatible': False, 'width': 160, 'height': 120},
                        'preview_url': API + '/' + ident + '/video', 'expires_at': time.time() + 7200})
                elif path.startswith(API + '/') and path.endswith('/video'):
                    windows.append((request.method, parse_qs(parsed.query)))
                    if failure['window']:
                        await handler.fulfill(status=failure['window'], json={'detail': 'Preview temporarily unavailable'})
                    else:
                        await serve_video(handler, mp4)
                elif path.startswith(API + '/') and request.method == 'DELETE':
                    deleted.append(path.rsplit('/', 1)[1])
                    await handler.fulfill(json={'deleted': True})
                else:
                    await handler.fulfill(status=404)

            await page.route('**/*', route)
            try:
                await page.goto('https://reference-preview.test/')
                assert await page.title() == 'Compatible reference preview QA'
                await page.locator('#file').set_input_files(mkv)
                await page.evaluate("""async()=>{
                  const send=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(body){window.uploadBody=body;return send.call(this,body)};
                  window.ranges=await import('/static/js/referenceVideoRange.js');
                  window.sourceFile=document.querySelector('#file').files[0];
                  ranges.setReferenceVideoRange(sourceFile,{start_seconds:23,duration_seconds:4});
                  window.openEditor=()=>{window.editor=ranges.createReferenceVideoRangeEditor(sourceFile);document.querySelector('#editor').replaceChildren(editor.node)};
                  openEditor();
                }""")
                assert not uploads and not windows
                await page.get_by_text('Preview video and choose a section', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert len(uploads) == 1 and len(uploads[0]) == 1
                part = uploads[0][0]
                assert part.get_param('name', header='content-disposition') == 'video'
                assert part.get_filename() == mkv.name
                # CDP omits multipart file bytes; inspect the real XHR FormData.
                body_hash = await page.evaluate("async()=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',await uploadBody.get('video').arrayBuffer()))).map(n=>n.toString(16).padStart(2,'0')).join('')")
                assert body_hash == hashlib.sha256(mkv.read_bytes()).hexdigest()
                assert windows[-1][1] == {'start_seconds': ['23'], 'duration_seconds': ['30']}
                await page.locator('video').evaluate('(v)=>v.play()')
                await page.wait_for_function('document.querySelector("video").getVideoPlaybackQuality().totalVideoFrames>1')
                bright = await page.locator('video').evaluate('''v=>{v.pause();const c=document.createElement('canvas');c.width=160;c.height=120;const x=c.getContext('2d');x.drawImage(v,0,0,160,120);return Array.from(x.getImageData(0,0,160,120).data).filter((n,i)=>i%4!==3&&n>40).length}''')
                assert bright > 1000, 'Neutral decoded frame must not be black'
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 23, 'duration_seconds': 4}
                await page.wait_for_function('document.querySelector("video").seekable.length>0')
                await page.locator('video').evaluate('v=>new Promise(resolve=>{v.addEventListener("seeked",resolve,{once:true});v.currentTime=2})')
                await page.get_by_role('button', name='Use current time as start').click()
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 25, 'duration_seconds': 4}
                await page.get_by_role('button', name='Jump forward 30 seconds').click()
                await page.wait_for_function('document.querySelector("video").src.includes("start_seconds=55")&&document.querySelector("video").readyState>=2')
                await page.get_by_role('button', name='Play selected section').click()
                await page.wait_for_function('document.querySelector("video").src.includes("start_seconds=25")&&document.querySelector("video").readyState>=2')
                assert len(uploads) == 1
                # Upload-list rerender reclaims the same File and uploaded token.
                await page.evaluate('editor.destroy();openEditor()')
                await page.get_by_text('Preview video and choose a section', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert len(uploads) == 1 and not deleted
                # A transient converter failure retries against the same source.
                failure['window'] = 503
                await page.get_by_role('button', name='Jump forward 30 seconds').click()
                await page.get_by_role('status').filter(has_text='could not load').wait_for()
                failure['window'] = 0
                await page.get_by_role('button', name='Compatible preview', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert len(uploads) == 1
                # Expired/missing temporary source retries with a fresh upload.
                failure['window'] = 410
                await page.get_by_role('button', name='Jump forward 30 seconds').click()
                await page.get_by_role('status').filter(has_text='expired').wait_for()
                failure['window'] = 0
                await page.get_by_role('button', name='Compatible preview', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert len(uploads) == 2 and deleted == ['1' * 32]
                submitted = await page.evaluate('''async()=>{const {appendVideoInputs}=await import('/static/js/videoInputs.js');const b=appendVideoInputs(new FormData(),{reference_videos:[sourceFile]});const f=b.get('reference_videos');return {name:f.name,size:f.size,fields:[...b.keys()],range:editor.getValue()}}''')
                assert submitted == {'name': mkv.name, 'size': mkv.stat().st_size, 'fields': ['reference_videos'], 'range': {'start_seconds': 25, 'duration_seconds': 4}}
                await page.locator('video').evaluate('v=>v.play()')
                await page.wait_for_function('document.querySelector("video").getVideoPlaybackQuality().totalVideoFrames>1')
                await page.locator('video').evaluate('v=>v.pause()')
                await page.screenshot(path=str(tmp_path / 'local-preview-desktop.png'))
                await page.set_viewport_size({'width': 390, 'height': 844})
                bounds = await page.locator('.h3-reference-preview button:visible').evaluate_all('buttons=>buttons.map(b=>({left:b.getBoundingClientRect().left,right:b.getBoundingClientRect().right}))')
                assert all(b['left'] >= 0 and b['right'] <= 390 for b in bounds)
                await page.screenshot(path=str(tmp_path / 'local-preview-mobile.png'))
                await page.evaluate('editor.destroy();document.querySelector("#editor").replaceChildren()')
                await page.wait_for_timeout(350)
                assert deleted == ['1' * 32, '2' * 32]
                assert not errors
            finally:
                await browser.close()
    asyncio.run(exercise())


@pytest.mark.parametrize('source', ['server-mkv', 'server-metadata', 'local-decode-error'])
def test_incompatible_inputs_fall_back_automatically_and_upload_retry_is_visible(tmp_path, neutral_media, source):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Browser plugin unavailable; Chrome required')
    mp4, _ = neutral_media
    calls, errors = [], []
    upload_attempts = 0

    async def exercise():
        nonlocal upload_attempts
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            page.set_default_timeout(6000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                nonlocal upload_attempts
                request = handler.request
                path = urlparse(request.url).path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><title>Automatic compatible preview QA</title></head><body><main></main></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript')
                elif path.endswith('/server-files/metadata'):
                    calls.append(path)
                    await handler.fulfill(json={'duration_seconds': 600, 'browser_compatible': False})
                elif path.endswith('/server-files/preview') or path.endswith('/video') and path.startswith(API):
                    calls.append(path)
                    await serve_video(handler, mp4)
                elif path == API and request.method == 'POST':
                    upload_attempts += 1
                    if upload_attempts == 1:
                        await handler.fulfill(status=507, json={'detail': 'Not enough preview disk space.'})
                    else:
                        await handler.fulfill(status=201, json={'id': 'a'*32, 'metadata': {'duration_seconds': 600},
                            'preview_url': API + '/' + 'a'*32 + '/video', 'expires_at': time.time()+7200})
                elif path.startswith(API) and request.method == 'DELETE':
                    await handler.fulfill(json={'deleted': True})
                else:
                    calls.append(path)
                    await handler.fulfill(status=404)
            await page.route('**/*', route)
            try:
                await page.goto('https://automatic-preview.test/')
                await page.evaluate('''async source=>{
                  const r=await import('/static/js/referenceVideoRange.js');
                  const f=source==='local-decode-error'?new File(['not a decodable video'],'bad.mp4',{type:'video/mp4'}):{name:source==='server-mkv'?'source.mkv':'source.mp4',serverFile:{root:'e',path:'source',size:123,mtime_ns:'123'}};
                  r.setReferenceVideoRange(f,{start_seconds:23,duration_seconds:4});
                  window.editor=r.createReferenceVideoRangeEditor(f);document.querySelector('main').append(editor.node);
                }''', source)
                assert not calls and not upload_attempts
                await page.get_by_text('Preview video and choose a section', exact=True).click()
                if source == 'local-decode-error':
                    await page.get_by_role('status').filter(has_text='Not enough preview disk space').wait_for()
                    assert upload_attempts == 1
                    assert await page.evaluate('editor.getValue()') == {'start_seconds': 23, 'duration_seconds': 4}
                    await page.get_by_role('button', name='Compatible preview', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert 'starts at 0:23.00' in await page.get_by_role('status').inner_text()
                assert not any(path.endswith('/server-files/video') for path in calls)
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 23, 'duration_seconds': 4}
                assert upload_attempts == (2 if source == 'local-decode-error' else 0)
                assert not errors
                await page.evaluate('editor.destroy()')
                await page.wait_for_timeout(300)
            finally:
                await browser.close()
    asyncio.run(exercise())


def test_jump_groups_use_source_playhead_reuse_local_upload_and_do_not_edit_selection(tmp_path, neutral_media):
    pw = pytest.importorskip('playwright.async_api')
    chrome = shutil.which('google-chrome') or shutil.which('chromium')
    if not chrome:
        pytest.skip('Browser plugin unavailable; Chrome required')
    mp4, mkv = neutral_media
    uploads, windows, errors = [], [], []

    async def exercise():
        async with pw.async_playwright() as p:
            browser = await p.chromium.launch(executable_path=chrome, headless=True, args=['--no-sandbox'])
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            page.set_default_timeout(7000)
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def route(handler):
                request = handler.request
                parsed = urlparse(request.url)
                path = parsed.path
                if path == '/':
                    await handler.fulfill(content_type='text/html', body='<html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Reference video jumps QA</title><link rel="stylesheet" href="/static/style.css"><link rel="stylesheet" href="/static/h3-video.css"></head><body style="display:block"><input id="file" type="file"><main style="margin:12px"></main></body></html>')
                elif path.startswith('/static/'):
                    file = ROOT / path.lstrip('/')
                    await handler.fulfill(body=file.read_bytes(), content_type='text/javascript' if file.suffix == '.js' else mimetypes.guess_type(file.name)[0])
                elif path == API and request.method == 'POST':
                    uploads.append(path)
                    await handler.fulfill(status=201, json={'id': 'a'*32, 'metadata': {'duration_seconds': 4000},
                        'preview_url': API + '/' + 'a'*32 + '/video', 'expires_at': time.time()+7200})
                elif path.startswith(API) and path.endswith('/video'):
                    windows.append(float(parse_qs(parsed.query)['start_seconds'][0]))
                    await serve_video(handler, mp4)
                elif path == '/direct.mp4':
                    await serve_video(handler, mp4)
                elif path.startswith(API) and request.method == 'DELETE':
                    await handler.fulfill(json={'deleted': True})
                else:
                    await handler.fulfill(status=404)
            await page.route('**/*', route)
            try:
                await page.goto('https://video-jumps.test/')
                await page.locator('#file').set_input_files(mkv)
                await page.evaluate('''async()=>{
                  window.ranges=await import('/static/js/referenceVideoRange.js');window.file=document.querySelector('#file').files[0];
                  ranges.setReferenceVideoRange(file,{start_seconds:23,duration_seconds:4});
                  window.changes=0;window.editor=ranges.createReferenceVideoRangeEditor(file,()=>changes++);
                  document.querySelector('main').append(editor.node);
                }''')
                await page.get_by_text('Preview video and choose a section', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                assert await page.get_by_role('group', name='Jump backward', exact=True).get_by_role('button').count() == 5
                assert await page.get_by_role('group', name='Jump forward', exact=True).get_by_role('button').count() == 5

                async def seek_two():
                    await page.wait_for_function('document.querySelector("video").readyState>=2 && document.querySelector("video").seekable.length>0')
                    await page.locator('video').evaluate('v=>new Promise(resolve=>{v.pause();v.addEventListener("seeked",resolve,{once:true});v.currentTime=2})')

                async def jump(direction, label, expected_start):
                    await page.get_by_role('button', name=f'Jump {direction} {label}', exact=True).click()
                    await page.wait_for_function('expected=>Number(new URL(document.querySelector("video").src).searchParams.get("start_seconds"))===expected&&document.querySelector("video").readyState>=2', arg=expected_start)

                for delta, label in [(30, '30 seconds'), (60, '60 seconds'), (300, '5 minutes'), (600, '10 minutes'), (1800, '30 minutes')]:
                    await page.get_by_role('button', name='Compatible preview', exact=True).click()
                    await seek_two()  # Source time 23 + 2, not window start 23.
                    await jump('forward', label, 25+delta)
                    await page.get_by_role('button', name='Compatible preview', exact=True).click()
                    await seek_two()
                    await jump('forward', '30 minutes', 1825)
                    await seek_two()  # Source time 1825 + 2.
                    await jump('backward', label, 1827-delta)
                    assert await page.evaluate('editor.getValue()') == {'start_seconds': 23, 'duration_seconds': 4}
                    assert await page.evaluate('changes') == 0
                # Pasted display timestamps remain numeric source ranges.
                start_input = page.get_by_role('textbox', name='Start at (seconds or time) for neutral-source.mkv', exact=True)
                await start_input.fill('23:14.5')
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 1394.5, 'duration_seconds': 4}
                await page.get_by_role('button', name='Play selected section', exact=True).click()
                await page.wait_for_function('Number(new URL(document.querySelector("video").src).searchParams.get("start_seconds"))===1394.5&&document.querySelector("video").readyState>=2')
                await page.locator('video').evaluate('v=>v.pause()')
                await start_input.fill('1:02:03.5')
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 3723.5, 'duration_seconds': 4}
                await seek_two()
                # Typing a start does not move playback; jump uses 1394.5+2.
                await jump('forward', '30 seconds', 1426.5)
                assert await page.evaluate('editor.getValue().start_seconds') == 3723.5
                await start_input.fill('90')
                assert await page.evaluate('editor.getValue().start_seconds') == 90
                for invalid in ['1:60', '1:60:00', '-1', '1::2', 'not a time', '']:
                    await start_input.fill(invalid)
                    assert not await start_input.evaluate('input=>input.checkValidity()')
                    assert await page.evaluate('editor.getValue().start_seconds') == 90
                count_before_invalid_play = len(windows)
                await start_input.fill('1:30:00')  # Valid syntax, beyond this source.
                assert not await start_input.evaluate('input=>input.checkValidity()')
                await page.get_by_role('button', name='Play selected section', exact=True).click()
                await page.wait_for_timeout(100)
                assert len(windows) == count_before_invalid_play
                await start_input.fill('23')
                assert await start_input.evaluate('input=>input.checkValidity()')
                await page.evaluate('window.changes=0')
                # Boundary clamps retain the final two playable seconds.
                await page.get_by_role('button', name='Compatible preview', exact=True).click()
                await seek_two()
                await jump('backward', '30 minutes', 0)
                assert await page.get_by_role('button', name='Jump backward 30 seconds', exact=True).is_disabled()
                await jump('forward', '30 minutes', 1800)
                await seek_two()
                await jump('forward', '30 minutes', 3602)
                await seek_two()
                await jump('forward', '30 minutes', 3998)
                await page.wait_for_function('document.querySelector("video").currentTime===0')
                assert await page.get_by_role('button', name='Jump forward 30 seconds', exact=True).is_disabled()
                assert len(uploads) == 1
                # Locked drafts remain browseable but cannot apply a new section.
                await page.evaluate('editor.setDisabled(true)')
                await jump('backward', '5 minutes', 3698)
                assert await page.get_by_role('button', name='Use current time as start').is_disabled()
                assert await page.evaluate('changes') == 0
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 23, 'duration_seconds': 4}
                await page.evaluate('editor.setDisabled(false)')
                await page.get_by_role('button', name='Use current time as start').click()
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 3698, 'duration_seconds': 4}
                assert await page.evaluate('changes') == 1
                bounds = await page.locator('.h3-reference-jumps button').evaluate_all('buttons=>buttons.map(b=>({left:b.getBoundingClientRect().left,right:b.getBoundingClientRect().right}))')
                assert all(b['left'] >= 0 and b['right'] <= 390 for b in bounds)
                await page.locator('video').evaluate('v=>v.play()')
                await page.wait_for_function('document.querySelector("video").getVideoPlaybackQuality().totalVideoFrames>1')
                await page.locator('video').evaluate('v=>v.pause()')
                await page.evaluate('document.documentElement.style.height="auto";document.body.style.height="auto";document.body.scrollTop=0;window.scrollTo(0,0)')
                await page.screenshot(path=str(tmp_path / 'video-jumps-mobile.png'), full_page=True)
                await page.set_viewport_size({'width': 1100, 'height': 850})
                await page.screenshot(path=str(tmp_path / 'video-jumps-desktop.png'), full_page=True)
                # Browser-compatible videos seek directly and never create a proxy.
                await page.evaluate('''()=>{editor.destroy();window.direct={name:'direct.mp4',preview_url:'/direct.mp4'};ranges.setReferenceVideoRange(direct,{start_seconds:1,duration_seconds:4});window.editor=ranges.createReferenceVideoRangeEditor(direct);document.querySelector('main').replaceChildren(editor.node)}''')
                await page.get_by_text('Preview video and choose a section', exact=True).click()
                await page.wait_for_function('document.querySelector("video").readyState>=2')
                proxy_count = len(windows)
                for label in ['30 seconds', '60 seconds', '5 minutes', '10 minutes', '30 minutes']:
                    await page.get_by_role('button', name=f'Jump forward {label}', exact=True).click()
                    await page.wait_for_function('document.querySelector("video").currentTime===document.querySelector("video").duration-2')
                    await page.get_by_role('button', name=f'Jump backward {label}', exact=True).click()
                    await page.wait_for_function('document.querySelector("video").currentTime===0')
                assert len(windows) == proxy_count and len(uploads) == 1
                assert await page.evaluate('editor.getValue()') == {'start_seconds': 1, 'duration_seconds': 4}
                # Queued/rerun fields use this same parser with previews disabled.
                await page.evaluate('''()=>{editor.destroy();window.rerun=ranges.referenceVideoRangesField([{start_seconds:59.999,duration_seconds:4}]).create(()=>{});const form=document.createElement('form');form.append(rerun.node);document.querySelector('main').replaceChildren(form)}''')
                assert 'Use 1:00.00' in await page.locator('.h3-reference-range > p').inner_text()
                rerun_start = page.get_by_role('textbox', name='Start at (seconds or time) for reference video 1', exact=True)
                await rerun_start.fill('23:14.5')
                assert await page.evaluate('rerun.getValue()') == [{'start_seconds': 1394.5, 'duration_seconds': 4}]
                await page.evaluate('rerun.setValue([{start_seconds:3599.999,duration_seconds:4}])')
                assert 'Use 60:00.00' in await page.locator('.h3-reference-range > p').inner_text()
                await rerun_start.fill('60:00.00')
                assert await page.evaluate('rerun.getValue()[0].start_seconds') == 3600
                await page.evaluate('rerun.setValue([{start_seconds:3723.5,duration_seconds:4}])')
                assert await rerun_start.input_value() == '3723.5'
                await rerun_start.fill('1:60')
                assert not await page.locator('form').evaluate('form=>form.checkValidity()')
                await page.evaluate('rerun.setValue([{start_seconds:90,duration_seconds:4}])')
                assert await page.locator('form').evaluate('form=>form.checkValidity()')
                assert await page.evaluate('rerun.getValue()') == [{'start_seconds': 90, 'duration_seconds': 4}]
                assert not errors
                await page.evaluate('editor.destroy()')
            finally:
                await browser.close()
    asyncio.run(exercise())
