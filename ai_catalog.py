"""Provider presets and conservative classification of public model metadata.

Directory reference: https://github.com/open-free-llm-api/awesome-freellm-apis
(MIT, verified 2026-11-19). The presets and implementation below are original;
only provider names and public URLs are used from that directory. Endpoints are
checked against the providers' own documentation linked in each preset.

No model list, account eligibility, quota or pricing is copied from the directory.
Those are discovered from the user's provider at connection/refresh time.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re


OPENROUTER_BASE = "https://openrouter.ai/api/v1"
OPENROUTER_FREE_ROUTER = "openrouter/free"

PROVIDER_PRESETS = {
    "openrouter": {
        "id": "openrouter", "name": "OpenRouter", "kind": "openrouter",
        "endpoint": OPENROUTER_BASE,
        "key_url": "https://openrouter.ai/keys",
        "docs_url": "https://openrouter.ai/docs/quickstart",
        "quota_note": "Modelos gratuitos têm limites de uso e disponibilidade definidos pelo OpenRouter.",
        "price_note": "Consulte o preço de cada modelo. O roteador openrouter/free seleciona modelos gratuitos.",
    },
    "groq": {
        "id": "groq", "name": "Groq", "kind": "custom",
        "endpoint": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        "docs_url": "https://console.groq.com/docs/openai",
        "quota_note": "As cotas dependem do modelo e do plano da conta; confira os limites no console Groq.",
        "price_note": "Uma cota gratuita depende da conta. Preço ausente na API aparece como não informado.",
    },
    "gemini": {
        "id": "gemini", "name": "Google Gemini", "kind": "custom",
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai",
        "key_url": "https://aistudio.google.com/apikey",
        "docs_url": "https://ai.google.dev/gemini-api/docs/openai",
        "quota_note": "Cotas e elegibilidade dependem do projeto, do modelo, da região e do plano Google.",
        "price_note": "Confirme a faixa gratuita ou paga do modelo no projeto associado à chave.",
    },
    "nvidia": {
        "id": "nvidia", "name": "NVIDIA NIM", "kind": "custom",
        "endpoint": "https://integrate.api.nvidia.com/v1",
        "key_url": "https://build.nvidia.com/settings/api-keys",
        "docs_url": "https://docs.api.nvidia.com/nim/reference/llm-apis",
        "quota_note": "O acesso hospedado e seus limites dependem da conta e das condições da NVIDIA.",
        "price_note": "Consulte as condições do modelo e da conta para confirmar acesso gratuito ou cobrança.",
    },
    "mistral": {
        "id": "mistral", "name": "Mistral AI", "kind": "custom",
        "endpoint": "https://api.mistral.ai/v1",
        "key_url": "https://console.mistral.ai/api-keys",
        "docs_url": "https://docs.mistral.ai/getting-started/quickstarts/studio/activate-and-generate-api-key",
        "quota_note": "A disponibilidade da API e os limites dependem do modelo e do plano Mistral.",
        "price_note": "Confirme no plano da sua conta se há cota gratuita ou cobrança pelo uso.",
    },
}


def public_catalog() -> dict:
    """Return an independent, credential-free snapshot for the connection UI."""
    return {
        "source": {
            "name": "awesome-freellm-apis",
            "url": "https://github.com/open-free-llm-api/awesome-freellm-apis",
            "license": "MIT",
            "verified_at": "2026-11-19",
        },
        "providers": deepcopy(list(PROVIDER_PRESETS.values())),
        "notice": (
            "Preços, cotas e disponibilidade variam por modelo e conta. "
            "Confirme as condições no provedor. A lista de modelos é consultada "
            "na API ao conectar ou atualizar; preço ausente fica como não informado."
        ),
    }


_PRICE_METADATA = {"currency", "currency_code", "unit", "billing_unit"}
_PRIVATE_FIELD = re.compile(r"(?:^|_)(?:secret|key|authorization|password|credential|access_token)(?:_|$)")
_PRICE_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
# Match task names at token boundaries, not substrings in ordinary chat IDs.
_NON_CHAT_ID = re.compile(r"(?:^|[/_.:-])(?:whisper|embeddings?|embed|rerank(?:er)?)(?:$|[/_.:-])", re.I)


def _amount(value: object) -> Decimal | None:
    """Parse an explicit finite nonnegative amount without rounding to float."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    raw = str(value).strip()
    if not raw or len(raw) > 128:
        return None
    try:
        result = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _public_pricing(value: object) -> dict | None:
    """Keep amounts, never arbitrary nested provider fields or credentials.

    Unknown pricing components are retained conservatively: an invalid amount
    becomes None, so it cannot silently turn a partially priced model into free.
    Currency/unit annotations are not monetary charges and are omitted.
    """
    if not isinstance(value, dict):
        return None
    result = {}
    for key, amount in value.items():
        if not isinstance(key, str) or not _PRICE_FIELD.fullmatch(key):
            continue
        if key in _PRICE_METADATA or _PRIVATE_FIELD.search(key):
            continue
        parsed = _amount(amount)
        result[key] = str(parsed) if parsed is not None else None
    return result


def _price_class(model_id: str, pricing: dict | None, kind: str) -> str:
    amounts = {key: _amount(value) for key, value in (pricing or {}).items()}
    # Any published charge takes precedence over suffixes and router names.
    if any(value is not None and value > 0 for value in amounts.values()):
        return "paid"
    if kind == "openrouter" and model_id == OPENROUTER_FREE_ROUTER:
        # This is an officially documented free router, not a guessed model ID.
        # Explicit malformed pricing still warrants an unknown result.
        return "free" if all(value is not None for value in amounts.values()) else "unknown"
    if (
        amounts.get("prompt") == 0
        and amounts.get("completion") == 0
        and all(value is not None and value == 0 for value in amounts.values())
    ):
        return "free"
    return "unknown"


def _chat_compatible(model: dict, model_id: str) -> bool:
    capabilities = model.get("capabilities")
    if isinstance(capabilities, dict) and isinstance(capabilities.get("completion_chat"), bool):
        if not capabilities["completion_chat"]:
            return False

    endpoints = model.get("endpoints", model.get("supported_endpoints"))
    endpoint_chat = None
    if isinstance(endpoints, list) and all(isinstance(item, str) for item in endpoints):
        endpoint_chat = any(item.rstrip("/").endswith("/chat/completions") for item in endpoints)
        if not endpoint_chat:
            return False

    methods = model.get("supportedGenerationMethods", model.get("supported_generation_methods"))
    method_chat = None
    if isinstance(methods, list) and all(isinstance(item, str) for item in methods):
        method_chat = any(item in {"generateContent", "streamGenerateContent"} for item in methods)
        if not method_chat:
            return False

    architecture = model.get("architecture")
    outputs = architecture.get("output_modalities") if isinstance(architecture, dict) else None
    if isinstance(outputs, list):
        return any(isinstance(item, str) and item.casefold() == "text" for item in outputs)
    if endpoint_chat or method_chat or (isinstance(capabilities, dict) and capabilities.get("completion_chat") is True):
        return True
    # Normalized records are persisted; preserve their classification on refresh.
    if isinstance(model.get("chat_compatible"), bool):
        return model["chat_compatible"]
    return not bool(_NON_CHAT_ID.search(model_id))


def classify_model(model: dict, kind: str = "custom") -> dict:
    """Normalize a model and attach pricing/chat labels without trusting labels.

    ``:free`` by itself is deliberately insufficient: both prompt/completion
    prices and every other reported monetary component must be valid zeroes.
    The only name-based exception is OpenRouter's own ``openrouter/free``.
    """
    if not isinstance(model, dict):
        raise ValueError("Modelo inválido retornado pelo endpoint /models")
    model_id = model.get("id") or model.get("name")
    if not isinstance(model_id, str):
        raise ValueError("Modelo inválido retornado pelo endpoint /models")
    model_id = model_id.strip().removeprefix("models/")
    if not model_id or len(model_id) > 256 or any(character.isspace() or ord(character) < 32 for character in model_id):
        raise ValueError("Modelo inválido retornado pelo endpoint /models")

    pricing = _public_pricing(model.get("pricing"))
    price_class = _price_class(model_id, pricing, kind)
    labels = {
        "free": "Gratuito · preços informados em zero",
        "paid": "Pago · há cobrança informada",
        "unknown": "Preço não informado · consulte o provedor",
    }
    if price_class == "free" and kind == "openrouter" and model_id == OPENROUTER_FREE_ROUTER:
        labels["free"] = "Gratuito · roteador OpenRouter"
    name = model.get("name")
    description = model.get("description")
    context = model.get("context_length")
    if isinstance(context, bool) or not isinstance(context, int) or context < 0:
        context = None
    return {
        "id": model_id,
        "name": (name if isinstance(name, str) and name else model_id)[:160],
        "context_length": context,
        "pricing": pricing,
        "description": description[:300] if isinstance(description, str) else "",
        "price_class": price_class,
        "price_label": labels[price_class],
        "chat_compatible": _chat_compatible(model, model_id),
    }


def normalize_models(response: dict, kind: str = "custom") -> list[dict]:
    """Parse common /models envelopes, then order free models before others.

    The 10,000 item bound protects against accidental oversized responses while
    preserving the full provider catalog before any UI pagination or filtering.
    No unavailable model, including the free router, is synthesized here.
    """
    if not isinstance(response, dict):
        raise ValueError("O endpoint /models não devolveu uma lista compatível")
    raw = response.get("data", response.get("models", []))
    if isinstance(raw, dict):
        raw = raw.get("models", [])
    if not isinstance(raw, list):
        raise ValueError("O endpoint /models não devolveu uma lista compatível")
    mapped = {}
    for value in raw[:10000]:
        if isinstance(value, str):
            value = {"id": value}
        if not isinstance(value, dict):
            continue
        try:
            normalized = classify_model(value, kind)
        except ValueError:
            continue
        mapped[normalized["id"]] = normalized
    if not mapped:
        raise ValueError("Nenhum modelo válido retornado pelo endpoint /models")
    return sorted(mapped.values(), key=lambda model: (
        model["price_class"] != "free",
        not (kind == "openrouter" and model["id"] == OPENROUTER_FREE_ROUTER),
        model["name"].casefold(), model["id"].casefold(),
    ))
