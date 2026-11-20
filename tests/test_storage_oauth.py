"""Drive connection authorization stays separate from studio sign-in.

Exercise the real router, signed cookies and callback decisions. Google token
exchange and identity verification are replaced so these tests never contact an
account or create an external folder.
"""
import base64
import hashlib
import importlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


ORIGIN = "https://studio.example.com"
OWNER_EMAIL = "owner@example.com"
OTHER_EMAIL = "other@example.com"
ACCESS_TOKEN = "test-access-token-never-public"
REFRESH_TOKEN = "test-refresh-token-never-public"
ID_TOKEN = "test.id-token.never-public"


@pytest.fixture
def oauth(monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_ID", "test-studio.apps.googleusercontent.com")
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET", "test-client-secret-never-public")
    monkeypatch.setenv("AUTOCLIPS_SESSION_SECRET", "test-session-key-" * 4)
    monkeypatch.setenv("AUTOCLIPS_ALLOWED_EMAILS", f"{OWNER_EMAIL},{OTHER_EMAIL}")
    monkeypatch.setenv("AUTOCLIPS_PUBLIC_URL", ORIGIN)
    auth = importlib.import_module("studio_auth")
    connections = []
    exchanges = []
    monkeypatch.setattr(auth, "DRIVE_CONNECT_HANDLER", lambda *args: connections.append(args))
    app = FastAPI()
    app.include_router(auth.router)
    with TestClient(app, base_url=ORIGIN) as client:
        context = SimpleNamespace(auth=auth, client=client, connections=connections, exchanges=exchanges)
        yield context


def _session(context, email=OWNER_EMAIL):
    context.client.cookies.set(
        context.auth.SESSION_COOKIE,
        context.auth.serializer("studio-session").dumps({"email": email}),
        domain="studio.example.com", path="/",
    )


def _start(context, *, login=False):
    if not login:
        _session(context)
    path = "/login/google" if login else "/auth/storage/drive/start"
    response = context.client.get(path, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    pending = context.auth.read_oauth_cookie(context.client.cookies.get(context.auth.OAUTH_COOKIE))
    return response, query, pending


def _provider(context, monkeypatch, pending, *, email=OWNER_EMAIL, verified=True, nonce=None, scope=None):
    token = {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN,
             "id_token": ID_TOKEN, "expires_in": 3600,
             "scope": "openid email profile " + context.auth.DRIVE_SCOPE if scope is None else scope}
    identity = {"sub": "google-account-123", "email": email,
                "email_verified": verified, "nonce": pending["nonce"] if nonce is None else nonce}

    def exchange(code, verifier):
        context.exchanges.append((code, verifier))
        return token

    monkeypatch.setattr(context.auth, "exchange_code", exchange)

    def verify(raw, client_id):
        assert raw == ID_TOKEN
        assert client_id == "test-studio.apps.googleusercontent.com"
        return identity

    monkeypatch.setattr(context.auth, "verify_identity", verify)
    return token, identity


def _callback(context, pending, **params):
    return context.client.get("/login/google/callback",
                              params={"code": "approved-code", "state": pending["state"], **params},
                              follow_redirects=False)


def _assert_no_tokens(response):
    exposed = response.text + repr(dict(response.headers))
    for value in (ACCESS_TOKEN, REFRESH_TOKEN, ID_TOKEN, "test-client-secret-never-public"):
        assert value not in exposed


def test_drive_start_requires_an_active_studio_session(oauth):
    response = oauth.client.get("/auth/storage/drive/start", follow_redirects=False)
    assert response.status_code == 401
    assert "location" not in response.headers
    assert not oauth.connections
    assert oauth.client.cookies.get(oauth.auth.OAUTH_COOKIE) is None


def test_drive_start_requires_registered_connection_handler(oauth, monkeypatch):
    _session(oauth)
    monkeypatch.setattr(oauth.auth, "DRIVE_CONNECT_HANDLER", None)
    assert oauth.client.get("/auth/storage/drive/start", follow_redirects=False).status_code == 401


def test_drive_start_binds_purpose_and_user_with_offline_scope_and_pkce(oauth):
    response, query, pending = _start(oauth)
    assert urlsplit(response.headers["location"]).hostname == "accounts.google.com"
    assert set(query["scope"][0].split()) == {"openid", "email", "profile", oauth.auth.DRIVE_SCOPE}
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["login_hint"] == [OWNER_EMAIL]
    assert query["redirect_uri"] == [ORIGIN + "/login/google/callback"]
    assert query["code_challenge_method"] == ["S256"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == [challenge]
    assert pending["purpose"] == "drive"
    assert pending["email"] == OWNER_EMAIL
    assert query["state"] == [pending["state"]]
    assert query["nonce"] == [pending["nonce"]]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "Path=/login/google" in cookie
    _assert_no_tokens(response)


def test_valid_drive_callback_connects_private_owner_without_returning_tokens(oauth, monkeypatch):
    _, _, pending = _start(oauth)
    previous_session = oauth.client.cookies.get(oauth.auth.SESSION_COOKIE)
    token, identity = _provider(oauth, monkeypatch, pending, email=" OWNER@EXAMPLE.COM ")
    response = _callback(oauth, pending)
    assert response.status_code == 303
    assert response.headers["location"] == "/?storage=connected#connections"
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert oauth.exchanges == [("approved-code", pending["verifier"])]
    assert oauth.connections == [(hashlib.sha256(OWNER_EMAIL.encode()).hexdigest(), token, identity)]
    assert oauth.client.cookies.get(oauth.auth.SESSION_COOKIE) == previous_session
    assert oauth.client.cookies.get(oauth.auth.OAUTH_COOKIE) is None
    _assert_no_tokens(response)


def test_normal_login_does_not_expand_scopes_or_invoke_drive_handler(oauth, monkeypatch):
    _, query, pending = _start(oauth, login=True)
    assert query["scope"] == ["openid email profile"]
    assert query["access_type"] == ["online"]
    assert query["prompt"] == ["select_account"]
    assert pending["purpose"] == "login" and "email" not in pending
    _provider(oauth, monkeypatch, pending)
    response = _callback(oauth, pending)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert not oauth.connections
    assert oauth.client.get("/api/auth/session").json() == {"authenticated": True, "email": OWNER_EMAIL}
    _assert_no_tokens(response)


def test_drive_consent_denial_returns_generic_connection_error(oauth, monkeypatch):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending)
    response = _callback(oauth, pending, code="", error="access_denied-" + ACCESS_TOKEN)
    assert response.status_code == 303
    assert response.headers["location"] == "/?storage=error#connections"
    assert not oauth.exchanges and not oauth.connections
    assert oauth.client.cookies.get(oauth.auth.OAUTH_COOKIE) is None
    _assert_no_tokens(response)


@pytest.mark.parametrize("scope", ["", "openid email profile", "https://www.googleapis.com/auth/drive.file.extra"])
def test_drive_callback_requires_exact_granted_scope(oauth, monkeypatch, scope):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending, scope=scope)
    response = _callback(oauth, pending)
    assert response.status_code == 303
    assert response.headers["location"] == "/?storage=error#connections"
    assert not oauth.connections
    _assert_no_tokens(response)


@pytest.mark.parametrize("state", ["", "different-state", "estado-inválido"])
def test_invalid_state_is_rejected_before_token_exchange(oauth, monkeypatch, state):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending)
    response = _callback(oauth, pending, state=state)
    assert response.status_code == 400
    assert not oauth.exchanges and not oauth.connections
    _assert_no_tokens(response)


@pytest.mark.parametrize("cookie_failure", ["missing", "tampered", "expired"])
def test_untrusted_or_expired_oauth_cookie_is_rejected(oauth, monkeypatch, cookie_failure):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending)
    if cookie_failure == "expired":
        monkeypatch.setattr(oauth.auth, "OAUTH_MAX_AGE", -1)
    else:
        oauth.client.cookies.jar.clear(domain="studio.example.com", path="/login/google", name=oauth.auth.OAUTH_COOKIE)
        if cookie_failure == "tampered":
            oauth.client.cookies.set(oauth.auth.OAUTH_COOKIE, "untrusted-cookie", domain="studio.example.com", path="/login/google")
    response = _callback(oauth, pending)
    assert response.status_code == 400
    assert not oauth.exchanges and not oauth.connections


@pytest.mark.parametrize("mismatch", ["google_account", "active_session", "signed_out"])
def test_drive_connection_cannot_cross_accounts_or_survive_sign_out(oauth, monkeypatch, mismatch):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending, email=OTHER_EMAIL if mismatch == "google_account" else OWNER_EMAIL)
    if mismatch == "active_session":
        _session(oauth, OTHER_EMAIL)
    elif mismatch == "signed_out":
        oauth.client.cookies.jar.clear(domain="studio.example.com", path="/", name=oauth.auth.SESSION_COOKIE)
    response = _callback(oauth, pending)
    assert response.status_code == 403
    assert not oauth.connections
    _assert_no_tokens(response)


@pytest.mark.parametrize("failure", ["nonce", "unverified_email", "not_allowed"])
def test_drive_connection_requires_verified_google_identity(oauth, monkeypatch, failure):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending,
              email="stranger@example.com" if failure == "not_allowed" else OWNER_EMAIL,
              verified=failure != "unverified_email",
              nonce="wrong-nonce" if failure == "nonce" else None)
    response = _callback(oauth, pending)
    assert response.status_code == 403
    assert not oauth.connections
    _assert_no_tokens(response)


@pytest.mark.parametrize("exception", [ValueError, httpx.HTTPError])
def test_storage_handler_failure_does_not_reflect_provider_secrets(oauth, monkeypatch, exception):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending)

    def failing_handler(*_args):
        raise exception("Provider failure containing " + ACCESS_TOKEN + " " + REFRESH_TOKEN)

    monkeypatch.setattr(oauth.auth, "DRIVE_CONNECT_HANDLER", failing_handler)
    response = _callback(oauth, pending)
    assert response.status_code == 303
    assert response.headers["location"] == "/?storage=error#connections"
    _assert_no_tokens(response)


def test_missing_authorization_code_cannot_connect_drive(oauth, monkeypatch):
    _, _, pending = _start(oauth)
    _provider(oauth, monkeypatch, pending)
    response = _callback(oauth, pending, code="")
    assert response.status_code == 400
    assert not oauth.exchanges and not oauth.connections


def test_token_exchange_failure_is_generic_and_never_connects(oauth, monkeypatch):
    _, _, pending = _start(oauth)

    def failing_exchange(_code, _verifier):
        raise httpx.HTTPError("Provider failure containing " + ACCESS_TOKEN)

    monkeypatch.setattr(oauth.auth, "exchange_code", failing_exchange)
    response = _callback(oauth, pending)
    assert response.status_code == 502
    assert response.json()["detail"] == "Falha na autenticação Google"
    assert not oauth.connections
    _assert_no_tokens(response)
