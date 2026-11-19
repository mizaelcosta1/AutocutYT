"""Bounded, zero-price OpenRouter requests and safe provider errors.

Model rotation is delegated to OpenRouter's official free router. Rate limits
apply to the whole credential, not to individual model names. No paid fallback
or automatic retry consumes a second request after a provider rejects one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import math
import threading
import time

import httpx

from ai_catalog import OPENROUTER_BASE


class ModelRequestError(ValueError):
    def __init__(self, message: str, status_code: int = 502, retry_after: int = 0):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


_lock = threading.RLock()
_cooldowns: dict[str, float] = {}


def _credential_id(selected: dict) -> str:
    return hashlib.sha256((selected["endpoint"] + "\0" + selected["api_key"]).encode()).hexdigest()


def cooldown_remaining(selected: dict) -> int:
    with _lock:
        return max(0, math.ceil(_cooldowns.get(_credential_id(selected), 0) - time.monotonic()))


def _retry_after(headers, default: int = 60) -> int:
    value = str(headers.get("retry-after", "")).strip()
    if value.isdigit():
        return max(1, int(value))
    try:
        reset = parsedate_to_datetime(value)
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        return max(1, math.ceil((reset - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return default


def _provider_error(status: int, headers, selected: dict) -> ModelRequestError:
    retry = 0
    if status == 429 or (status == 503 and headers.get("retry-after")):
        retry = _retry_after(headers)
        with _lock:
            now = time.monotonic()
            for key, expiry in list(_cooldowns.items()):
                if expiry <= now:
                    _cooldowns.pop(key, None)
            _cooldowns[_credential_id(selected)] = now + retry
    if status in {401, 403}:
        message = "A chave ou a permissão foi rejeitada pelo provedor. Confira a conexão."
    elif status == 402:
        message = "O provedor bloqueou a chamada por saldo ou limite da conta. Confira os limites da chave."
    elif status == 429:
        message = f"Limite do provedor atingido. Aguarde {retry} s antes de tentar novamente; trocar de modelo não reinicia a cota da conta."
    elif status == 404:
        message = "O modelo está indisponível. Atualize o catálogo ou escolha outro modelo gratuito."
    elif status in {400, 413, 422}:
        message = "O modelo rejeitou os parâmetros ou o tamanho do texto. Escolha outro modelo compatível."
    else:
        message = "O provedor está temporariamente indisponível. Tente novamente mais tarde."
    if selected.get("free_only"):
        message += " Nenhum modelo pago foi acionado."
    return ModelRequestError(message, status_code=status if status in {401, 402, 403, 404, 429, 503} else 502, retry_after=retry)


def chat_completion(selected: dict, messages: list[dict], *, temperature: float = 0,
                    max_tokens: int = 384, timeout: float = 45) -> dict:
    """Return verified text and the actual model, never the credential or raw error."""
    waiting = cooldown_remaining(selected)
    if waiting:
        raise ModelRequestError(f"Conexão em pausa pelo limite do provedor. Aguarde {waiting} s.", 429, waiting)
    body = {"model": selected["model_id"], "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if selected.get("free_only"):
        if selected.get("kind") != "openrouter" or selected["endpoint"] != OPENROUTER_BASE:
            raise ModelRequestError("O modo gratuito protegido exige a conexão oficial do OpenRouter.", 422)
        # A price cap is a hard filter, unlike ordering endpoints by cheapness.
        # No models array, plugins or paid search tools are added here.
        body["provider"] = {"max_price": {"prompt": 0, "completion": 0, "request": 0, "image": 0}}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
            response = client.post(selected["endpoint"] + "/chat/completions",
                                   headers={"Authorization": "Bearer " + selected["api_key"],
                                            "Content-Type": "application/json"}, json=body)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise _provider_error(exc.response.status_code, exc.response.headers, selected) from None
    except httpx.RequestError:
        raise ModelRequestError("Não foi possível alcançar o provedor de IA. Confira a conexão e tente novamente.") from None
    except ValueError:
        raise ModelRequestError("O provedor de IA não retornou uma resposta JSON válida.") from None
    if not isinstance(data, dict):
        raise ModelRequestError("Resposta inesperada do provedor de IA.")
    if data.get("error"):
        error = data["error"]
        code = error.get("code", 502) if isinstance(error, dict) else 502
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = 502
        raise _provider_error(code, {}, selected)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ModelRequestError("O modelo não devolveu texto utilizável. Tente outro modelo gratuito.") from None
    if not isinstance(content, str) or not content.strip():
        raise ModelRequestError("O modelo não devolveu texto utilizável. Tente outro modelo gratuito.")
    return {"content": content, "model": str(data.get("model") or selected["model_id"])[:256]}
