"""Exercise download identities, delivery boundaries and render/owner handoff."""
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

import server
from integrations import Vault
from storage_delivery import DeliveryError, StorageDelivery

VID = 'd' * 32


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.delenv('AUTOCLIPS_GOOGLE_CLIENT_ID', raising=False)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    monkeypatch.setattr(server, 'TASKS_FILE', tmp_path / 'tasks.json')
    monkeypatch.setattr(server, 'TASKS', {})
    monkeypatch.setattr(server, 'PROCESSING_VIDEOS', {})
    original = tmp_path / 'original.mp4'
    original.write_bytes(b'original media')
    work = tmp_path / 'work'
    work.mkdir()
    output = work / 'export.mp4'
    output.write_bytes(b'completed render bytes')
    manifest = {'source': str(original), 'sha256': server.sha256_file(original),
                'probe': {'audio_codec': None}, 'clips': [{'id': 'clip_01', 'title': 'Meu corte',
                'start': 0, 'end': 1, 'preview': {'path': str(output), 'render_id': 'exact123',
                'burn': False, 'layout': 'fit'}, 'status': 'preview_ready'}]}
    server.write_json(work / 'manifest.json', manifest)
    item = {'id': VID, 'path': str(original), 'duration': 1, 'name': 'original.mp4'}
    monkeypatch.setattr(server, 'video_info', lambda video_id: item if video_id == VID else
                        (_ for _ in ()).throw(server.HTTPException(404)))
    monkeypatch.setattr(server, 'get_manifest', lambda _: (json.loads((work / 'manifest.json').read_text()), work, item))
    service = StorageDelivery(tmp_path, Vault(tmp_path))
    monkeypatch.setattr(server, 'STORAGE_DELIVERY', service)
    return TestClient(server.app), service, output, work


def connect_fake_drive(service):
    service._save_connection('local', 'drive', {'account_id': 'test-account', 'folder_id': 'folder123',
                              'access_token': 'sample-access', 'refresh_token': 'sample-refresh'})


def delivery_body(**changes):
    return {'kind': 'clip', 'clip_id': 'clip_01', 'quality': 'preview', 'render_id': 'exact123',
            'provider': 'drive', **changes}


def test_download_sets_attachment_and_exact_version(setup):
    client, _, output, _ = setup
    path = f'/api/videos/{VID}/rendered/clip_01/preview'
    inline = client.get(path, params={'v': 'exact123'})
    assert inline.status_code == 200
    assert inline.headers['content-disposition'].startswith('inline;')
    download = client.get(path, params={'v': 'exact123', 'download': 1})
    assert download.content == output.read_bytes()
    assert download.headers['content-disposition'].startswith('attachment;')
    assert 'previa-720p-exact123.mp4' in download.headers['content-disposition']
    assert 'no-store' in download.headers['cache-control']
    assert download.headers['x-content-type-options'] == 'nosniff'
    assert client.get(path, params={'v': 'replaced', 'download': 1}).status_code == 409


def test_legacy_exports_still_download_and_send_with_file_revision(setup):
    client, service, output, work = setup
    connect_fake_drive(service)
    manifest = json.loads((work / 'manifest.json').read_text())
    manifest.update(transcription_engine='none', ranking='manual')
    manifest['probe']['duration'] = 1
    del manifest['clips'][0]['preview']['render_id']
    server.write_json(work / 'manifest.json', manifest)
    clip = client.get(f'/api/videos/{VID}/manifest').json()['clips'][0]
    identity = clip['preview_options']['render_id']
    assert identity.startswith('legacy-')
    assert client.get(clip['preview_url'] + '&download=1').content == output.read_bytes()
    assert client.post(f'/api/videos/{VID}/deliveries', json=delivery_body(render_id=identity)).status_code == 200
    output.write_bytes(b'a replacement revision')
    assert client.get(clip['preview_url'] + '&download=1').status_code == 409
    assert client.post(f'/api/videos/{VID}/deliveries', json=delivery_body(render_id=identity)).status_code == 409


def test_timeline_download_has_identity_and_attachment(setup):
    client, _, output, _ = setup
    doc, path = server._timeline_document(VID)
    server.write_json(path.parent / 'timeline_exports.json', {'final': {
        'version': doc['version'], 'path': str(output), 'render_id': 'timeline123', 'subtitles': False}})
    url = f'/api/videos/{VID}/timeline/rendered/final'
    response = client.get(url, params={'render_id': 'timeline123', 'download': 1})
    assert response.status_code == 200
    assert response.content == output.read_bytes()
    assert 'attachment;' in response.headers['content-disposition']
    assert 'final-1080p-timeline123.mp4' in response.headers['content-disposition']
    assert client.get(url, params={'render_id': 'old', 'download': 1}).status_code == 409


def test_settings_require_explicit_connection_and_never_return_tokens(setup):
    client, service, _, _ = setup
    initial = client.get('/api/storage/settings')
    assert initial.status_code == 200
    assert initial.json()['auto_download'] is False
    assert initial.json()['auto_upload'] is False
    assert client.patch('/api/storage/settings', json={'auto_upload': True}).status_code == 422
    connect_fake_drive(service)
    update = client.patch('/api/storage/settings', json={'auto_upload': True, 'auto_download': True,
                          'qualities': ['preview', 'final']})
    assert update.status_code == 200
    assert update.json()['drive']['connected'] is True
    assert 'sample-access' not in update.text and 'sample-refresh' not in update.text
    assert client.patch('/api/storage/settings', json={'auto_upload': 'true'}).status_code == 422
    assert client.patch('/api/storage/settings', json={'qualities': []}).status_code == 422


def test_storage_mutations_reject_foreign_origin(setup):
    client, _, _, _ = setup
    response = client.patch('/api/storage/settings', json={'auto_download': True},
                            headers={'Origin': 'https://untrusted.example'})
    assert response.status_code == 403
    assert client.get('/api/storage/settings').json()['auto_download'] is False


def test_github_invalid_form_does_not_echo_secret(setup):
    client, _, _, _ = setup
    response = client.post('/api/storage/github', json={'token': 'do-not-echo-this-sample', 'repository': 123})
    assert response.status_code == 422
    assert 'do-not-echo' not in response.text


def test_delivery_validates_identity_pins_exact_bytes_and_deduplicates(setup):
    client, service, output, _ = setup
    connect_fake_drive(service)
    url = f'/api/videos/{VID}/deliveries'
    assert client.post(url, json=delivery_body(render_id='stale')).status_code == 409
    assert client.post(url, json=delivery_body(path='/etc/passwd')).status_code == 422
    assert client.post(url, json=delivery_body(kind='timeline')).status_code == 422
    response = client.post(url, json=delivery_body())
    assert response.status_code == 200, response.text
    job = response.json()['job']
    assert job['status'] == 'queued' and job['render_id'] == 'exact123'
    assert 'path' not in job and 'owner' not in job and 'remote' not in job
    assert client.post(url, json=delivery_body()).json()['job']['id'] == job['id']
    assert len(client.get(url).json()['jobs']) == 1
    output.unlink()
    assert Path(service.jobs[job['id']]['path']).read_bytes() == b'completed render bytes'


def test_disconnect_stops_automation_without_deleting_render(setup):
    client, service, output, _ = setup
    connect_fake_drive(service)
    service.configure('local', {'auto_upload': True})
    response = client.post('/api/storage/drive/disconnect')
    assert response.status_code == 200
    assert response.json()['auto_upload'] is False
    assert response.json()['drive']['connected'] is False
    assert output.exists()


def test_completed_render_queues_its_new_identity_and_survives_upload_error(setup, monkeypatch):
    _, service, _, work = setup
    connect_fake_drive(service)
    service.configure('local', {'auto_upload': True, 'qualities': ['preview']})
    monkeypatch.setattr(server, '_require_render_space', lambda *_args, **_kwargs: None)
    def encode(_source, destination, _clip, **_kwargs):
        destination.write_bytes(b'newly rendered exact bytes')
        return {'duration': 1, 'width': 720, 'height': 1280}
    monkeypatch.setattr(server, 'render_clip', encode)
    marker = server.REQUEST_OWNER.set('local')
    try:
        result = server.render_one(VID, 'clip_01', 'preview', 'fit', False)
        assert result['delivery_job']['status'] == 'queued'
        assert result['render_id'] in result['url']
        job = service.jobs[result['delivery_job']['id']]
        assert Path(job['path']).read_bytes() == b'newly rendered exact bytes'
        assert job['render_id'] == result['render_id']
        def rejected(*_args, **_kwargs):
            raise DeliveryError('Reconecte o armazenamento.')
        monkeypatch.setattr(service, 'enqueue', rejected)
        second = server.render_one(VID, 'clip_01', 'preview', 'fit', False)
        assert second['delivery_error'] == 'Reconecte o armazenamento.'
        assert 'delivery_job' not in second
        latest = json.loads((work / 'manifest.json').read_text())['clips'][0]['preview']
        assert latest['render_id'] == second['render_id']
        assert Path(latest['path']).is_file()
    finally:
        server.REQUEST_OWNER.reset(marker)


def test_scheduler_captures_owner_for_render_after_browser_leaves(setup, monkeypatch):
    _, _, _, _ = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    captured = []
    original_save = server._save_tasks
    def save():
        original_save()
        if any(task.get('status') == 'done' for task in server.TASKS.values()):
            finished.set()
    monkeypatch.setattr(server, '_save_tasks', save)
    def work():
        captured.append(server.TASK_CONTEXT.delivery_owner)
        entered.set()
        assert release.wait(2)
        return {'finished': True}
    marker = server.REQUEST_OWNER.set('account-hash')
    try:
        task_id = server.schedule('delivery-owner-test', work, video_id=VID)
    finally:
        server.REQUEST_OWNER.reset(marker)
    assert entered.wait(2)
    assert server.REQUEST_OWNER.get() is None
    assert captured == ['account-hash']
    release.set()
    assert finished.wait(2)
    assert server.TASKS[task_id]['status'] == 'done'


def test_authenticated_request_hands_owner_to_real_render_queue(setup, monkeypatch):
    client, service, _, _ = setup
    connect_fake_drive(service)
    service.configure('local', {'auto_upload': True, 'qualities': ['preview']})
    finished = threading.Event()
    monkeypatch.setattr(server, '_require_render_space', lambda *_args, **_kwargs: None)
    def encode(_source, destination, _clip, **_kwargs):
        destination.write_bytes(b'request render')
        return {'duration': 1, 'width': 720, 'height': 1280}
    monkeypatch.setattr(server, 'render_clip', encode)
    original_save = server._save_tasks
    def save():
        original_save()
        if any(task.get('status') in {'done', 'error'} for task in server.TASKS.values()):
            finished.set()
    monkeypatch.setattr(server, '_save_tasks', save)
    response = client.post(f'/api/videos/{VID}/render/preview', json={
        'clip_id': 'clip_01', 'layout': 'fit', 'subtitles': False, 'approved_selection': True})
    assert response.status_code == 200, response.text
    assert finished.wait(3)
    task = client.get('/api/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'done', task
    job = task['result']['delivery_job']
    assert service.jobs[job['id']]['owner'] == 'local'
    assert job['render_id'] == task['result']['render_id']
    assert client.get(f'/api/videos/{VID}/deliveries').json()['jobs'][0]['id'] == job['id']
