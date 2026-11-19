"""AutoClips connections v0.5.0: encrypted model credentials, OAuth linking and YouTube upload."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import tempfile
import threading
import time
from typing import Literal
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
from pydantic import BaseModel, Field

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
SOCIAL = {
    "youtube": {
        "label": "YouTube", "env_id": "AUTOCLIPS_YOUTUBE_CLIENT_ID", "env_secret": "AUTOCLIPS_YOUTUBE_CLIENT_SECRET",
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth", "token": "https://oauth2.googleapis.com/token",
        "scopes": "https://www.googleapis.com/auth/youtube.readonly",
        "docs": "https://console.cloud.google.com/apis/credentials",
    },
    "tiktok": {
        "label": "TikTok", "env_id": "AUTOCLIPS_TIKTOK_CLIENT_KEY", "env_secret": "AUTOCLIPS_TIKTOK_CLIENT_SECRET",
        "authorize": "https://www.tiktok.com/v2/auth/authorize/", "token": "https://open.tiktokapis.com/v2/oauth/token/",
        "scopes": "user.info.basic", "docs": "https://developers.tiktok.com/",
    },
    "instagram": {
        "label": "Instagram", "env_id": "AUTOCLIPS_INSTAGRAM_CLIENT_ID", "env_secret": "AUTOCLIPS_INSTAGRAM_CLIENT_SECRET",
        "authorize": "https://www.instagram.com/oauth/authorize", "token": "https://api.instagram.com/oauth/access_token",
        "scopes": "instagram_business_basic", "docs": "https://developers.facebook.com/apps/",
    },
    "twitch": {
        "label": "Twitch", "env_id": "AUTOCLIPS_TWITCH_CLIENT_ID", "env_secret": "AUTOCLIPS_TWITCH_CLIENT_SECRET",
        "authorize": "https://id.twitch.tv/oauth2/authorize", "token": "https://id.twitch.tv/oauth2/token",
        "scopes": "user:read:email", "docs": "https://dev.twitch.tv/console/apps",
    },
    "kick": {
        "label": "Kick", "env_id": "AUTOCLIPS_KICK_CLIENT_ID", "env_secret": "AUTOCLIPS_KICK_CLIENT_SECRET",
        "authorize": "https://id.kick.com/oauth/authorize", "token": "https://id.kick.com/oauth/token",
        "scopes": "user:read", "docs": "https://dev.kick.com/",
    },
}

YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
STAGES = {"moments": "Selecao de momentos", "titles": "Titulos e descricoes", "review": "Revisao de conteudo"}


class NewProvider(BaseModel):
    kind: Literal["openrouter", "custom"]
    name: str = Field(min_length=2, max_length=80)
    api_key: str = Field(min_length=5, max_length=4096)
    endpoint: str | None = Field(default=None, max_length=1024)


class ModelSelection(BaseModel):
    stage: Literal["moments", "titles", "review"]
    provider_id: str
    model_id: str


def utc():
    return datetime.now(timezone.utc).isoformat()


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "connections.enc"
        self.lock = threading.RLock()
        key = os.environ.get("AUTOCLIPS_ENCRYPTION_KEY")
        if key:
            keybytes = key.encode("ascii")
        else:
            keypath = self.root / ".connections.key"
            try:
                fd = os.open(keypath, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                keybytes = keypath.read_bytes().strip()
            else:
                keybytes = Fernet.generate_key()
                with os.fdopen(fd, "wb") as f:
                    f.write(keybytes)
        self.cipher = Fernet(keybytes)

    def read(self) -> dict:
        with self.lock:
            if not self.path.exists():
                return {"providers": {}, "selected": {}, "social": {}, "pending": {}}
            try:
                return json.loads(self.cipher.decrypt(self.path.read_bytes()))
            except (InvalidToken, ValueError) as exc:
                raise RuntimeError("Cofre de conexoes ilegivel; restaure a chave original") from exc

    def save(self, state: dict):
        with self.lock:
            encoded = self.cipher.encrypt(json.dumps(state, ensure_ascii=False).encode("utf-8"))
            fd, temp = tempfile.mkstemp(prefix=".connections-", dir=self.root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as fh:
                    fh.write(encoded)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temp, self.path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)


def validate_endpoint(base: str) -> str:
    url = urlsplit((base or "").strip())
    if url.scheme != "https" or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("Use somente uma URL-base HTTPS sem usuario, parametros ou fragmentos")
    hostname = url.hostname or ""
    if hostname.lower() in {"localhost", "metadata.google.internal"} or hostname.lower().endswith((".local", ".internal", ".localhost")):
        raise ValueError("Endpoints locais ou internos nao sao permitidos")
    try:
        ip = ipaddress.ip_address(hostname)
        addresses = [ip]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(a[4][0]) for a in socket.getaddrinfo(hostname, url.port or 443, type=socket.SOCK_STREAM)]
        except (socket.gaierror, ValueError) as exc:
            raise ValueError("Nao foi possivel resolver o endereco do provedor") from exc
    if not addresses:
        raise ValueError("Nenhum endereco resolvido")
    if any(not ip.is_global for ip in addresses) and os.environ.get("AUTOCLIPS_ALLOW_PRIVATE_PROVIDERS") != "1":
        raise ValueError("Endereco nao publico bloqueado por seguranca")
    if url.port and url.port not in {443, 8443}:
        raise ValueError("Somente portas HTTPS 443 e 8443 sao aceitas")
    path = url.path.rstrip("/")
    if path.endswith("/models"):
        path = path[:-len("/models")]
    if not path or path == "/":
        path = "/v1"
    if ".." in path or "//" in path:
        raise ValueError("Caminho invalido")
    return urlunsplit(("https", url.netloc.lower(), path, "", ""))


def remote_get(url: str, key: str) -> dict:
    with httpx.Client(timeout=18, follow_redirects=False, trust_env=False) as client:
        try:
            response = client.get(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
            if response.status_code in {301, 302, 303, 307, 308}:
                raise ValueError("O provedor redirecionou a API; confira a URL-base")
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {401, 403}:
                raise ValueError("Chave rejeitada pelo provedor (401/403)") from exc
            raise ValueError("Provedor retornou HTTP " + str(exc.response.status_code)) from exc
        except (httpx.RequestError, json.JSONDecodeError) as exc:
            raise ValueError("Nao foi possivel consultar o provedor") from exc
    if not isinstance(data, dict):
        raise ValueError("Resposta inesperada do provedor")
    return data


def model_items(response: dict) -> list[dict]:
    raw = response.get("data", response.get("models", []))
    if isinstance(raw, dict):
        raw = raw.get("models", [])
    if not isinstance(raw, list):
        raise ValueError("O endpoint /models nao devolveu uma lista compativel")
    mapped = {}
    for value in raw[:10000]:
        if isinstance(value, str):
            value = {"id": value}
        if not isinstance(value, dict):
            continue
        model_id = str(value.get("id") or value.get("name") or "")
        if not model_id or len(model_id) > 256:
            continue
        if model_id.startswith("models/"):
            model_id = model_id[7:]
        mapped[model_id] = {
            "id": model_id, "name": str(value.get("name") or model_id)[:160],
            "context_length": value.get("context_length"),
            "pricing": value.get("pricing") if isinstance(value.get("pricing"), dict) else None,
            "description": str(value.get("description") or "")[:300],
        }
    if not mapped:
        raise ValueError("Nenhum modelo valido retornado pelo endpoint /models")
    return sorted(mapped.values(), key=lambda x: x["name"].casefold())


def discover_models(kind: str, endpoint: str, key: str) -> list[dict]:
    if kind == "openrouter":
        remote_get(OPENROUTER_BASE + "/key", key)
    return model_items(remote_get(endpoint + "/models", key))


def public_provider(entry: dict) -> dict:
    return {k: entry.get(k) for k in ("id", "kind", "name", "endpoint", "created_at", "updated_at")} | {
        "model_count": len(entry.get("models", [])),
        "key_saved": bool(entry.get("api_key")), "last_four": entry.get("api_key", "")[-4:],
    }


def get_selected(vault: Vault, stage: str = "moments") -> dict | None:
    data = vault.read()
    sel = data.get("selected", {}).get(stage)
    if not sel:
        return None
    provider = data.get("providers", {}).get(sel["provider_id"])
    if not provider or sel["model_id"] not in {m["id"] for m in provider["models"]}:
        return None
    return {"provider_id": sel["provider_id"], "model_id": sel["model_id"],
            "endpoint": provider["endpoint"], "api_key": provider["api_key"]}


def _parse_llm_json(raw: str) -> dict:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def rerank_remote(proposals: list[dict], selected: dict) -> list[dict]:
    shortlist = sorted(proposals, key=lambda item: item.get("score", 0), reverse=True)[:12]
    for c in shortlist:
        excerpt = (c.get("excerpt") or "").strip()
        if not excerpt:
            continue
        prompt = ("Avalie de 0 a 10 o potencial deste trecho para video curto, "
                  "preservando o significado original. Responda SOMENTE JSON "
                  '{"score":7,"reason":"justificativa curta"}.\n\n' + excerpt[:700])
        with httpx.Client(timeout=60, follow_redirects=False, trust_env=False) as client:
            resp = client.post(selected["endpoint"] + "/chat/completions",
                               headers={"Authorization": "Bearer " + selected["api_key"], "Content-Type": "application/json"},
                               json={"model": selected["model_id"], "temperature": 0,
                                     "messages": [{"role": "system", "content": "Voce e um editor de video cuidadoso."},
                                                  {"role": "user", "content": prompt}]})
            resp.raise_for_status()
            result = resp.json()
        answer = result["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            raise ValueError("O modelo nao devolveu texto")
        parsed = _parse_llm_json(answer)
        score = max(0, min(10, float(parsed["score"])))
        c["score"] = round(c["score"] + score * 5, 2)
        c["reason"] = (c.get("reason", "") + "; IA: " + str(parsed.get("reason", ""))[:160])[:400]
        c["method"] = "remote-llm+heuristic"
    return proposals


def generate_clip_metadata(vault: Vault, excerpt: str, fallback_title: str = "") -> dict:
    clean_excerpt = (excerpt or "").strip()
    selected = get_selected(vault, "titles")
    if selected and clean_excerpt:
        prompt = (
            "Crie titulo curto (max 75 caracteres), descricao envolvente (max 220 caracteres) e 4 hashtags "
            "para YouTube Shorts/Reels em portugues com base fiel neste trecho transcrito. "
            'Responda SOMENTE JSON: {"title":"...","description":"...","tags":["shorts","clips"]}.\n\n'
            f"Trecho: {clean_excerpt[:900]}"
        )
        with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
            resp = client.post(
                selected["endpoint"] + "/chat/completions",
                headers={"Authorization": "Bearer " + selected["api_key"], "Content-Type": "application/json"},
                json={
                    "model": selected["model_id"],
                    "temperature": 0.4,
                    "messages": [
                        {"role": "system", "content": "Voce e especialista em retencao e titulos para YouTube Shorts."},
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        parsed = _parse_llm_json(data["choices"][0]["message"]["content"])
        title = str(parsed.get("title") or fallback_title or "Momento destaque")[:95].strip()
        desc = str(parsed.get("description") or clean_excerpt[:200])[:450].strip()
        raw_tags = parsed.get("tags") or ["shorts", "cortes"]
        tags = [re.sub(r"[^A-Za-z0-9À-ÿ_]", "", str(t).lstrip("#")) for t in raw_tags if str(t).strip()][:8]
        return {"title": title, "description": desc, "tags": tags, "source": "ai:" + selected["model_id"]}

    words = [w for w in clean_excerpt.split() if len(w) > 2]
    snippet = " ".join(words[:11]).rstrip(".,;:")
    title = (fallback_title.strip() if fallback_title and fallback_title.strip() not in {"Momento especial", ""} else
             (snippet[:72] + ("..." if len(snippet) > 72 else "") if snippet else "Corte em destaque #Shorts"))
    if "#shorts" not in title.lower() and len(title) <= 85:
        title = f"{title} #Shorts"
    desc = (clean_excerpt[:240] + "\n\n#Shorts #Cortes #AutoClips") if clean_excerpt else "Corte editado no AutoClips AI Studio.\n\n#Shorts #Cortes"
    return {"title": title[:95], "description": desc[:450], "tags": ["Shorts", "Cortes", "AutoClips"], "source": "heuristic"}


def _public_url(request: Request | None = None) -> str:
    configured = os.getenv("AUTOCLIPS_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("Configure AUTOCLIPS_PUBLIC_URL com HTTPS publico ou loopback local")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username:
            raise ValueError("AUTOCLIPS_PUBLIC_URL deve ser apenas a origem")
        return configured

    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip().lower()
        host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0].strip()
        if host:
            hostname = host.split(":")[0].lower()
            if proto == "https" or hostname in {"localhost", "127.0.0.1"}:
                return f"{proto}://{host}"
    return "http://127.0.0.1:8765"


def _social_settings(provider: str) -> tuple[dict, str, str]:
    if provider not in SOCIAL:
        raise HTTPException(404, "Rede nao reconhecida")
    conf = SOCIAL[provider]
    client_id = os.getenv(conf["env_id"], "")
    secret = os.getenv(conf["env_secret"], "")
    if not client_id or not secret:
        raise HTTPException(409, f"O aplicativo AutoClips ainda nao esta registrado para {conf['label']}.")
    return conf, client_id, secret


def _exchange_code(provider: str, code: str, verifier: str, redirect_uri: str) -> dict:
    conf, client_id, secret = _social_settings(provider)
    data = {"client_id": client_id, "client_secret": secret, "code": code,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri}
    if provider == "tiktok":
        data = {"client_key": client_id, "client_secret": secret, "code": code,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri}
    if provider == "kick":
        data["code_verifier"] = verifier
    if provider == "instagram":
        data.pop("grant_type", None)
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        res = client.post(conf["token"], data=data, headers={"Accept": "application/json"})
        if res.status_code >= 400:
            raise ValueError(f"A plataforma rejeitou a autorizacao (HTTP {res.status_code})")
        payload = res.json()
    if provider == "tiktok":
        payload = payload.get("data", payload)
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise ValueError("A plataforma nao retornou um token de acesso")
    return payload


def _youtube_channel(access_token: str) -> dict:
    try:
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
            response = client.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": "Bearer " + access_token, "Accept": "application/json"},
            )
            response.raise_for_status()
            channels = response.json().get("items", [])
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        raise ValueError("Nao foi possivel consultar o canal autorizado no YouTube") from exc
    if not channels or not isinstance(channels, list):
        raise ValueError("A Conta Google autorizada nao retornou um canal do YouTube")
    item = channels[0]
    channel_id = str(item.get("id", ""))
    if not channel_id.startswith("UC") or len(channel_id) < 10:
        raise ValueError("O YouTube nao retornou um identificador valido de canal")
    return {"id": channel_id, "name": str(item.get("snippet", {}).get("title") or "Canal do YouTube")[:160]}


def _refresh_youtube_token(refresh_token: str) -> dict:
    _, client_id, client_secret = _social_settings("youtube")
    with httpx.Client(timeout=20, trust_env=False, follow_redirects=False) as client:
        try:
            response = client.post("https://oauth2.googleapis.com/token", data={
                "client_id": client_id, "client_secret": client_secret,
                "refresh_token": refresh_token, "grant_type": "refresh_token",
            }, headers={"Accept": "application/json"})
            response.raise_for_status()
            token_data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError("O Google nao conseguiu renovar a autorizacao. Reconecte o canal.") from exc
    if not isinstance(token_data, dict) or not token_data.get("access_token"):
        raise ValueError("A renovacao do Google nao retornou um token de acesso")
    return token_data


def get_youtube_access_token(vault: Vault, require_upload: bool = False) -> tuple[str, dict]:
    with vault.lock:
        state = vault.read()
        saved = state.get("social", {}).get("youtube")
        if not saved or not saved.get("access_token") or not saved.get("account", {}).get("id"):
            raise ValueError("Conecte primeiro seu canal do YouTube na aba Conexoes.")
        scopes = set(str(saved.get("scope", "")).split())
        if require_upload and YOUTUBE_UPLOAD_SCOPE not in scopes:
            raise ValueError("Ative a permissao de publicacao (youtube.upload) na aba Conexoes antes de enviar.")
        if saved.get("expires_at", 0) < time.time() + 45:
            if not saved.get("refresh_token"):
                raise ValueError("A conexao expirou. Conecte novamente o canal.")
            new_token = _refresh_youtube_token(saved["refresh_token"])
            saved["access_token"] = new_token["access_token"]
            saved["expires_at"] = time.time() + float(new_token.get("expires_in", 3600))
            if new_token.get("refresh_token"):
                saved["refresh_token"] = new_token["refresh_token"]
            vault.save(state)
        return saved["access_token"], dict(saved["account"])


def upload_youtube_video(
    vault: Vault,
    video_path: Path,
    title: str,
    description: str = "",
    privacy_status: Literal["private", "unlisted", "public"] = "private",
    tags: list[str] | None = None,
) -> dict:
    if privacy_status not in {"private", "unlisted", "public"}:
        raise ValueError("Status de privacidade invalido")
    if not video_path.exists() or video_path.stat().st_size == 0:
        raise ValueError("Arquivo final do clipe nao encontrado para envio")
    token, account = get_youtube_access_token(vault, require_upload=True)
    file_size = video_path.stat().st_size
    clean_title = (title or "AutoClips Short").strip()[:100]
    clean_desc = (description or "Publicado com AutoClips AI Studio").strip()[:4900]
    clean_tags = [str(t).strip()[:30] for t in (tags or ["Shorts", "AutoClips"]) if str(t).strip()][:15]

    metadata = {
        "snippet": {
            "title": clean_title,
            "description": clean_desc,
            "tags": clean_tags,
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    init_headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }

    with httpx.Client(timeout=120, trust_env=False, follow_redirects=False) as client:
        init_resp = client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=init_headers,
            json=metadata,
        )
        if init_resp.status_code >= 400:
            raise ValueError(f"YouTube recusou inicio do upload (HTTP {init_resp.status_code}): {init_resp.text[:200]}")
        upload_url = init_resp.headers.get("Location")
        if not upload_url:
            raise ValueError("YouTube nao retornou URL de upload resumivel")

        chunk_size = 4 * 1024 * 1024
        offset = 0
        retries_without_progress = 0
        result_payload = None
        with video_path.open("rb") as fh:
            while offset < file_size:
                # The server can acknowledge only PART of a chunk with HTTP 308.
                # Always seek to its actual next byte before reading again.
                fh.seek(offset)
                chunk = fh.read(min(chunk_size, file_size - offset))
                if not chunk:
                    break
                end_byte = offset + len(chunk) - 1
                put_resp = client.put(
                    upload_url,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end_byte}/{file_size}",
                        "Content-Type": "video/mp4",
                    },
                    content=chunk,
                )
                if put_resp.status_code in {200, 201}:
                    result_payload = put_resp.json()
                    break
                if put_resp.status_code == 308:
                    range_hdr = put_resp.headers.get("Range", "")
                    if range_hdr:
                        match = re.fullmatch(r"bytes=0-(\d+)", range_hdr)
                        if not match:
                            raise ValueError("YouTube retornou Range invalido na retomada")
                        acknowledged = int(match.group(1)) + 1
                        if not 0 <= acknowledged <= end_byte + 1:
                            raise ValueError("YouTube retornou progresso invalido no upload")
                        next_offset = acknowledged
                    else:
                        # An absent Range on a 308 means the server has not
                        # confirmed any bytes yet, not that the chunk succeeded.
                        next_offset = 0
                    retries_without_progress = (retries_without_progress + 1 if next_offset <= offset else 0)
                    if retries_without_progress >= 4:
                        raise ValueError("YouTube nao confirmou progresso apos varias tentativas")
                    offset = next_offset
                    continue
                raise ValueError(f"Falha no envio do bloco ao YouTube (HTTP {put_resp.status_code}): {put_resp.text[:200]}")

    if not isinstance(result_payload, dict) or not result_payload.get("id"):
        raise ValueError("Upload concluido sem ID de video retornado pelo YouTube")
    yt_id = str(result_payload["id"])
    return {
        "youtube_video_id": yt_id,
        "url": f"https://www.youtube.com/watch?v={yt_id}",
        "studio_url": f"https://studio.youtube.com/video/{yt_id}/edit",
        "privacy_status": privacy_status,
        "channel": account,
    }


def _identity(provider: str, token: str, client_id: str) -> dict:
    if provider == "youtube":
        return _youtube_channel(token)
    try:
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
            headers = {"Authorization": "Bearer " + token}
            if provider == "twitch":
                headers["Client-Id"] = client_id
                r = client.get("https://api.twitch.tv/helix/users", headers=headers)
                r.raise_for_status()
                items = r.json().get("data", [])
                if items:
                    return {"id": items[0].get("id", ""), "name": items[0].get("display_name", "Twitch")}
            elif provider == "instagram":
                r = client.get("https://graph.instagram.com/me", params={"fields": "id,username,account_type"}, headers=headers)
                r.raise_for_status()
                return {"id": r.json().get("id", ""), "name": r.json().get("username", "Instagram")}
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return {"id": "", "name": SOCIAL[provider]["label"] + " conectada"}


def build_router(vault: Vault) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ai/providers")
    def ai_providers():
        data = vault.read()
        return {"providers": [public_provider(x) for x in data["providers"].values()],
                "selected": data.get("selected", {}), "stages": STAGES}

    @router.post("/api/ai/providers", status_code=201)
    def add_provider(form: NewProvider):
        kind = form.kind
        try:
            endpoint = OPENROUTER_BASE if kind == "openrouter" else validate_endpoint(form.endpoint or "")
            models = discover_models(kind, endpoint, form.api_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        with vault.lock:
            state = vault.read()
            item = {"id": uuid4().hex, "kind": kind, "name": form.name.strip(), "endpoint": endpoint,
                    "api_key": form.api_key, "models": models, "created_at": utc(), "updated_at": utc()}
            state["providers"][item["id"]] = item
            vault.save(state)
        return {"provider": public_provider(item), "models": models}

    @router.get("/api/ai/providers/{provider_id}/models")
    def get_models(provider_id: str, q: str = Query("", max_length=100), offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        item = vault.read()["providers"].get(provider_id)
        if not item:
            raise HTTPException(404, "Provedor nao cadastrado")
        models = [m for m in item["models"] if q.casefold() in (m["id"] + " " + m["name"]).casefold()]
        return {"total": len(models), "models": models[offset:offset + limit]}

    @router.post("/api/ai/providers/{provider_id}/refresh")
    def refresh_models(provider_id: str):
        with vault.lock:
            state = vault.read()
            item = state["providers"].get(provider_id)
            if not item:
                raise HTTPException(404, "Provedor nao cadastrado")
            try:
                items = discover_models(item["kind"], item["endpoint"], item["api_key"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            item["models"] = items
            item["updated_at"] = utc()
            for stage, sel in list(state.get("selected", {}).items()):
                if sel["provider_id"] == provider_id and sel["model_id"] not in {m["id"] for m in items}:
                    state["selected"].pop(stage)
            vault.save(state)
        return {"count": len(items), "provider": public_provider(item)}

    @router.delete("/api/ai/providers/{provider_id}")
    def delete_provider(provider_id: str):
        with vault.lock:
            state = vault.read()
            if state["providers"].pop(provider_id, None) is None:
                raise HTTPException(404, "Provedor nao cadastrado")
            for stage, sel in list(state.get("selected", {}).items()):
                if sel["provider_id"] == provider_id:
                    state["selected"].pop(stage)
            vault.save(state)
        return {"ok": True}

    @router.post("/api/ai/selection")
    def choose_model(form: ModelSelection):
        with vault.lock:
            state = vault.read()
            item = state["providers"].get(form.provider_id)
            if not item:
                raise HTTPException(404, "Provedor nao encontrado")
            if form.model_id not in {m["id"] for m in item["models"]}:
                raise HTTPException(422, "Selecione um modelo listado pelo provedor")
            state["selected"][form.stage] = {"provider_id": form.provider_id, "model_id": form.model_id}
            vault.save(state)
        return {"stage": form.stage, "provider": item["name"], "model_id": form.model_id}

    @router.get("/api/social/accounts")
    def social_accounts():
        current = vault.read().get("social", {})
        result = []
        for ident, conf in SOCIAL.items():
            saved = current.get(ident, {})
            scopes = set(str(saved.get("scope", "")).split())
            needs_reconnect = bool(saved) and not bool(saved.get("refresh_token")) and (
                saved.get("expires_at", 0) < time.time()
            )
            result.append({
                "id": ident, "name": conf["label"],
                "configured": bool(os.getenv(conf["env_id"]) and os.getenv(conf["env_secret"])),
                "connected": bool(saved.get("access_token")) and not needs_reconnect and (
                    ident != "youtube" or bool((saved.get("account") or {}).get("id"))
                ),
                "account": saved.get("account"), "scopes": saved.get("scope", ""),
                "can_upload": ident == "youtube" and YOUTUBE_UPLOAD_SCOPE in scopes,
                "needs_reconnect": needs_reconnect,
                "docs": conf["docs"],
            })
        return {"networks": result}

    @router.get("/api/social/youtube/channel")
    def youtube_channel():
        try:
            token, saved_account = get_youtube_access_token(vault, require_upload=False)
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        try:
            identity = _youtube_channel(token)
        except ValueError as exc:
            raise HTTPException(502, str(exc)) from exc
        if identity["id"] != saved_account["id"]:
            raise HTTPException(409, "A conta autorizada mudou. Reconecte seu canal.")
        saved = vault.read().get("social", {}).get("youtube", {})
        return {"channel": identity, "can_upload": YOUTUBE_UPLOAD_SCOPE in str(saved.get("scope", "")).split()}

    @router.get("/auth/{provider}/start")
    def auth_start(provider: str, request: Request, permission: Literal["view", "publish"] = "view"):
        conf, client_id, _ = _social_settings(provider)
        try:
            redirect_uri = _public_url(request) + "/auth/" + provider + "/callback"
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        csrf = secrets.token_urlsafe(32)
        browser_nonce = secrets.token_urlsafe(32)
        params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
                  "state": csrf}
        extra_scopes = os.getenv("AUTOCLIPS_" + provider.upper() + "_SCOPES", conf["scopes"])
        if provider == "youtube":
            extra_scopes = YOUTUBE_READ_SCOPE + (" " + YOUTUBE_UPLOAD_SCOPE if permission == "publish" else "")
            params.update(access_type="offline", include_granted_scopes="true", prompt="consent")
        params["scope"] = extra_scopes if provider not in {"tiktok", "instagram"} else ",".join(extra_scopes.replace(",", " ").split())
        verifier = ""
        if provider == "kick":
            verifier = secrets.token_urlsafe(64)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            params.update(code_challenge=challenge, code_challenge_method="S256")
        with vault.lock:
            state = vault.read()
            now = time.time()
            state["pending"] = {k: v for k, v in state.get("pending", {}).items() if v.get("expires", 0) > now}
            state["pending"][csrf] = {"provider": provider, "redirect": redirect_uri, "expires": now + 600, "verifier": verifier,
                                      "nonce_hash": hashlib.sha256(browser_nonce.encode()).hexdigest(), "requested_scopes": extra_scopes}
            vault.save(state)
        response = RedirectResponse(conf["authorize"] + "?" + urlencode(params), status_code=302)
        response.set_cookie("autoclips_oauth_" + provider, browser_nonce, httponly=True,
                            secure=redirect_uri.startswith("https://"), samesite="lax", path="/auth/" + provider, max_age=600)
        return response

    @router.get("/auth/{provider}/callback")
    def auth_callback(provider: str, request: Request, code: str = "", state: str = "", error: str = ""):
        if provider not in SOCIAL:
            raise HTTPException(404, "Rede nao reconhecida")
        if error:
            return HTMLResponse("<h1>Autorizacao cancelada</h1><p>Volte ao AutoClips para tentar novamente.</p>", status_code=400)
        with vault.lock:
            data = vault.read()
            pending = data.get("pending", {}).pop(state, None)
            vault.save(data)
        nonce = request.cookies.get("autoclips_oauth_" + provider, "")
        valid_nonce = (pending is not None and nonce and
                       secrets.compare_digest(pending.get("nonce_hash", ""), hashlib.sha256(nonce.encode()).hexdigest()))
        if not pending or not valid_nonce or pending.get("provider") != provider or pending.get("expires", 0) < time.time() or not code:
            raise HTTPException(400, "Autorizacao expirada, invalida ou ja utilizada")
        try:
            payload = _exchange_code(provider, code, pending.get("verifier", ""), pending["redirect"])
            client_id = os.getenv(SOCIAL[provider]["env_id"], "")
            identity = _identity(provider, payload["access_token"], client_id)
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(502, "Nao foi possivel finalizar a autorizacao oficial: " + str(exc)[:150]) from exc
        if provider == "youtube" and not payload.get("scope"):
            raise HTTPException(502, "O Google nao confirmou as permissoes concedidas. Reconecte o canal.")
        actual_scopes = set(str(payload.get("scope") or pending.get("requested_scopes", "")).split())
        if provider == "youtube":
            required = set(str(pending.get("requested_scopes", "")).split())
            if not required.issubset(actual_scopes):
                raise HTTPException(403, "O Google nao concedeu as permissoes solicitadas. Tente novamente.")
        with vault.lock:
            data = vault.read()
            previous = data.get("social", {}).get(provider, {})
            previous_refresh = previous.get("refresh_token", "") if previous.get("account", {}).get("id") == identity.get("id") else ""
            data["social"][provider] = {
                "access_token": payload["access_token"], "refresh_token": payload.get("refresh_token") or previous_refresh,
                "expires_at": time.time() + float(payload.get("expires_in", 3600)),
                "scope": " ".join(sorted(actual_scopes)), "account": identity, "connected_at": utc(),
            }
            vault.save(data)
        response = RedirectResponse("/?connected=" + provider + "#connections", status_code=303)
        response.delete_cookie("autoclips_oauth_" + provider, path="/auth/" + provider)
        return response

    @router.post("/api/social/{provider}/disconnect")
    def social_disconnect(provider: str):
        if provider not in SOCIAL:
            raise HTTPException(404, "Rede nao reconhecida")
        with vault.lock:
            data = vault.read()
            data["social"].pop(provider, None)
            vault.save(data)
        return {"ok": True, "notice": "Desconectado localmente."}

    return router
