"""Exercise YouTube OAuth decisions without using a real account or token."""
import hashlib
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import integrations
import studio_auth


ORIGIN = "https://studio.example.com"
OWNER = "owner@example.com"
OTHER = "other@example.com"
COOKIE = "autoclips_oauth_youtube"


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    for key, value in {
        "AUTOCLIPS_GOOGLE_CLIENT_ID": "studio-client",
        "AUTOCLIPS_GOOGLE_CLIENT_SECRET": "studio-secret",
        "AUTOCLIPS_SESSION_SECRET": "test-session-secret-" * 3,
        "AUTOCLIPS_ALLOWED_EMAILS": OWNER + "," + OTHER,
        "AUTOCLIPS_PUBLIC_URL": ORIGIN,
        "AUTOCLIPS_YOUTUBE_CLIENT_ID": "youtube-client",
        "AUTOCLIPS_YOUTUBE_CLIENT_SECRET": "youtube-secret",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTOCLIPS_ENCRYPTION_KEY", raising=False)
    vault = integrations.Vault(tmp_path)
    app = FastAPI()
    app.include_router(integrations.build_router(vault))
    context = SimpleNamespace(client=TestClient(app, base_url=ORIGIN), vault=vault, exchanges=[])
    context.token = {
        "access_token": "test-access-never-reflect",
        "refresh_token": "test-refresh-never-reflect",
        "expires_in": 3600,
        "scope": integrations.YOUTUBE_READ_SCOPE + " " + integrations.YOUTUBE_UPLOAD_SCOPE,
    }
    def exchange(*args):
        context.exchanges.append(args)
        return dict(context.token)
    monkeypatch.setattr(integrations, "_exchange_code", exchange)
    monkeypatch.setattr(integrations, "_identity", lambda *args: {"id": "UC_test_channel", "name": "Canal de teste"})
    return context


def session(context, email=OWNER):
    value = studio_auth.serializer("studio-session").dumps({"email": email})
    context.client.cookies.set(studio_auth.SESSION_COOKIE, value, domain="studio.example.com", path="/")


def start(context, permission="view"):
    session(context)
    response = context.client.get("/auth/youtube/start", params={"permission": permission}, follow_redirects=False)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["state"][0], query, response


def callback(context, state, **params):
    return context.client.get("/auth/youtube/callback", params={"state": state, "code": "test-code", **params}, follow_redirects=False)


def test_start_requires_studio_session_even_when_router_is_used_directly(oauth):
    response = oauth.client.get("/auth/youtube/start", follow_redirects=False)
    assert response.status_code == 401
    assert not oauth.vault.read()["pending"]
    assert not oauth.client.cookies.get(COOKIE)


def test_start_binds_user_and_only_asks_upload_permission_when_selected(oauth):
    state, query, response = start(oauth)
    saved = oauth.vault.read()["pending"][state]
    assert saved["studio_owner"] == hashlib.sha256(OWNER.encode()).hexdigest()
    assert OWNER not in str(saved)
    assert query["scope"] == [integrations.YOUTUBE_READ_SCOPE]
    assert query["redirect_uri"] == [ORIGIN + "/auth/youtube/callback"]
    assert query["access_type"] == ["offline"]
    assert response.headers["Cache-Control"] == "no-store"
    state, query, _ = start(oauth, "publish")
    assert set(query["scope"][0].split()) == {integrations.YOUTUBE_READ_SCOPE, integrations.YOUTUBE_UPLOAD_SCOPE}


def test_wrong_browser_cannot_consume_a_valid_pending_attempt(oauth):
    state, _, _ = start(oauth)
    original_cookie = oauth.client.cookies.get(COOKIE)
    oauth.client.cookies.set(COOKIE, "wrong-browser", domain="studio.example.com", path="/auth/youtube")
    assert callback(oauth, state).status_code == 400
    assert state in oauth.vault.read()["pending"]
    assert not oauth.exchanges
    oauth.client.cookies.set(COOKIE, original_cookie, domain="studio.example.com", path="/auth/youtube")
    assert callback(oauth, state).status_code == 303


def test_switching_studio_account_during_oauth_cannot_link_to_other_user(oauth):
    state, _, _ = start(oauth)
    session(oauth, OTHER)
    response = callback(oauth, state)
    assert response.status_code == 403
    assert not oauth.exchanges
    assert not oauth.vault.read()["social"]
    assert state in oauth.vault.read()["pending"]
    session(oauth)
    assert callback(oauth, state).status_code == 303


def test_signing_out_before_callback_does_not_exchange_tokens(oauth):
    state, _, _ = start(oauth)
    oauth.client.cookies.delete(studio_auth.SESSION_COOKIE, domain="studio.example.com", path="/")
    assert callback(oauth, state).status_code == 401
    assert not oauth.exchanges
    assert state in oauth.vault.read()["pending"]


@pytest.mark.parametrize("state", ["", "invalid", "💥", "x" * 257])
def test_invalid_state_never_starts_token_exchange(oauth, state):
    real_state, _, _ = start(oauth)
    assert callback(oauth, state).status_code == 400
    assert not oauth.exchanges
    assert real_state in oauth.vault.read()["pending"]


def test_cancelled_attempt_is_consumed_cookie_cleared_and_secret_not_reflected(oauth):
    state, _, _ = start(oauth)
    oauth.client.headers["accept"] = "text/html"
    response = callback(oauth, state, error="access_denied-test-secret", code="")
    assert response.status_code == 400
    assert "Voltar às conexões" in response.text
    assert 'href="/#connections"' in response.text
    assert "test-secret" not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert not oauth.client.cookies.get(COOKIE)
    assert state not in oauth.vault.read()["pending"]
    assert not oauth.exchanges
    assert callback(oauth, state).status_code == 400


def test_forged_cancellation_cannot_discard_pending_connection(oauth):
    state, _, _ = start(oauth)
    response = callback(oauth, "wrong", error="access_denied")
    assert response.status_code == 400
    assert state in oauth.vault.read()["pending"]
    assert oauth.client.cookies.get(COOKIE)


def test_provider_failure_is_generic_and_preserves_previous_connection(oauth, monkeypatch):
    data = oauth.vault.read()
    previous = {"access_token": "previous-token", "account": {"id": "UC_previous"}}
    data["social"]["youtube"] = previous
    oauth.vault.save(data)
    state, _, _ = start(oauth)
    def failed_exchange(*args):
        raise ValueError("provider reflected SECRET_TOKEN and test-code")
    monkeypatch.setattr(integrations, "_exchange_code", failed_exchange)
    response = callback(oauth, state)
    assert response.status_code == 502
    assert response.headers["content-type"].startswith("application/json")
    assert "SECRET_TOKEN" not in response.text and "test-code" not in response.text
    assert oauth.vault.read()["social"]["youtube"] == previous
    assert state not in oauth.vault.read()["pending"]
    assert not oauth.client.cookies.get(COOKIE)


@pytest.mark.parametrize("scope,status", [("", 502), (integrations.YOUTUBE_READ_SCOPE, 403)])
def test_missing_upload_consent_cannot_save_upload_enabled_connection(oauth, scope, status):
    state, _, _ = start(oauth, "publish")
    oauth.token["scope"] = scope
    response = callback(oauth, state)
    assert response.status_code == status
    assert not oauth.vault.read()["social"]
    assert not oauth.client.cookies.get(COOKIE)


def test_success_is_one_use_and_never_exposes_tokens(oauth):
    state, _, _ = start(oauth, "publish")
    response = callback(oauth, state)
    assert response.status_code == 303
    assert response.headers["location"] == "/?connected=youtube#connections"
    assert response.headers["Cache-Control"] == "no-store"
    assert not oauth.client.cookies.get(COOKIE)
    for secret in (oauth.token["access_token"], oauth.token["refresh_token"]):
        assert secret not in str(response.headers)
        assert secret not in oauth.client.get("/api/social/accounts").text
    assert callback(oauth, state).status_code == 400
    assert len(oauth.exchanges) == 1


def test_missing_configuration_has_readable_recovery_in_browser(oauth, monkeypatch):
    session(oauth)
    monkeypatch.delenv("AUTOCLIPS_YOUTUBE_CLIENT_ID")
    response = oauth.client.get("/auth/youtube/start", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 409
    assert "Voltar às conexões" in response.text
    assert not oauth.vault.read()["pending"]
