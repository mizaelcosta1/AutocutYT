"""Test Google sign-in as studio authentication, separate from YouTube linking."""
import importlib
import sys
from urllib.parse import parse_qs, urlparse
from fastapi.testclient import TestClient
import pytest

@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTOCLIPS_DATA_ROOT', str(tmp_path))
    monkeypatch.setenv('AUTOCLIPS_GOOGLE_CLIENT_ID', 'studio-oidc-client.apps.googleusercontent.com')
    monkeypatch.setenv('AUTOCLIPS_GOOGLE_CLIENT_SECRET', 'server-only-secret')
    monkeypatch.setenv('AUTOCLIPS_SESSION_SECRET', 'r' * 48)
    monkeypatch.setenv('AUTOCLIPS_ALLOWED_EMAILS', 'owner@example.com')
    monkeypatch.setenv('AUTOCLIPS_PUBLIC_URL', 'https://studio.example.com')
    monkeypatch.delenv('AUTOCLIPS_PASSWORD', raising=False)
    for name in ['studio_auth', 'server']:
        sys.modules.pop(name, None)
    server = importlib.import_module('server')
    return server, TestClient(server.app, base_url="https://studio.example.com")


def test_studio_requires_google_auth_not_admin_password(setup):
    _server, client = setup
    assert client.get('/', follow_redirects=False).headers['location'] == '/login'
    assert client.get('/api/social/accounts', follow_redirects=False).status_code == 401
    login = client.get('/login')
    assert login.status_code == 200
    assert 'Entrar com Google' in login.text
    assert 'senha administrativa' not in login.text.lower()
    assert 'AUTOCLIPS_GOOGLE_CLIENT_SECRET' not in login.text


def test_google_login_uses_oidc_email_pkce_and_nonce(setup):
    _server, client = setup
    redirect = client.get('/login/google', follow_redirects=False)
    assert redirect.status_code == 302
    target = urlparse(redirect.headers['location'])
    query = parse_qs(target.query)
    assert target.hostname == 'accounts.google.com'
    assert query['scope'] == ['openid email profile']
    assert query['redirect_uri'] == ['https://studio.example.com/login/google/callback']
    assert query['code_challenge_method'] == ['S256']
    assert len(query['nonce'][0]) >= 20
    assert len(query['state'][0]) >= 20
    assert query['access_type'] == ['online']
    assert 'youtube.upload' not in target.query


def test_valid_login_session_and_logout(setup, monkeypatch):
    server, client = setup
    state = parse_qs(urlparse(client.get('/login/google', follow_redirects=False).headers['location']).query)['state'][0]
    import studio_auth
    monkeypatch.setattr(studio_auth, 'exchange_code', lambda code, verifier: {'id_token': 'test.id.token'})
    monkeypatch.setattr(studio_auth, 'verify_identity', lambda raw, client_id: {
        'iss': 'https://accounts.google.com', 'aud': client_id,
        'email_verified': True, 'email': 'owner@example.com',
        'nonce': studio_auth.read_oauth_cookie(client.cookies.get('ac_google_oauth'))['nonce'],
    })
    response = client.get('/login/google/callback', params={'code': 'approved', 'state': state}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers['location'] == '/'
    assert client.get('/').status_code == 200
    assert client.get('/api/auth/session').json()['email'] == 'owner@example.com'
    assert client.get('/api/social/accounts').status_code == 200
    assert client.get('/logout', follow_redirects=False).status_code == 303
    assert client.get('/api/social/accounts').status_code == 401


def test_unapproved_email_refused(setup, monkeypatch):
    _, client = setup
    import studio_auth
    state = parse_qs(urlparse(client.get('/login/google', follow_redirects=False).headers['location']).query)['state'][0]
    monkeypatch.setattr(studio_auth, 'exchange_code', lambda code, verifier: {'id_token': 'test.id.token'})
    monkeypatch.setattr(studio_auth, 'verify_identity', lambda raw, client_id: {
        'iss': 'https://accounts.google.com', 'aud': client_id,
        'email_verified': True, 'email': 'stranger@example.com',
        'nonce': studio_auth.read_oauth_cookie(client.cookies.get('ac_google_oauth'))['nonce'],
    })
    response = client.get('/login/google/callback', params={'code':'approved','state':state}, follow_redirects=False)
    assert response.status_code == 403
    assert client.get('/api/social/accounts').status_code == 401


def test_csrf_mismatch_refused(setup):
    _, client = setup
    client.get('/login/google', follow_redirects=False)
    assert client.get('/login/google/callback', params={'code':'approved','state':'wrong'}, follow_redirects=False).status_code == 400
