"""Test Google sign-in as studio authentication, separate from YouTube linking."""
import importlib
import sys
from urllib.parse import parse_qs, urlparse
import httpx
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


def test_protected_project_returns_to_same_project_after_login(setup):
    _server, client = setup
    response = client.get('/studio/project_123', headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == '/login?next=%2Fstudio%2Fproject_123'
    # APIs keep structured authentication errors; no HTML redirect or action replay.
    response = client.get('/api/videos/project_123', follow_redirects=False)
    assert response.status_code == 401


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


@pytest.mark.parametrize('target', [
    'https://evil.example/phish', '//evil.example', '/\\evil.example',
    '/%2f%2fevil.example', '/studio/%2e%2e/logout', '/studio/../../logout',
    '/logout', '/login/google', '/auth/youtube/start', '/api/social/youtube/disconnect',
    '/?next=https://evil.example', '/#<script>', '/\r\nLocation: https://evil.example',
])
def test_login_next_cannot_leave_the_studio_or_run_an_action(setup, target):
    _, client = setup
    import studio_auth
    started = client.get('/login/google', params={'next': target}, follow_redirects=False)
    assert started.status_code == 302
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    assert pending['next'] == '/'


def test_login_returns_to_the_signed_project_destination(setup, monkeypatch):
    _, client = setup
    import studio_auth
    destination = '/studio/8c8938d53b1c4711a809a58db68db4d1'
    page = client.get('/login', params={'next': destination})
    assert page.status_code == 200
    assert '/login/google?next=%2Fstudio%2F8c8938d53b1c4711a809a58db68db4d1' in page.text
    started = client.get('/login/google', params={'next': destination}, follow_redirects=False)
    query = parse_qs(urlparse(started.headers['location']).query)
    assert query['redirect_uri'] == ['https://studio.example.com/login/google/callback']
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    assert pending['next'] == destination
    monkeypatch.setattr(studio_auth, 'exchange_code', lambda *_: {'id_token': 'test.id.token'})
    monkeypatch.setattr(studio_auth, 'verify_identity', lambda *_: {
        'email_verified': True, 'email': 'owner@example.com', 'nonce': pending['nonce'],
    })
    response = client.get('/login/google/callback', params={
        'code': 'approved', 'state': pending['state'], 'next': 'https://evil.example',
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == destination
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert client.get('/login', params={'next': destination}, follow_redirects=False).headers['location'] == destination
    assert client.get('/login', params={'next': '//evil.example'}, follow_redirects=False).headers['location'] == '/'


def test_login_can_return_to_connections_without_requesting_extra_scopes(setup):
    _, client = setup
    import studio_auth
    started = client.get('/login/google', params={'next': '/#connections'}, follow_redirects=False)
    query = parse_qs(urlparse(started.headers['location']).query)
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    assert pending['next'] == '/#connections'
    assert query['scope'] == ['openid email profile']
    assert studio_auth.login_url('/#connections') == '/login?next=%2F%23connections'


def test_cancelled_login_shows_a_safe_retry_screen_in_the_browser(setup):
    _, client = setup
    import studio_auth
    destination = '/studio/8c8938d53b1c4711a809a58db68db4d1'
    client.get('/login/google', params={'next': destination}, follow_redirects=False)
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    response = client.get('/login/google/callback', params={
        'state': pending['state'], 'error': 'access_denied<script>provider-secret</script>',
    }, headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 400
    assert response.headers['content-type'].startswith('text/html')
    assert 'tentar novamente' in response.text
    assert 'Entrar com Google' in response.text
    assert destination.replace('/', '%2F') in response.text
    assert 'provider-secret' not in response.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert client.cookies.get(studio_auth.OAUTH_COOKIE) is None
    assert client.cookies.get(studio_auth.SESSION_COOKIE) is None


def test_api_keeps_a_structured_error_when_login_is_cancelled(setup):
    _, client = setup
    import studio_auth
    client.get('/login/google', follow_redirects=False)
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    response = client.get('/login/google/callback', params={
        'state': pending['state'], 'error': 'access_denied',
    }, headers={'Accept': 'application/json'}, follow_redirects=False)
    assert response.status_code == 400
    assert response.json() == {'detail': 'A autorização Google foi cancelada'}


def test_expired_state_shows_a_retry_without_creating_a_session(setup):
    _, client = setup
    import studio_auth
    response = client.get('/login/google/callback', params={'code': 'private-code', 'state': 'expired'},
                          headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == 400
    assert 'tentativa de entrada expirou' in response.text
    assert 'private-code' not in response.text
    assert client.cookies.get(studio_auth.SESSION_COOKIE) is None


@pytest.mark.parametrize('failure,status,message', [
    ('provider', 502, 'Não conseguimos confirmar'),
    ('account', 403, 'conta ainda não tem acesso'),
    ('unverified', 403, 'endereço de e-mail confirmado'),
    ('nonce', 403, 'validar esta tentativa'),
])
def test_google_login_failures_keep_status_and_offer_recovery(setup, monkeypatch, failure, status, message):
    _, client = setup
    import studio_auth
    client.get('/login/google', follow_redirects=False)
    pending = studio_auth.read_oauth_cookie(client.cookies.get(studio_auth.OAUTH_COOKIE))
    def exchange(*_):
        if failure == 'provider':
            raise httpx.ConnectError('provider-secret and token details')
        return {'id_token': 'private.id.token'}
    monkeypatch.setattr(studio_auth, 'exchange_code', exchange)
    monkeypatch.setattr(studio_auth, 'verify_identity', lambda *_: {
        'email_verified': failure != 'unverified',
        'email': 'stranger@example.com' if failure == 'account' else 'owner@example.com',
        'nonce': 'não-ascii' if failure == 'nonce' else pending['nonce'],
    })
    response = client.get('/login/google/callback', params={'code': 'approved', 'state': pending['state']},
                          headers={'Accept': 'text/html'}, follow_redirects=False)
    assert response.status_code == status
    assert message in response.text
    assert 'Entrar com Google' in response.text
    assert 'provider-secret' not in response.text
    assert 'private.id.token' not in response.text
    assert 'stranger@example.com' not in response.text
    assert client.cookies.get(studio_auth.SESSION_COOKIE) is None


def test_public_login_explains_channel_connection_and_hides_server_setup(setup, monkeypatch):
    _, client = setup
    page = client.get('/login')
    assert 'Depois de entrar, abra Conexões e autorize o canal' in page.text
    assert page.headers['cache-control'] == 'no-store'
    monkeypatch.delenv('AUTOCLIPS_GOOGLE_CLIENT_SECRET')
    pending = client.get('/login')
    assert pending.status_code == 503
    assert 'Acesso em configuração' in pending.text
    assert 'AUTOCLIPS_' not in pending.text
