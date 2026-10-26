"""An edit reuses the completed result with preservation instructions, not drafts."""
import uuid
from pathlib import Path
import pytest
from src import h3_video as h3
from src import video_result_edit as edit
from tests.test_video_submission import backend, send


def completed(backend):
    response=send(backend,'h3'); assert response.status_code==201,response.text
    directory=backend.managers['h3'].directory(response.json()['id'])
    path=directory/'state.json'; h3._write(path,{**h3._read(path),'status':'completed'})
    manifest=h3._read(directory/'manifest.json'); Path(manifest['output_path']).write_bytes(b'completed-result')
    return response.json()['id'],directory


def test_edit_prepares_preservation_and_retries_without_duplicates(backend, monkeypatch):
    monkeypatch.setattr(edit,'probe',lambda p:dict(width=960,height=544,fps=24,has_audio=False))
    identity,folder=completed(backend);original=h3._read(folder/'manifest.json')
    client=backend.client();url=f'/api/video/h3/jobs/{identity}/edit-result'
    detail=client.get(url);assert detail.status_code==200,detail.text
    assert detail.json()['config']['seed_hunter']['stage']=='single'
    assert detail.json()['config']['loras']==[]
    body=dict(change='Make the jacket purple.',preserve_audio=True,steps=20,request_id=str(uuid.uuid4()))
    result=client.post(url,json=body);assert result.status_code==201,result.text
    assert client.post(url,json=body).json()['id']==result.json()['id']
    manifest=h3._read(backend.managers['h3'].directory(result.json()['id'])/'manifest.json')
    assert manifest['config']['prompt']==edit.prompt(body['change'])
    assert manifest['config']['seed']==original['config']['seed']
    assert manifest['result_edit']['source_job_id']==identity
    assert Path(manifest['reference_videos'][0]).read_bytes()==b'completed-result'
    assert manifest['config']['seed_hunter']['preview_count']==1
    assert manifest['config']['width']==960 and manifest['config']['height']==544
    assert 'auto_refine' not in manifest['config']['seed_hunter']
    assert h3._read(folder/'manifest.json')==original
    assert len(backend.calls)==2
    assert client.get(url,headers={'x-user':'other'}).status_code==404
    assert client.post(url,headers={'x-user':'other'},json={**body,'request_id':str(uuid.uuid4())}).status_code==404


def test_reject_missing_change_and_active_source(backend,monkeypatch):
    identity,folder=completed(backend);client=backend.client();url=f'/api/video/h3/jobs/{identity}/edit-result'
    assert client.post(url,json=dict(change='',request_id=str(uuid.uuid4()))).status_code==400
    path=folder/'state.json';h3._write(path,{**h3._read(path),'status':'queued'})
    assert client.get(url).status_code==400


def test_soundtrack_copy_uses_original_stream_and_replaces_output(tmp_path,monkeypatch):
    from scripts.h3_result_edit import preserve_soundtrack
    import subprocess
    source=tmp_path/'source.mp4';source.write_bytes(b'source')
    output=tmp_path/'output.mp4';output.write_bytes(b'generated')
    commands=[]
    def run(cmd,**kwargs):
        commands.append(cmd);Path(cmd[-1]).write_bytes(b'copied-original-audio')
        return subprocess.CompletedProcess(cmd,0)
    monkeypatch.setattr(subprocess,'run',run)
    preserve_soundtrack({'result_edit':{'preserve_audio':True},'reference_videos':[str(source)]},output,'ffmpeg')
    assert commands[0][commands[0].index('-c')+1]=='copy'
    assert '1:a:0?' in commands[0] and '0:v:0' in commands[0]
    assert output.read_bytes()==b'copied-original-audio'
