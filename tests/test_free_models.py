"""Exercise the public AI routes and runtime without real providers or keys."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

import ai_runtime
import integrations
from ai_catalog import OPENROUTER_BASE, OPENROUTER_FREE_ROUTER


KEY = "test-only-provider-credential-1234"
FREE = {"id": "vendor/free-chat", "pricing": {"prompt": "0", "completion": "0"}}
PAID = {"id": "vendor/paid-chat", "pricing": {"prompt": "0.1", "completion": "0.2"}}
ROUTER = {"id": OPENROUTER_FREE_ROUTER}


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOCLIPS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(ai_runtime, "_cooldowns", {})
    # Prevent DNS and HTTP access even if a future implementation adds a call.
    monkeypatch.setattr(integrations.socket, "getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.216.34", 443))
    ])
    original_client = httpx.Client
    requests = []
    handler = [None]

    def dispatch(request):
        requests.append(request)
        if handler[0] is None:
            pytest.fail("Unexpected provider request: " + request.method + " " + request.url.path)
        return handler[0](request)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(dispatch)
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    vault = integrations.Vault(tmp_path / "vault")
    app = FastAPI()
    app.include_router(integrations.build_router(vault))

    def seed(models=None, *, provider_id="provider-one", kind="openrouter", key=KEY):
        state = vault.read()
        state["providers"][provider_id] = {
            "id": provider_id, "kind": kind, "name": "Test provider",
            "endpoint": OPENROUTER_BASE if kind == "openrouter" else "https://provider.example/v1",
            "api_key": key, "models": models if models is not None else [ROUTER, FREE, PAID],
            "created_at": "2026-11-19T00:00:00+00:00", "updated_at": "2026-11-19T00:00:00+00:00",
        }
        vault.save(state)
        return provider_id

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, vault=vault, seed=seed, requests=requests,
                              respond=lambda function: handler.__setitem__(0, function))


def test_catalog_and_presets_are_available_and_invalid_preset_never_calls_network(api):
    response = api.client.get("/api/ai/catalog")
    assert response.status_code == 200
    catalog = response.json()
    assert catalog["source"]["license"] == "MIT"
    presets = {item["id"]: item for item in catalog["providers"]}
    assert set(presets) == {"openrouter", "groq", "gemini", "nvidia", "mistral"}
    assert presets["gemini"]["endpoint"].endswith("/v1beta/openai")
    invalid = api.client.post("/api/ai/providers", json={
        "kind": "custom", "name": "Test", "api_key": KEY,
        "preset_id": "unrecognized", "endpoint": "https://invalid.example/v1",
    })
    assert invalid.status_code == 422
    assert not api.requests and not api.vault.read()["providers"]

    api.respond(lambda request: httpx.Response(200, json={"data": [{"id": "groq-chat"}]}))
    added = api.client.post("/api/ai/providers", json={
        "kind": "custom", "name": "Ignored preset name", "api_key": KEY,
        "preset_id": "groq", "endpoint": "https://ignored.example/v1",
    })
    assert added.status_code == 201, added.text
    assert added.json()["provider"]["endpoint"] == presets["groq"]["endpoint"]
    assert str(api.requests[0].url) == presets["groq"]["endpoint"] + "/models"
    assert api.requests[0].headers["authorization"] == "Bearer " + KEY


def test_connect_validates_key_separately_from_public_catalog_and_encrypts_it(api):
    def respond(request):
        if request.url.path.endswith("/key"):
            assert request.headers["authorization"] == "Bearer " + KEY
            return httpx.Response(200, json={"data": {
                "limit_remaining": 0, "api_key": KEY,
                "free_model_daily_requests": {"used": 2, "limit": 50, "remaining": 48, "secret": KEY},
            }})
        assert request.url.path.endswith("/models")
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [ROUTER, FREE, PAID]})

    api.respond(respond)
    created = api.client.post("/api/ai/providers", json={
        "kind": "openrouter", "name": "OpenRouter", "api_key": KEY, "preset_id": "openrouter",
    })
    assert created.status_code == 201, created.text
    assert [request.url.path for request in api.requests] == ["/api/v1/key", "/api/v1/models"]
    assert all(request.method == "GET" for request in api.requests)
    provider = created.json()["provider"]
    assert provider["key_saved"] is True and provider["free_router_available"] is True
    assert provider["quota"]["remaining"] == 48 and "secret" not in provider["quota"]
    assert api.vault.read()["providers"][provider["id"]]["api_key"] == KEY
    assert KEY.encode() not in api.vault.path.read_bytes()
    assert KEY not in created.text and "api_key" not in created.text
    assert KEY not in api.client.get("/api/ai/providers").text


def test_price_and_chat_filters_are_applied_before_pagination(api):
    models = [{"id": f"paid-{index}", "name": f"A paid {index}", "pricing": PAID["pricing"]} for index in range(350)]
    models += [
        {**FREE, "id": "free-a", "name": "Z free A"},
        {**FREE, "id": "free-b", "name": "Z free B"},
        {"id": "unknown-a"}, {"id": "unknown-b:free"},
        {**FREE, "id": "whisper-large-v3"},
    ]
    provider_id = api.seed(models)
    path = f"/api/ai/providers/{provider_id}/models"
    first = api.client.get(path, params={"cost": "free", "limit": 1}).json()
    second = api.client.get(path, params={"cost": "free", "offset": 1, "limit": 1}).json()
    assert first["total"] == second["total"] == 2
    assert first["models"][0]["id"] == "free-a" and first["has_more"] is True
    assert second["models"][0]["id"] == "free-b" and second["has_more"] is False
    assert first["counts"] == {"free": 2, "paid": 350, "unknown": 2, "all": 354}
    for cost, total in [("paid", 350), ("unknown", 2)]:
        data = api.client.get(path, params={"cost": cost, "limit": 1}).json()
        assert data["total"] == total and data["models"][0]["price_class"] == cost
    assert not api.requests


def test_non_chat_model_cannot_be_selected_even_when_its_price_is_zero(api):
    provider_id = api.seed([FREE, {**FREE, "id": "embed-only", "architecture": {"output_modalities": ["embeddings"]}}])
    response = api.client.post("/api/ai/selection", json={
        "stage": "moments", "provider_id": provider_id, "model_id": "embed-only",
    })
    assert response.status_code == 422
    assert not api.vault.read()["selected"]
    assert not api.requests


def test_refresh_preserves_free_guard_and_removes_selection_when_price_changes(api, monkeypatch):
    provider_id = api.seed([FREE, PAID])
    for stage, model in [("moments", FREE), ("review", PAID)]:
        selected = api.client.post("/api/ai/selection", json={
            "stage": stage, "provider_id": provider_id, "model_id": model["id"],
        })
        assert selected.status_code == 200
    assert api.vault.read()["selected"]["moments"]["free_only"] is True
    monkeypatch.setattr(integrations, "discover_models", lambda *args: [FREE, PAID])
    assert api.client.post(f"/api/ai/providers/{provider_id}/refresh").status_code == 200
    assert integrations.get_selected(api.vault)["free_only"] is True
    paid_formerly_free = {**FREE, "pricing": {"prompt": "0.01", "completion": "0"}}
    monkeypatch.setattr(integrations, "discover_models", lambda *args: [paid_formerly_free, PAID])
    assert api.client.post(f"/api/ai/providers/{provider_id}/refresh").status_code == 200
    assert integrations.get_selected(api.vault) is None
    assert "moments" not in api.vault.read()["selected"]
    assert api.vault.read()["selected"]["review"]["model_id"] == PAID["id"]
    assert not api.requests


def test_free_defaults_configures_two_tasks_and_exposes_no_key(api):
    provider_id = api.seed()
    api.client.post("/api/ai/selection", json={"stage": "review", "provider_id": provider_id, "model_id": PAID["id"]})
    response = api.client.post(f"/api/ai/providers/{provider_id}/free-defaults")
    assert response.status_code == 200
    selected = response.json()["selected"]
    for stage in ("moments", "titles"):
        assert selected[stage] == {"provider_id": provider_id, "model_id": OPENROUTER_FREE_ROUTER, "free_only": True}
        assert integrations.get_selected(api.vault, stage)["free_only"] is True
    assert selected["review"]["model_id"] == PAID["id"]
    assert KEY not in response.text and "api_key" not in response.text
    assert not api.requests


def test_free_probe_sends_one_small_text_request_with_zero_price_caps(api):
    provider_id = api.seed()
    api.respond(lambda request: httpx.Response(200, json={
        "model": "vendor/actual-free-model", "choices": [{"message": {"content": "OK"}}],
    }))
    response = api.client.post(f"/api/ai/providers/{provider_id}/test")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["model_id"] == "vendor/actual-free-model"
    assert len(api.requests) == 1
    request = api.requests[0]
    body = json.loads(request.content)
    assert request.method == "POST" and str(request.url) == OPENROUTER_BASE + "/chat/completions"
    assert request.headers["authorization"] == "Bearer " + KEY
    assert body["model"] == OPENROUTER_FREE_ROUTER
    assert body["messages"] == [{"role": "user", "content": "Responda apenas OK."}]
    assert body["max_tokens"] <= 64
    assert body["provider"]["max_price"] == {"prompt": 0, "completion": 0, "request": 0, "image": 0}
    assert "models" not in body and "plugins" not in body
    assert KEY not in response.text and "api_key" not in response.text


@pytest.mark.parametrize("status", [401, 402])
def test_provider_auth_and_billing_errors_are_sanitized_without_retry(api, status):
    provider_id = api.seed()
    raw_error = "Raw provider error with private data: " + KEY
    api.respond(lambda request: httpx.Response(status, json={"error": {"message": raw_error}}))
    response = api.client.post(f"/api/ai/providers/{provider_id}/test")
    assert response.status_code == status
    assert len(api.requests) == 1
    assert KEY not in response.text and raw_error not in response.text
    assert "Nenhum modelo pago" in response.json()["detail"]


def test_retry_after_cooldown_is_shared_by_models_but_not_other_credentials(api, monkeypatch):
    provider_id = api.seed()
    now = [1000.0]
    monkeypatch.setattr(ai_runtime.time, "monotonic", lambda: now[0])
    api.respond(lambda request: httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"message": KEY}}))
    first = api.client.post(f"/api/ai/providers/{provider_id}/test")
    assert first.status_code == 429 and first.headers["retry-after"] == "60"
    assert KEY not in first.text
    second = api.client.post(f"/api/ai/providers/{provider_id}/test")
    assert second.status_code == 429 and len(api.requests) == 1
    selected = {"kind": "openrouter", "endpoint": OPENROUTER_BASE, "api_key": KEY,
                "model_id": FREE["id"], "free_only": True}
    with pytest.raises(ai_runtime.ModelRequestError) as error:
        ai_runtime.chat_completion(selected, [{"role": "user", "content": "OK"}])
    assert error.value.status_code == 429 and len(api.requests) == 1
    api.respond(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    assert ai_runtime.chat_completion({**selected, "api_key": "different-test-credential"}, [])['content'] == "OK"
    assert len(api.requests) == 2
    now[0] += 61
    assert ai_runtime.chat_completion(selected, [])['content'] == "OK"
    assert len(api.requests) == 3


def test_http_date_retry_after_becomes_positive_cooldown(api):
    provider_id = api.seed()
    retry_date = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=5), usegmt=True)
    api.respond(lambda request: httpx.Response(429, headers={"Retry-After": retry_date}))
    response = api.client.post(f"/api/ai/providers/{provider_id}/test")
    assert response.status_code == 429
    assert 290 <= int(response.headers["retry-after"]) <= 300
    assert api.client.post(f"/api/ai/providers/{provider_id}/test").status_code == 429
    assert len(api.requests) == 1


def test_migration_runs_once_and_preserves_other_provider_selection(api):
    provider_id = api.seed()
    other_id = api.seed([{"id": "custom-chat"}], provider_id="provider-two", kind="custom", key="other-test-key")
    state = api.vault.read()
    original_titles = {"provider_id": provider_id, "model_id": PAID["id"]}
    other_selection = {"provider_id": other_id, "model_id": "custom-chat"}
    state["selected"] = {"moments": other_selection, "titles": original_titles}
    api.vault.save(state)
    first = integrations.migrate_free_defaults(api.vault)
    assert first == {"migrated": True, "tasks_configured": ["titles"]}
    upgraded = api.vault.read()
    assert upgraded["selected"]["moments"] == other_selection
    assert upgraded["selected"]["titles"]["model_id"] == OPENROUTER_FREE_ROUTER
    assert upgraded["selected"]["titles"]["free_only"] is True
    assert upgraded["selection_before_free_defaults"]["titles"] == original_titles
    assert upgraded["free_models_upgrade"] == 1
    # A subsequent deliberate choice must survive future application startups.
    chosen = api.client.post("/api/ai/selection", json={"stage": "titles", "provider_id": provider_id, "model_id": PAID["id"]})
    assert chosen.status_code == 200
    before = api.vault.read()
    assert integrations.migrate_free_defaults(api.vault) == {"migrated": False}
    assert api.vault.read() == before
    assert not api.requests


def test_migration_protects_legacy_free_selection_without_router_from_later_charges(api, monkeypatch):
    provider_id = api.seed([FREE])
    state = api.vault.read()
    state["selected"]["titles"] = {"provider_id": provider_id, "model_id": FREE["id"]}
    api.vault.save(state)
    assert integrations.migrate_free_defaults(api.vault) == {"migrated": True, "tasks_configured": []}
    assert api.vault.read()["selected"]["titles"]["free_only"] is True
    assert integrations.get_selected(api.vault, "titles")["model_id"] == FREE["id"]

    paid_formerly_free = {**FREE, "pricing": {"prompt": "0.01", "completion": "0"}}
    monkeypatch.setattr(integrations, "discover_models", lambda *args: [paid_formerly_free])
    assert api.client.post(f"/api/ai/providers/{provider_id}/refresh").status_code == 200
    assert integrations.get_selected(api.vault, "titles") is None
    assert "titles" not in api.vault.read()["selected"]
    metadata = integrations.generate_clip_metadata(api.vault, "Um trecho simples para verificar o título local.")
    assert metadata["source"] == "heuristic"
    assert not api.requests


def test_legacy_custom_openrouter_with_explicit_https_port_is_canonicalized_and_protected(api):
    provider_id = api.seed([FREE], kind="custom")
    state = api.vault.read()
    state["providers"][provider_id]["endpoint"] = "https://openrouter.ai:443/api/v1/"
    state["selected"]["moments"] = {"provider_id": provider_id, "model_id": FREE["id"]}
    api.vault.save(state)
    assert integrations.migrate_free_defaults(api.vault)["migrated"] is True
    provider = api.vault.read()["providers"][provider_id]
    assert provider["kind"] == "openrouter" and provider["endpoint"] == OPENROUTER_BASE
    assert api.vault.read()["selected"]["moments"]["free_only"] is True
    selected = integrations.get_selected(api.vault)
    assert selected["kind"] == "openrouter" and selected["endpoint"] == OPENROUTER_BASE
    assert selected["free_only"] is True

    api.respond(lambda request: httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]}))
    assert ai_runtime.chat_completion(selected, [{"role": "user", "content": "OK"}])["content"] == "OK"
    assert len(api.requests) == 1
    assert all(value == 0 for value in json.loads(api.requests[0].content)["provider"]["max_price"].values())


def test_router_with_positive_price_cannot_be_selected_or_used_from_legacy_state(api):
    provider_id = api.seed([{**ROUTER, "pricing": {"prompt": "0", "completion": "0.02"}}])
    state = api.vault.read()
    state["selected"]["titles"] = {"provider_id": provider_id, "model_id": OPENROUTER_FREE_ROUTER}
    api.vault.save(state)
    response = api.client.post("/api/ai/selection", json={
        "stage": "moments", "provider_id": provider_id, "model_id": OPENROUTER_FREE_ROUTER,
    })
    assert response.status_code == 422
    assert "moments" not in api.vault.read()["selected"]
    assert integrations.get_selected(api.vault, "titles") is None
    assert api.client.post(f"/api/ai/providers/{provider_id}/test").status_code == 422
    assert not api.requests


def test_new_custom_openrouter_with_https_port_uses_authenticated_discovery(api):
    def respond(request):
        assert request.url.host == "openrouter.ai"
        if request.url.path == "/api/v1/key":
            assert request.headers["authorization"] == "Bearer " + KEY
            return httpx.Response(200, json={"data": {}})
        assert request.url.path == "/api/v1/models"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [ROUTER, FREE]})

    api.respond(respond)
    response = api.client.post("/api/ai/providers", json={
        "kind": "custom", "name": "My OpenRouter", "api_key": KEY,
        "endpoint": "https://openrouter.ai:443/api/v1/",
    })
    assert response.status_code == 201, response.text
    provider = response.json()["provider"]
    assert provider["kind"] == "openrouter" and provider["endpoint"] == OPENROUTER_BASE
    assert provider["free_router_available"] is True
    assert [request.url.path for request in api.requests] == ["/api/v1/key", "/api/v1/models"]
    assert KEY not in response.text
    assert api.client.post(f'/api/ai/providers/{provider["id"]}/free-defaults').status_code == 200
    assert integrations.get_selected(api.vault)["free_only"] is True
