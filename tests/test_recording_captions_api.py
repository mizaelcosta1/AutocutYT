"""v0.7.4 lifecycle tests; synthetic media, controlled ASR, real FFmpeg exports."""
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def wait_task(client, task_id):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        task = client.get('/api/tasks/' + task_id).json()
        if task['status'] not in {'running', 'queued'}:
            return task
        time.sleep(.05)
    pytest.fail('Task did not finish')


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv('AUTOCLIPS_DATA_ROOT', str(tmp_path / 'data'))
    for key in ('AUTOCLIPS_GOOGLE_CLIENT_ID', 'AUTOCLIPS_GOOGLE_CLIENT_SECRET', 'AUTOCLIPS_SESSION_SECRET'):
        monkeypatch.delenv(key, raising=False)
    previous_modules = {name: sys.modules.get(name) for name in ('studio_auth', 'server')}
    for name in previous_modules:
        sys.modules.pop(name, None)
    server = importlib.import_module('server')
    source = server.MEDIA / 'source.mp4'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=navy:s=160x90:r=12',
                    '-f', 'lavfi', '-i', 'sine=frequency=400:sample_rate=16000', '-t', '3',
                    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source)], check=True)
    video_id = 'e' * 32
    server.save_video({'id': video_id, 'path': str(source), 'duration': 3., 'width': 160, 'height': 90,
                       'name': 'source.mp4', 'platform': 'fixture', 'created_at': server.utcnow(),
                       'size': source.stat().st_size, 'thumbnail': None})
    words = [{'start': .25, 'end': .7, 'text': 'Olá'}, {'start': .8, 'end': 1.2, 'text': 'mundo'}]
    def fake_transcript(path, model_name=None, **kwargs):
        if kwargs.get('progress'):
            kwargs['progress']({'phase': 'transcribing', 'progress': 50, 'message': 'Transcrevendo trecho 1 de 1.'})
        return {'words': words.copy(), 'engine': 'fixture-asr'}
    monkeypatch.setattr(server, 'transcript_local', fake_transcript)
    # Capability import only; this suite must not require a model or network.
    monkeypatch.setitem(sys.modules, 'faster_whisper', SimpleNamespace())
    yield server, TestClient(server.app), video_id, words
    server.POOL.shutdown(wait=True)
    for name, previous in previous_modules.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def test_post_recording_transcription_is_independent_of_cut_analysis(studio):
    server, client, vid, _ = studio
    result = server._prepare_recorded_video(vid, transcribe_after=True, analyze_after=False, auto_preview=False)
    assert result['words'] == 2 and result['proposals'] == 0
    assert client.get(f'/api/videos/{vid}/transcript').json()['available'] is True
    srt = client.get(f'/api/videos/{vid}/subtitles.srt')
    vtt = client.get(f'/api/videos/{vid}/subtitles.vtt')
    assert 'Olá mundo' in srt.text and '00:00:00,250' in srt.text
    assert vtt.text.startswith('WEBVTT') and '00:00:00.250' in vtt.text
    assert 'no-store' in vtt.headers['cache-control']
    assert client.get(f'/api/videos/{vid}/subtitles.exe').status_code == 404


def test_generate_captions_updates_existing_manual_project_and_persists_style(studio):
    server, client, vid, _ = studio
    assert client.post(f'/api/videos/{vid}/manual', json={'start': 0, 'end': 2}).status_code == 200
    assert server._transcript_document(vid)['engine'] == 'none'
    response = client.post(f'/api/videos/{vid}/transcript', json={})
    task = wait_task(client, response.json()['task_id'])
    assert task['status'] == 'done', task
    assert task['message'].startswith('Transcrevendo')
    manifest, work, _ = server.get_manifest(vid)
    assert manifest['transcription_engine'] == 'fixture-asr'
    assert manifest['clips'][0]['excerpt'] == 'Olá mundo'
    assert json.loads((work / 'transcript.json').read_text())['words']
    style = {'font_family': 'Liberation Serif', 'font_size': 72, 'color': '#22d3ee'}
    assert client.put(f'/api/videos/{vid}/subtitle-style', json=style).status_code == 200
    restored = client.get(f'/api/videos/{vid}/transcript').json()['subtitle_style']
    assert restored == {**style, 'color': '#22D3EE'}
    assert client.put(f'/api/videos/{vid}/subtitle-style', json={**style, 'font_family': "Bad';movie=/tmp/a"}).status_code == 422
    assert client.put(f'/api/videos/{vid}/subtitle-style', json={**style, 'font_size': 200}).status_code == 422


def test_clip_final_requires_matching_caption_style_and_current_transcript(studio):
    server, client, vid, words = studio
    server._transcribe_for_video(vid)
    client.post(f'/api/videos/{vid}/manual', json={'start': 0, 'end': 2})
    style = {'font_family': 'DejaVu Sans', 'font_size': 64, 'color': '#22D3EE'}
    payload = {'clip_id': 'clip_01', 'layout': 'fit', 'subtitles': True,
               'subtitle_style': style, 'approved_selection': True, 'approved_preview': True}
    start = client.post(f'/api/videos/{vid}/render/preview', json=payload)
    task = wait_task(client, start.json()['task_id'])
    assert task['status'] == 'done', task
    changed = {**payload, 'subtitle_style': {**style, 'color': '#FF5555'}}
    assert client.post(f'/api/videos/{vid}/render/final', json=changed).status_code == 409
    manifest = client.get(f'/api/videos/{vid}/manifest').json()
    assert manifest['clips'][0]['preview_options']['subtitle_style'] == style
    assert client.get(manifest['clips'][0]['preview_url']).status_code == 200
    words[0]['text'] = 'Revisado'
    server._transcribe_for_video(vid, force=True)
    assert client.post(f'/api/videos/{vid}/render/final', json=payload).status_code == 409
    assert client.get(f'/api/videos/{vid}/rendered/clip_01/preview').status_code == 409
    assert client.get(f'/api/videos/{vid}/manifest').json()['clips'][0]['preview_url'] is None


def test_timeline_style_changes_need_new_preview_and_vtt_rebases(studio):
    server, client, vid, _ = studio
    server._transcribe_for_video(vid)
    base = f'/api/videos/{vid}'
    client.get(base + '/timeline')
    client.post(base + '/timeline', json={'action': 'trim', 'id': 'seg_001', 'source_in': .5,
                                        'source_out': 2, 'expected_version': 1})
    vtt = client.get(base + '/timeline.vtt')
    assert vtt.status_code == 200 and 'WEBVTT' in vtt.text and 'mundo' in vtt.text
    style = {'font_family': 'DejaVu Serif', 'font_size': 60, 'color': '#FF5555'}
    request = {'layout': 'fit', 'subtitles': True, 'subtitle_style': style,
               'approved_selection': True, 'approved_preview': True}
    response = client.post(base + '/timeline/render/preview', json=request)
    task = wait_task(client, response.json()['task_id'])
    assert task['status'] == 'done', task
    meta = client.get(base + '/timeline/exports').json()['exports']['preview']
    assert meta['subtitle_style'] == style and meta['render_id']
    assert client.post(base + '/timeline/render/final', json={**request,
                       'subtitle_style': {**style, 'font_size': 80}}).status_code == 409


def test_failed_transcription_preserves_saved_recording_for_retry(studio, monkeypatch):
    server, client, vid, _ = studio
    def fail(*args, **kwargs):
        raise ValueError('O vídeo não contém uma faixa de áudio para transcrever.')
    monkeypatch.setattr(server, 'transcript_local', fail)
    result = server._prepare_recorded_video(vid, transcribe_after=True, analyze_after=False, auto_preview=False)
    assert result['transcription'] == 'error'
    assert client.get(f'/api/videos/{vid}/stream').status_code == 200
    assert client.get(f'/api/videos/{vid}/transcript').json()['state']['status'] == 'error'


def test_duplicate_transcription_and_deletion_blocked_during_processing(studio, monkeypatch):
    server, client, vid, words = studio
    entered, release = threading.Event(), threading.Event()
    def blocking(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {'engine': 'fixture', 'words': words}
    monkeypatch.setattr(server, 'transcript_local', blocking)
    first = client.post(f'/api/videos/{vid}/transcript', json={})
    try:
        assert entered.wait(3)
        assert client.post(f'/api/videos/{vid}/transcript', json={}).status_code == 409
        assert client.delete(f'/api/videos/{vid}').status_code == 409
    finally:
        release.set()
    assert wait_task(client, first.json()['task_id'])['status'] == 'done'


def test_live_preview_is_authenticated_and_serves_only_capture_assets(studio, monkeypatch):
    server, client, vid, _ = studio
    target = server.RECORDINGS / 'recording.ts'
    directory = server.preview_directory(target)
    directory.mkdir()
    (directory / 'segment_000000.ts').write_bytes(b'fixture-segment')
    (directory / 'index.m3u8').write_text('#EXTM3U\n#EXTINF:2,\nsegment_000000.ts\n')
    server.LIVE['test'] = {'id': 'test', 'target': target, 'status': 'recording', 'url': 'https://twitch.tv/test', 'platform': 'twitch'}
    path = '/api/live/test/preview/index.m3u8'
    assert client.get(path).headers['content-type'].startswith('application/vnd.apple.mpegurl')
    assert client.get(path).headers['cache-control'] == 'no-store'
    assert client.get('/api/live/test/preview/master.m3u8').status_code == 404
    monkeypatch.setattr(server.studio_auth, 'configured', lambda: True)
    monkeypatch.setattr(server.studio_auth, 'read_session', lambda req: None)
    assert client.get(path).status_code == 401


def test_capture_end_saves_mp4_then_transcribes_even_without_analysis(studio):
    server, client, vid, _ = studio
    target = server.RECORDINGS / 'completed.ts'
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', server.video_info(vid)['path'],
                    '-c', 'copy', '-f', 'mpegts', str(target)], check=True)
    server.LIVE['ended'] = {'target': target, 'platform': 'fixture', 'status': 'stopping',
                            'analyze_after': False, 'transcribe_after': True, 'auto_preview': False}
    server.watch_live('ended', SimpleNamespace(wait=lambda: 0), target, io.StringIO())
    info = server.LIVE['ended']
    assert info['video_id'] and info['analysis_task_id']
    assert not target.exists(), 'The TS duplicate must be reclaimed after MP4 succeeds'
    task = wait_task(client, info['analysis_task_id'])
    assert task['status'] == 'done', task
    assert client.get(f"/api/videos/{info['video_id']}/transcript").json()['available'] is True


def test_auto_previews_include_captions_only_for_clips_with_speech(studio, monkeypatch, tmp_path):
    server, _, vid, _ = studio
    server._transcribe_for_video(vid)
    work = tmp_path / 'proposals'
    work.mkdir()
    clips = [{'id': 'spoken', 'start': 0, 'end': 1.5}, {'id': 'silent', 'start': 1.6, 'end': 3}]
    server.write_json(work / 'manifest.json', {'clips': clips})
    monkeypatch.setattr(server, 'analyze', lambda *a, **k: work)
    rendered = []
    monkeypatch.setattr(server, 'render_one', lambda video_id, clip_id, quality, layout, burn, style:
                        rendered.append((clip_id, burn)))
    result = server._prepare_recorded_video(vid, transcribe_after=True, analyze_after=True, auto_preview=True)
    assert result['previews'] == 2
    assert rendered == [('spoken', True), ('silent', False)]


def test_live_monitor_reserves_space_for_mp4_copy(studio, monkeypatch):
    server, _, _, _ = studio
    target = server.RECORDINGS / 'growing.ts'
    with target.open('wb') as stream:
        stream.truncate(120 * 1024 * 1024)
    server.LIVE['limited'] = {'target': target, 'status': 'recording'}
    stopped = []
    process = SimpleNamespace(poll=lambda: None, terminate=lambda: stopped.append(True))
    monkeypatch.setattr(server.shutil, 'disk_usage', lambda _: SimpleNamespace(free=150 * 1024 * 1024))
    server.monitor_live_storage('limited', process)
    assert stopped == [True]
    assert server.LIVE['limited']['status'] == 'stopping'
    assert 'reservar espaço' in server.LIVE['limited']['message']


def test_live_state_keeps_transcription_error_visible_after_task_completion(studio):
    server, client, _, _ = studio
    server.TASKS['finished'] = {'id': 'finished', 'status': 'done', 'message': 'Preparando prévia 3 de 3.',
                               'result': {'transcription': 'error', 'transcription_message': 'Sem áudio para transcrever.'}}
    server.LIVE['complete'] = {'target': server.RECORDINGS / 'ended.ts', 'status': 'completed',
                               'platform': 'fixture', 'url': 'https://twitch.tv/test', 'analysis_task_id': 'finished'}
    info = client.get('/api/lives').json()['lives'][0]
    assert info['analysis_message'] == 'Sem áudio para transcrever.'
