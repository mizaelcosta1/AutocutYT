"""Exercise public sign-up through the same endpoints used by the browser."""
import re
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
import account_workspaces
import public_accounts
import server
import studio_auth

ORIGIN = 'https://studio.test'
PASSWORD = 'senha longa para testar'


@pytest.fixture
def studio(tmp_path, monkeypatch):
    base = tmp_path / 'legacy'
    base.mkdir()
    volume = tmp_path / 'expanded'
    volume.mkdir()
    (volume / '.autoclips-volume').touch()
    for key, value in {'PUBLIC_URL': ORIGIN, 'DATA_ROOT': str(base), 'PUBLIC_SIGNUP': '1',
                       'EXPANDED_VOLUME': str(volume), 'SESSION_SECRET': 'a' * 48,
                       'SMTP_FROM': 'studio@example.org', 'SMTP_HOST': 'smtp.example.org'}.items():
        monkeypatch.setenv('AUTOCLIPS_' + key, value)
    monkeypatch.delenv('AUTOCLIPS_ALLOWED_EMAILS', raising=False)
    accounts = account_workspaces.Accounts(base, server._load_initial_tasks)
    monkeypatch.setattr(server, 'ACCOUNTS', accounts)
    inbox = {}
    monkeypatch.setattr(public_accounts, 'send_code', inbox.__setitem__)
    return TestClient(server.app, base_url=ORIGIN), TestClient(server.app, base_url=ORIGIN), inbox, volume


def credentials(email):
    return {'email': email, 'password': PASSWORD}


def signup(client, email, inbox):
    assert client.post('/login/email/register', json=credentials(email)).status_code == 202
    assert re.fullmatch('[0-9]{6}', inbox[email])
    assert client.post('/login/email/signin', json=credentials(email)).status_code == 401
    return client.post('/login/email/confirm', json={'email': email, 'code': inbox[email]})


def test_registration_and_private_volume(studio):
    alice, bob, inbox, volume = studio
    email = 'alice@example.org'
    assert alice.get('/api/videos').status_code == 401
    assert signup(alice, email, inbox).json()['authenticated']
    assert signup(bob, 'bob@example.org', inbox).status_code == 200
    aid = alice.post('/api/upload/start', json={'size': 2, 'filename': 'a.mp4', 'authorized': True}).json()['upload_id']
    assert alice.put(f'/api/upload/{aid}/chunk/0', content=b'ab').status_code == 200
    assert bob.get(f'/api/upload/{aid}').status_code == 404
    owner = studio_auth.storage_owner(email)
    ws = server.ACCOUNTS.get(owner)
    assert ws.ROOT == volume / 'studios' / owner
    assert (ws.UPLOADS / aid / 'video.part').read_bytes() == b'ab'
    first = ws.MEDIA / 'only-alice.mp4'
    first.write_bytes(b'private')
    vid = 'a' * 32
    server.write_json(ws.INDEX, {vid: {'id': vid, 'name': first.name, 'path': str(first), 'created_at': 'now'}})
    assert alice.get(f'/api/videos/{vid}/stream').content == b'private'
    assert bob.get('/api/videos').json() == {'videos': []}
    assert bob.get(f'/api/videos/{vid}/stream').status_code == 404
    ws.CONNECTION_VAULT.save({'selected': {}, 'providers': {}, 'social': {}, 'pending': {}, 'private': 'alice'})
    assert 'private' not in server.ACCOUNTS.get(studio_auth.storage_owner('bob@example.org')).CONNECTION_VAULT.read()
    alice.get('/logout')
    assert alice.get('/api/videos').status_code == 401
    assert alice.post('/login/email/signin', json=credentials(email)).status_code == 200
    assert alice.get('/api/videos').json()['videos'][0]['id'] == vid
    assert alice.post('/login/email/confirm', json={'email': email, 'code': inbox[email]}).status_code == 400
    assert server.ACCOUNTS.get(owner).ROOT == ws.ROOT
    (volume / '.autoclips-volume').unlink()
    assert alice.get('/api/videos').status_code == 503


def test_expiry_attempt_limits_and_binding(studio):
    a, b, inbox, _ = studio
    email = 'pending@example.org'
    assert a.post('/login/email/register', json=credentials(email)).status_code == 202
    body = {'email': email, 'code': inbox[email]}
    assert b.post('/login/email/confirm', json=body).status_code == 400
    assert a.post('/login/email/register', json=credentials(email)).status_code == 429
    wrong = '999999' if inbox[email] != '999999' else '888888'
    for _ in range(5):
        assert a.post('/login/email/confirm', json={**body, 'code': wrong}).status_code == 400
    assert a.post('/login/email/confirm', json=body).status_code == 400
    with sqlite3.connect(public_accounts.data_root() / 'accounts.sqlite3') as db:
        db.execute('UPDATE pending SET tries=0, expires=?', (int(time.time())-1,))
    assert a.post('/login/email/confirm', json=body).status_code == 400


def test_delivery_failure_allows_retry_and_validation_redacts_password(studio, monkeypatch):
    a, _, inbox, _ = studio
    def fail(email, code):
        raise OSError()
    monkeypatch.setattr(public_accounts, 'send_code', fail)
    data = credentials('retry@example.org')
    assert a.post('/login/email/register', json=data).status_code == 503
    monkeypatch.setattr(public_accounts, 'send_code', inbox.__setitem__)
    assert a.post('/login/email/register', json=data).status_code == 202
    assert a.post('/login/email/register', json=data, headers={'Origin':'https://other.example'}).status_code == 403
    data['password'] = 'private' * 100
    response = a.post('/login/email/register', json=data)
    assert response.status_code == 422 and 'private' not in response.text


def test_background_task_scoped_after_request(studio):
    a, b, inbox, _ = studio
    for client, email in ((a,'a@example.org'), (b,'b@example.org')):
        assert signup(client, email, inbox).status_code == 200
    ws = server.ACCOUNTS.get(studio_auth.storage_owner('a@example.org'))
    marker = account_workspaces.active.set(ws)
    try:
        tid = server.schedule('check', lambda: str(server.studio_state().ROOT))
    finally:
        account_workspaces.active.reset(marker)
    for _ in range(100):
        task = a.get('/api/tasks/' + tid).json()
        if task.get('status') == 'done':
            break
        time.sleep(.01)
    assert task['result'] == str(ws.ROOT)
    assert b.get('/api/tasks/' + tid).status_code == 404
