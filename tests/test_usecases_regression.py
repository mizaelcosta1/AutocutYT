"""No paid API calls: regression tests for user-visible AutoClips actions."""
import importlib
import json
import sys
from types import SimpleNamespace
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


def fresh_server(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_DATA_ROOT", str(tmp_path / "data"))
    for name in ("AUTOCLIPS_GOOGLE_CLIENT_ID", "AUTOCLIPS_GOOGLE_CLIENT_SECRET",
                 "AUTOCLIPS_SESSION_SECRET", "AUTOCLIPS_ALLOWED_EMAILS",
                 "AUTOCLIPS_COMFY_WORKFLOWS_JSON"):
        monkeypatch.delenv(name, raising=False)
    for name in ("server", "studio_auth"):
        sys.modules.pop(name, None)
    return importlib.import_module("server")


class UploadResponse:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = ""
        self.body = body or {}
    def json(self):
        return self.body


class YouTubeStub:
    def __init__(self, acks):
        self.acks = list(acks)
        self.chunks = []
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return None
    def post(self, *args, **kwargs):
        return UploadResponse(200, {"Location": "https://www.googleapis.com/upload/resumable/test"})
    def put(self, url, *, headers, content):
        self.chunks.append((headers["Content-Range"], content))
        if self.acks:
            return UploadResponse(308, self.acks.pop(0))
        return UploadResponse(201, body={"id": "test-video-id"})


@pytest.mark.parametrize("acks,expected", [
    ([{"Range": "bytes=0-1"}], [("bytes 0-5/6", b"ABCDEF"), ("bytes 2-5/6", b"CDEF")]),
    ([{}], [("bytes 0-5/6", b"ABCDEF"), ("bytes 0-5/6", b"ABCDEF")]),
])
def test_youtube_resumable_resends_unacknowledged_bytes(tmp_path, monkeypatch, acks, expected):
    import integrations
    monkeypatch.setattr(integrations, "get_youtube_access_token", lambda *_args, **_kwargs: ("fake-token", {"id": "UC_test"}))
    stub = YouTubeStub(acks)
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: stub)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"ABCDEF")
    result = integrations.upload_youtube_video(None, video, "test", privacy_status="private")
    assert stub.chunks == expected
    assert result["youtube_video_id"] == "test-video-id"


def test_youtube_resumable_stops_unacknowledged_infinite_loop(tmp_path, monkeypatch):
    import integrations
    monkeypatch.setattr(integrations, "get_youtube_access_token", lambda *_args, **_kwargs: ("fake-token", {"id": "UC_test"}))
    stub = YouTubeStub([{}] * 30)
    monkeypatch.setattr(integrations.httpx, "Client", lambda **kwargs: stub)
    video = tmp_path / "short.mp4"
    video.write_bytes(b"ABCDEF")
    with pytest.raises(ValueError, match="progresso|tentativas"):
        integrations.upload_youtube_video(None, video, "test")
    assert len(stub.chunks) <= 6


def test_vod_import_rejects_unsupported_host_without_scheduling(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/true")
    sent = []
    monkeypatch.setattr(server, "schedule", lambda *args, **kwargs: sent.append(args) or "fake-task")
    client = TestClient(server.app)
    for url in ("https://example.net/video", "https://youtube.com.evil.net/watch?v=x",
                "https://127.0.0.1/video", "https://www.youtube.com:8000/video"):
        response = client.post("/api/vod/download", json={"url": url, "authorized": True})
        assert response.status_code == 400, (url, response.text)
    assert sent == []
    legitimate = client.post("/api/vod/download", json={"url": "https://www.youtube.com/watch?v=example", "authorized": True})
    assert legitimate.status_code == 200
    assert len(sent) == 1


def test_upload_start_refuses_more_bytes_than_storage(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: SimpleNamespace(free=20*1024*1024))
    client = TestClient(server.app)
    result = client.post("/api/upload/start", json={"filename": "big.mp4", "size": 70*1024*1024, "authorized": True})
    assert result.status_code == 413, result.text
    assert not list(server.UPLOADS.iterdir())


def test_ai_title_button_capability_false_until_configured(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    response = TestClient(server.app).get('/api/config')
    assert response.status_code == 200
    assert response.json()["ai_titles_ready"] is False


def test_comfy_catalog_is_explicit_and_curated(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    client = TestClient(server.app)
    empty = client.get("/api/creative/workflows")
    assert empty.status_code == 200
    assert empty.json() == {"configured": False, "execution": "external", "workflows": []}
    monkeypatch.setenv("AUTOCLIPS_COMFY_WORKFLOWS_JSON", json.dumps([
        {"title": "Frame vertical", "workflow_id": "fe5b7c97-11fa-448b-bb4e-12edd5bb72e0"},
        {"title": "Link externo", "workflow_id": "../../malicious"},
        {"title": "fake endpoint", "workflow_id": "https://evil.com/"},
    ]))
    response = client.get("/api/creative/workflows")
    assert response.status_code == 200
    data = response.json()
    assert data["configured"] is True
    assert data["execution"] == "external"
    assert data["workflows"] == [{"title": "Frame vertical", "url": "https://cloud.comfy.org/#fe5b7c97-11fa-448b-bb4e-12edd5bb72e0"}]
    assert "../../malicious" not in response.text


def test_comfy_catalog_requires_studio_auth_on_deployment(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_ID", "id.apps.googleusercontent.com")
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET", "server-secret")
    monkeypatch.setenv("AUTOCLIPS_SESSION_SECRET", "x" * 40)
    monkeypatch.setenv("AUTOCLIPS_ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("AUTOCLIPS_PUBLIC_URL", "https://app.example.com")
    response = TestClient(server.app).get("/api/creative/workflows")
    assert response.status_code == 401


def test_published_clip_cannot_be_published_twice_and_edits_clear_stale_youtube_state(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    source = tmp_path / 'demo.mp4'; source.write_bytes(b'video-source')
    final = tmp_path / 'final.mp4'; final.write_bytes(b'final-video')
    work = tmp_path / 'work'; work.mkdir()
    video_id = 'f' * 32
    server.save_video({
        'id': video_id, 'name': 'demo.mp4', 'path': str(source), 'duration': 20.0,
        'platform': 'upload', 'created_at': '2026-01-01', 'size': source.stat().st_size,
        'width': 320, 'height': 180, 'thumbnail': None, 'job_path': str(work)
    })
    clip = {
        'id': 'clip_01', 'title': 'Publicado', 'start': 0, 'end': 5,
        'selected': True, 'status': 'final_ready',
        'final': {'path': str(final)},
        'youtube': {'youtube_video_id': 'yt123', 'url': 'https://www.youtube.com/watch?v=yt123'},
    }
    server.write_json(work / 'manifest.json', {
        'clips': [clip], 'probe': {'duration': 20.0}, 'ranking': 'manual', 'transcription_engine': 'none'
    })
    monkeypatch.setattr(server, 'schedule', lambda *args, **kwargs: 'should-not-be-scheduled')
    client = TestClient(server.app)
    duplicate = client.post(f'/api/videos/{video_id}/clips/clip_01/publish/youtube', json={'approved_publish': True})
    assert duplicate.status_code == 409, duplicate.text
    update = client.put(f'/api/videos/{video_id}/clips', json={'clips': [
        {'id': 'clip_01', 'title': 'Novo', 'start': 1, 'end': 5, 'selected': True}
    ]})
    assert update.status_code == 200
    manifest = client.get(f'/api/videos/{video_id}/manifest').json()
    assert manifest['clips'][0].get('youtube') is None
    assert manifest['clips'][0]['youtube_history'][0]['youtube_video_id'] == 'yt123'
    assert manifest['clips'][0]['final_url'] is None


def test_publish_prevents_two_concurrent_upload_tasks(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    source = tmp_path / 'demo.mp4'; source.write_bytes(b'video-source')
    final = tmp_path / 'final.mp4'; final.write_bytes(b'final-video')
    work = tmp_path / 'work'; work.mkdir()
    video_id = 'e' * 32
    server.save_video({'id': video_id, 'name': 'demo.mp4', 'path': str(source),
                      'duration': 20.0, 'platform': 'upload', 'created_at': '2026-01-01',
                      'size': 12, 'width': 320, 'height': 180, 'thumbnail': None, 'job_path': str(work)})
    server.write_json(work / 'manifest.json', {
        'clips': [{'id': 'clip_01', 'title': 'Novo', 'start': 0, 'end': 5,
                   'selected': True, 'final': {'path': str(final)}}],
        'probe': {'duration': 20.0}, 'ranking': 'manual', 'transcription_engine': 'none'
    })
    queued = []
    monkeypatch.setattr(server.POOL, 'submit', queued.append)

    def fail_upload(*args, **kwargs):
        raise RuntimeError('falha de envio simulada')

    monkeypatch.setattr(server, 'upload_youtube_video', fail_upload)
    client = TestClient(server.app)
    project = f'/api/videos/{video_id}'
    path = f'{project}/clips/clip_01/publish/youtube'
    sent = client.post(path, json={'approved_publish': True})
    assert sent.status_code == 200, sent.text
    task_id = sent.json()['task_id']
    assert client.get(f'/api/tasks/{task_id}').json()['status'] == 'queued'
    assert client.get(f'{project}/activity').json() == {'activity': {
        'task_id': task_id, 'kind': 'publish-youtube', 'status': 'queued',
    }}
    assert client.post(path, json={'approved_publish': True}).status_code == 409
    worker, = queued
    worker()
    assert client.get(f'/api/tasks/{task_id}').json()['status'] == 'error'
    assert client.get(f'{project}/activity').json() == {'activity': None}


def test_edit_changed_clip_reclaims_only_managed_previous_renders(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    source = tmp_path / 'source.mp4'; source.write_bytes(b'video')
    outside = tmp_path / 'external.mp4'; outside.write_bytes(b'not-managed')
    work = tmp_path / 'work'; work.mkdir()
    p = work / 'preview'; p.mkdir()
    f = work / 'final'; f.mkdir()
    prev = p / 'clip_01.mp4'; prev.write_bytes(b'preview')
    subtitle = p / 'clip_01.srt'; subtitle.write_text('subtitle')
    fin = f / 'clip_01.mp4'; fin.write_bytes(b'final')
    video_id = 'c' * 32
    server.save_video({'id': video_id, 'name': 'source.mp4', 'path': str(source),
                       'duration': 20.0, 'platform': 'upload', 'created_at': '2026-01-01',
                       'size': 5, 'width': 320, 'height': 180, 'thumbnail': None, 'job_path': str(work)})
    server.write_json(work / 'manifest.json', {
        'clips': [{'id': 'clip_01', 'title': 'Sample', 'start': 0, 'end': 3,
                   'selected': True, 'preview': {'path': str(prev), 'subtitles': str(subtitle)},
                   'final': {'path': str(fin), 'subtitles': str(outside)}}],
        'probe': {'duration': 20.0}, 'ranking': 'manual', 'transcription_engine': 'none',
    })
    response = TestClient(server.app).put(f'/api/videos/{video_id}/clips', json={
        'clips': [{'id': 'clip_01', 'title': 'Changed', 'start': 1, 'end': 3, 'selected': True}]})
    assert response.status_code == 200, response.text
    assert not prev.exists() and not fin.exists() and not subtitle.exists()
    assert outside.read_bytes() == b'not-managed'


def test_cannot_change_or_remove_a_clip_while_youtube_upload_in_progress(tmp_path, monkeypatch):
    server = fresh_server(tmp_path, monkeypatch)
    source = tmp_path / 'demo.mp4'; source.write_bytes(b'source')
    final = tmp_path / 'final.mp4'; final.write_bytes(b'final')
    work = tmp_path / 'work'; work.mkdir()
    video_id = 'd' * 32
    server.save_video({'id': video_id, 'name': 'demo.mp4', 'path': str(source),
                       'duration': 20.0, 'platform': 'upload', 'created_at': '2026-01-01',
                       'size': 6, 'width': 320, 'height': 180, 'thumbnail': None, 'job_path': str(work)})
    server.write_json(work / 'manifest.json', {
        'clips': [{'id': 'clip_01', 'title': 'Sample', 'start': 0, 'end': 3,
                   'selected': True, 'final': {'path': str(final)}}],
        'probe': {'duration': 20.0}, 'ranking': 'manual', 'transcription_engine': 'none',
    })
    queued = []
    monkeypatch.setattr(server.POOL, 'submit', queued.append)
    monkeypatch.setattr(server, 'upload_youtube_video',
                        lambda *args, **kwargs: {'youtube_video_id': 'test-upload'})
    client = TestClient(server.app)
    project = f'/api/videos/{video_id}'
    sent = client.post(f'{project}/clips/clip_01/publish/youtube', json={'approved_publish': True})
    assert sent.status_code == 200
    task_id = sent.json()['task_id']
    assert client.get(f'{project}/activity').json()['activity']['task_id'] == task_id
    changes = {'clips': [{'id': 'clip_01', 'title': 'Changed', 'start': 1, 'end': 3, 'selected': True}]}
    editing = client.put(f'{project}/clips', json=changes)
    removing = client.delete(f'{project}/clips/clip_01')
    assert editing.status_code == 409, editing.text
    assert removing.status_code == 409, removing.text
    worker, = queued
    worker()
    assert client.get(f'/api/tasks/{task_id}').json()['status'] == 'done'
    assert client.get(f'{project}/activity').json() == {'activity': None}
    assert client.put(f'{project}/clips', json=changes).status_code == 200
    assert client.delete(f'{project}/clips/clip_01').status_code == 200
