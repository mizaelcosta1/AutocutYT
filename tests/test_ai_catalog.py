"""Catalog and pricing regressions without credentials or live API requests."""
import json

import pytest

from ai_catalog import (
    OPENROUTER_BASE, OPENROUTER_FREE_ROUTER, PROVIDER_PRESETS,
    classify_model, normalize_models, public_catalog,
)


def test_public_catalog_has_official_presets_and_independent_copies():
    catalog = public_catalog()
    assert catalog["source"] == {
        "name": "awesome-freellm-apis",
        "url": "https://github.com/open-free-llm-api/awesome-freellm-apis",
        "license": "MIT", "verified_at": "2026-11-19",
    }
    presets = {item["id"]: item for item in catalog["providers"]}
    assert set(presets) == {"openrouter", "groq", "gemini", "nvidia", "mistral"}
    assert presets["openrouter"]["endpoint"] == OPENROUTER_BASE
    assert presets["gemini"]["endpoint"].endswith("/v1beta/openai")
    assert all(item["key_url"].startswith("https://") and item["docs_url"].startswith("https://") for item in presets.values())
    assert all(item["quota_note"] and item["price_note"] for item in presets.values())
    catalog["providers"][0]["endpoint"] = "changed"
    assert public_catalog()["providers"][0]["endpoint"] == OPENROUTER_BASE
    assert PROVIDER_PRESETS["openrouter"]["endpoint"] == OPENROUTER_BASE


@pytest.mark.parametrize("model_id", ["vendor/chat:free", "vendor/zero-priced-preview"])
def test_explicit_zero_prices_are_free_with_or_without_suffix(model_id):
    result = classify_model({"id": model_id, "pricing": {
        "prompt": "0.000", "completion": "0e-9", "request": 0,
        "image": "0", "input_cache_read": "0", "currency": "USD",
    }}, "openrouter")
    assert result["price_class"] == "free"
    assert result["chat_compatible"] is True
    assert "Gratuito" in result["price_label"]


@pytest.mark.parametrize("field", ["prompt", "completion", "request", "image", "web_search", "input_cache_write", "future_fee"])
def test_any_positive_component_wins_over_free_suffix(field):
    pricing = {"prompt": "0", "completion": "0", field: "0.00000000000000000001"}
    assert classify_model({"id": "vendor/chat:free", "pricing": pricing}, "openrouter")["price_class"] == "paid"


@pytest.mark.parametrize("pricing", [
    None, {}, {"prompt": "0"}, {"completion": "0"},
    {"prompt": "0", "completion": None},
    {"prompt": "0", "completion": ""},
    {"prompt": "0", "completion": "NaN"},
    {"prompt": "0", "completion": float("nan")},
    {"prompt": "0", "completion": "Infinity"},
    {"prompt": "0", "completion": "-0.01"},
    {"prompt": "0", "completion": False},
    {"prompt": "0", "completion": "0", "request": None},
    {"prompt": "0", "completion": "0", "image": "not-a-price"},
])
def test_missing_malformed_or_negative_prices_never_become_zero(pricing):
    result = classify_model({"id": "vendor/chat:free", "pricing": pricing}, "openrouter")
    assert result["price_class"] == "unknown"
    json.dumps(result, allow_nan=False)


def test_partial_positive_price_is_paid_even_with_an_unknown_component():
    assert classify_model({"id": "chat", "pricing": {"prompt": "0.1", "completion": "NaN"}})["price_class"] == "paid"


def test_official_router_is_free_only_for_openrouter_and_never_invented():
    router = {"id": OPENROUTER_FREE_ROUTER}
    assert classify_model(router, "openrouter")["price_class"] == "free"
    assert classify_model(router, "custom")["price_class"] == "unknown"
    assert "roteador" in classify_model(router, "openrouter")["price_label"]
    assert classify_model({**router, "pricing": {"prompt": "1"}}, "openrouter")["price_class"] == "paid"
    assert classify_model({**router, "pricing": {"prompt": "NaN"}}, "openrouter")["price_class"] == "unknown"
    assert [item["id"] for item in normalize_models({"data": ["chat"]}, "openrouter")] == ["chat"]


def test_free_models_after_three_hundred_paid_models_are_kept_and_sorted_first():
    paid = [{"id": f"paid-{i}", "name": f"A paid {i}", "pricing": {"prompt": "1", "completion": "1"}} for i in range(350)]
    free = {"id": "free-preview", "name": "Z free", "pricing": {"prompt": "0", "completion": "0"}}
    result = normalize_models({"data": paid + [free, {"id": OPENROUTER_FREE_ROUTER}]}, "openrouter")
    assert len(result) == 352
    assert [item["id"] for item in result[:2]] == [OPENROUTER_FREE_ROUTER, "free-preview"]


@pytest.mark.parametrize(("model", "expected"), [
    ({"id": "vision-chat", "architecture": {"output_modalities": ["text", "image"]}}, True),
    ({"id": "image-only", "architecture": {"output_modalities": ["image"]}}, False),
    ({"id": "audio-only", "architecture": {"output_modalities": ["audio"]}}, False),
    ({"id": "empty", "architecture": {"output_modalities": []}}, False),
    ({"id": "whisper-large-v3"}, False),
    ({"id": "text-embedding-3-small"}, False),
    ({"id": "nvidia/nemotron-embed-1b"}, False),
    ({"id": "vendor/rerank-v2"}, False),
    ({"id": "vendor/whispering-chat"}, True),
    ({"id": "vendor/reranker-chat", "architecture": {"output_modalities": ["text"]}}, True),
    ({"id": "chat", "endpoints": ["/api/v1/chat/completions", "/api/v1/responses"]}, True),
    ({"id": "transcriber", "endpoints": ["/api/v1/audio/transcriptions"], "architecture": {"output_modalities": ["text"]}}, False),
    ({"id": "mistral-chat", "capabilities": {"completion_chat": True}}, True),
    ({"id": "mistral-embed", "capabilities": {"completion_chat": False}}, False),
    ({"id": "gemini", "supportedGenerationMethods": ["generateContent", "countTokens"]}, True),
    ({"id": "gemini", "supported_generation_methods": ["embedContent"]}, False),
])
def test_chat_compatibility_uses_explicit_capabilities_before_name_heuristics(model, expected):
    normalized = classify_model(model)
    assert normalized["chat_compatible"] is expected
    assert classify_model(normalized)["chat_compatible"] is expected


@pytest.mark.parametrize("response", [
    {"models": ["models/gemini-test"]},
    {"data": {"models": [{"name": "models/gemini-test"}]}},
    {"data": [{"id": "models/gemini-test"}]},
])
def test_supported_response_envelopes_and_native_model_prefix(response):
    assert normalize_models(response)[0]["id"] == "gemini-test"


@pytest.mark.parametrize("response", [None, [], {}, {"data": {}}, {"data": []}, {"data": None}, {"data": "bad"}, {"models": [None, {}, {"id": "x" * 257}]}])
def test_invalid_or_empty_model_responses_raise_value_error(response):
    with pytest.raises(ValueError):
        normalize_models(response)


def test_only_public_metadata_is_returned_without_nested_secrets():
    model = {
        "id": "chat", "name": {"api_key": "never-return-this"},
        "description": {"api_key": "never-return-this"},
        "api_key": "never-return-this", "context_length": {"secret": "never-return-this"},
        "pricing": {"prompt": "0", "completion": "0", "api_key": "never-return-this", "request": {"secret": "never-return-this"}},
    }
    result = classify_model(model)
    assert "never-return-this" not in json.dumps(result)
    assert result["name"] == "chat" and result["description"] == ""
    assert result["price_class"] == "unknown"
    assert "api_key" not in result["pricing"]


def test_normalization_keeps_input_unchanged_and_deduplicates_ids():
    source = {"id": "models/chat", "name": "Chat", "pricing": {"prompt": "0", "completion": "0"}}
    before = json.dumps(source, sort_keys=True)
    result = normalize_models({"data": [source, source, None, {"id": "bad\nmodel"}]})
    assert len(result) == 1 and result[0]["id"] == "chat"
    assert json.dumps(source, sort_keys=True) == before
    result[0]["pricing"]["prompt"] = "changed"
    assert source["pricing"]["prompt"] == "0"
