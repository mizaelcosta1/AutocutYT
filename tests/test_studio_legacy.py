import importlib
import io
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient


def load_server(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOCLIPS_DATA_ROOT', str(tmp_path / 'data'))
    monkeypatch.delenv('AUTOCLIPS_PASSWORD', raising=False)
    if 'server' in sys.modules:
        del sys.modules['server']
    return importlib.import_module('server')


def sample_video(dst: Path, seconds: int = 3):
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                    '-f', 'lavfi', '-i', 'testsrc2=size=240x120:rate=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=320:sample_rate=22050',
                    '-t', str(seconds), '-c:v', 'libx264', '-preset', 'ultrafast',
                    '-c:a', 'aac', str(dst)], check=True)
    return dst.read_bytes()


def upload_video(client, payload):
    result = client.post('/api/upload/start', json={
        'filename': 'my_demo.mp4', 'size': len(payload), 'authorized': True})
    assert result.status_code == 200, result.text
    uid = result.json()['upload_id']
    chunk_size = result.json()['chunksize']
    count = result.json()['chunks']
    for i in range(count):
        body = payload[i * chunk_size: (i + 1) * chunk_size]
        res = client.put(f'/api/upload/{uid}/chunk/{i}', content=body)
        assert res.status_code == 200, res.text
    complete = client.post(f'/api/upload/{uid}/complete')
    assert complete.status_code == 200, complete.text
    return complete.json()['video']


def wait_task(client, task_id):
    for _ in range(240):
        result = client.get('/api/tasks/' + task_id).json()
        if result['status'] != 'queued' and result['status'] != 'running':
            return result
        time.sleep(.25)
    pytest.fail('task timed out')


def test_platform_validation(tmp_path, monkeypatch):
    s = load_server(tmp_path, monkeypatch)
    for url, p in [('https://kick.com/live-name','kick'),
                   ('https://www.youtube.com/watch?v=123','youtube'),
                   ('https://www.twitch.tv/name','twitch')]:
        assert s.platform(url) == p
    for url in ['https://www.twitch.tv.evil.com', 'http://twitch.tv/name',
                'https://127.0.0.1:9000/', 'https://evil.com/']:
        with pytest.raises(ValueError):
            s.platform(url)


@pytest.mark.skipif(not __import__('shutil').which('ffmpeg'), reason='ffmpeg unavailable')
def test_chunked_upload_and_manual_preview(tmp_path, monkeypatch):
    s=load_server(tmp_path, monkeypatch)
    client=TestClient(s.app)
    src=tmp_path / 'source.mp4'
    payload=sample_video(src)
    assert client.post('/api/upload/start', json={'filename':'a.mp4', 'size':len(payload), 'authorized':False}).status_code==400
    vid=upload_video(client,payload)
    assert vid['duration']>=2
    assert client.get(f"/api/videos/{vid['id']}/stream", headers={'range':'bytes=0-19'}).status_code==206
    assert src.read_bytes()==payload
    add=client.post(f"/api/videos/{vid['id']}/manual",json={'start':0,'end':2,'title':'Corte de teste'})
    assert add.status_code==200,add.text
    manifest=client.get(f"/api/videos/{vid['id']}/manifest").json()
    assert manifest['clips'][0]['title']=='Corte de teste'
    unapproved=client.post(f"/api/videos/{vid['id']}/render/preview",json={'clip_id':'clip_01'})
    assert unapproved.status_code==400
    req=client.post(f"/api/videos/{vid['id']}/render/preview",json={'clip_id':'clip_01','approved_selection':True,'layout':'fit'})
    assert req.status_code==200,req.text
    task=wait_task(client,req.json()['task_id'])
    assert task['status']=='done',task
    content=client.get(f"/api/videos/{vid['id']}/rendered/clip_01/preview")
    assert content.status_code==200
    assert len(content.content)>10000
    final_req=client.post(f"/api/videos/{vid['id']}/render/final",json={'clip_id':'clip_01','approved_preview':True,'layout':'fit'})
    assert final_req.status_code==200
    final_task=wait_task(client,final_req.json()['task_id'])
    assert final_task['status']=='done',final_task
    assert client.get(f"/api/videos/{vid['id']}/rendered/clip_01/final").status_code==200
    # A modified timeline invalidates the old preview and final before a new render.
    modify=client.put(f"/api/videos/{vid['id']}/clips",json={'clips':[{'id':'clip_01','title':'Novo','start':0.1,'end':2.1,'selected':True}]})
    assert modify.status_code==200
    manifest=client.get(f"/api/videos/{vid['id']}/manifest").json()
    assert manifest['clips'][0]['preview_url'] is None
    assert manifest['clips'][0]['final_url'] is None
    assert client.post(f"/api/videos/{vid['id']}/render/final",json={'clip_id':'clip_01','approved_preview':True}).status_code==409
    # Reject stale approval synchronously before scheduling an expensive render.


def test_upload_resume_and_incomplete_rejected(tmp_path, monkeypatch):
    s=load_server(tmp_path, monkeypatch)
    client=TestClient(s.app)
    initial=client.post('/api/upload/start',json={'filename':'test.mp4', 'size':27,'authorized':True}).json()
    uid=initial['upload_id']
    assert client.post(f'/api/upload/{uid}/complete').status_code==409
    assert client.put(f'/api/upload/{uid}/chunk/0', content=b'1234').status_code==400
    assert client.get(f'/api/upload/{uid}').json()['received']==[]
    assert client.put(f'/api/upload/{uid}/chunk/0', content=b'x'*27).status_code==200
    assert client.get(f'/api/upload/{uid}').json()['received']==[0]
    assert client.post(f'/api/upload/{uid}/complete').status_code==422  # bad MP4 rejected
