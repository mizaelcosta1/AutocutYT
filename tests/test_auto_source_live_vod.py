"""Regression use case: same YouTube /live link can be live or an archived VOD.
All media downloads are fixture-controlled; no external video is claimed tested.
"""
import importlib
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

URL = 'https://www.youtube.com/live/hOOiQagTyTA?is=-WdBBCQjg7sX0w54'


def new_app(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOCLIPS_DATA_ROOT', str(tmp_path / 'studio'))
    for key in ('AUTOCLIPS_GOOGLE_CLIENT_ID','AUTOCLIPS_GOOGLE_CLIENT_SECRET',
                'AUTOCLIPS_SESSION_SECRET','AUTOCLIPS_ALLOWED_EMAILS'):
        monkeypatch.delenv(key, raising=False)
    for mod in ('studio_auth','server'):
        sys.modules.pop(mod, None)
    return importlib.import_module('server')


def fixture_mp4(path):
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
         '-f','lavfi','-i','testsrc2=size=160x90:rate=12',
         '-f','lavfi','-i','sine=frequency=440:sample_rate=16000',
         '-t','10','-c:v','libx264','-preset','ultrafast','-c:a','aac',str(path)],
         check=True)
    return path


def wait(client, task_id):
    for _ in range(200):
        x=client.get('/api/tasks/'+task_id).json()
        if x['status'] not in ('queued','running'):
            return x
        time.sleep(.15)
    pytest.fail('task did not finish')


def test_exact_youtube_live_url_is_accepted_as_youtube(tmp_path, monkeypatch):
    app = new_app(tmp_path, monkeypatch)
    assert app.platform(URL) == 'youtube'


@pytest.mark.parametrize('metadata,kind', [
    ({'live_status':'was_live','is_live':False,'duration':3600}, 'vod'),
    ({'live_status':'not_live','duration':600}, 'vod'),
    ({'live_status':'is_live','is_live':True}, 'live'),
    ({'is_live':True}, 'live'),
])
def test_same_live_link_uses_metadata_not_url_path(metadata,kind):
    from source_detect import classify_info
    assert classify_info(metadata,'youtube',URL)==kind


def test_source_import_archived_live_creates_real_720p_previews(tmp_path,monkeypatch):
    app = new_app(tmp_path, monkeypatch)
    monkeypatch.setattr(app.shutil,'which',lambda name: '/usr/bin/'+name)
    monkeypatch.setattr(app,'detect_source_kind',lambda url: 'vod')
    original = fixture_mp4(tmp_path/'fixture.mp4')
    monkeypatch.setattr(app,'download_vod',lambda url,folder: original)
    client = TestClient(app.app)
    response=client.post('/api/source/auto',json={'url':URL,'authorized':True,
                                   'analyze_after':True,'auto_preview':True,'transcribe_after':False})
    assert response.status_code == 200,response.text
    assert response.json()['kind']=='vod'
    outcome=wait(client,response.json()['task_id'])
    assert outcome['status']=='done',outcome
    video_id=outcome['result']['video_id']
    m=client.get(f'/api/videos/{video_id}/manifest').json()
    assert m['clips'],m
    ready=[c for c in m['clips'] if c.get('preview_url')]
    assert ready, m
    resp=client.get(ready[0]['preview_url'])
    assert resp.status_code==200 and len(resp.content)>10000
    preview_file=tmp_path/'preview_test.mp4';preview_file.write_bytes(resp.content)
    import autoclips
    assert autoclips.probe(preview_file)['width']==720
    assert autoclips.probe(preview_file)['height']==1280
    assert client.get(f'/api/videos/{video_id}/manifest').json()['review_required'] is True


def test_source_import_active_live_invokes_stream_recording_not_ytdlp_download(tmp_path,monkeypatch):
    app=new_app(tmp_path,monkeypatch)
    monkeypatch.setattr(app.shutil,'which',lambda name:'/usr/bin/'+name)
    monkeypatch.setattr(app,'detect_source_kind',lambda url:'live')
    called=[]
    class FakeProc:
        def __init__(self,*a,**k):called.append((a,k))
        def poll(self):return None
    monkeypatch.setattr(app,'start_capture',FakeProc)
    monkeypatch.setattr(app.threading,'Thread',lambda **kwargs:SimpleNamespace(start=lambda:None))
    response=TestClient(app.app).post('/api/source/auto',json={'url':URL,'authorized':True,
                                               'analyze_after':True,'auto_preview':True,'transcribe_after':False})
    assert response.status_code==200,response.text
    assert response.json()['kind']=='live'
    assert response.json()['live_id']
    assert called and called[0][0][0] == URL
    assert called[0][0][1].suffix == '.ts'
    assert app.LIVE[response.json()['live_id']]['auto_preview'] is True


def test_invalid_link_requires_authorization_and_never_calls_yt_dlp(tmp_path, monkeypatch):
    app=new_app(tmp_path, monkeypatch)
    hit=[]
    monkeypatch.setattr(app,'detect_source_kind',lambda _:hit.append('called') or 'vod')
    c=TestClient(app.app)
    assert c.post('/api/source/auto',json={'url':URL,'authorized':False}).status_code==400
    assert c.post('/api/source/auto',json={'url':'https://evil.example/video','authorized':True}).status_code==400
    assert hit==[]


def test_repeated_start_does_not_duplicate_active_stream_recording(tmp_path,monkeypatch):
    app=new_app(tmp_path,monkeypatch)
    monkeypatch.setattr(app.shutil,'which',lambda name:'/usr/bin/'+name)
    called=[]
    class FakeProc:
        def __init__(self,*args,**kwargs): called.append(args)
        def poll(self):return None
    monkeypatch.setattr(app,'start_capture',FakeProc)
    monkeypatch.setattr(app.threading,'Thread',lambda **kwargs:SimpleNamespace(start=lambda:None))
    client=TestClient(app.app)
    data={'url':URL,'authorized':True,'analyze_after':True,'transcribe_after':False}
    first=client.post('/api/live/start',json=data)
    assert first.status_code==200,first.text
    repeated=client.post('/api/live/start',json=data)
    assert repeated.status_code==409,repeated.text
    assert len(called)==1
