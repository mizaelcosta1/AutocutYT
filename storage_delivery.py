"""Private, explicit delivery destinations and a persistent export outbox.

Google Drive uses drive.file and an app-created folder. GitHub accepts private
repositories only and keeps archive Releases in draft. No provider operation
runs at import time, and no secret or local filename is exposed by public APIs.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
GITHUB_API = "https://api.github.com"
GITHUB_VERSION = "2026-03-10"
CHUNK = 8 * 1024 * 1024
DEFAULT_SETTINGS = {"auto_download": False, "auto_upload": False,
                    "provider": "drive", "qualities": ["final"]}
ACTIVE_STATES = {"queued", "running", "retry_wait"}
MAX_ATTEMPTS = 3
logger = logging.getLogger(__name__)


class DeliveryError(ValueError):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float = 0):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = min(3600, max(0, retry_after))


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _sha(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _json(response):
    try:
        value = response.json()
    except (ValueError, UnicodeDecodeError):
        raise DeliveryError("O armazenamento retornou uma resposta inválida.") from None
    return value


def _retry_seconds(response):
    value = response.headers.get("Retry-After")
    try:
        delay = float(value) if value else float(response.headers["X-RateLimit-Reset"]) - time.time()
    except (ValueError, KeyError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time() if value else 60
        except (ValueError, TypeError, OverflowError):
            delay = 60
    return min(3600, max(1, delay))


def _check(response, accepted=(200,)):
    if response.status_code in accepted:
        return response
    code = response.status_code
    if code == 401:
        raise DeliveryError("A autorização expirou ou foi recusada. Reconecte este armazenamento.")
    if code == 429 or code in {500, 502, 503, 504}:
        raise DeliveryError("O armazenamento está temporariamente indisponível. Aguarde a próxima tentativa.",
                            retryable=True, retry_after=_retry_seconds(response))
    if code == 403:
        # Inspect only known codes. Provider bodies can contain private data.
        try:
            payload = response.json()
            errors = payload.get("error", {}).get("errors", [])
            reasons = {x.get("reason") for x in errors if isinstance(x, dict)}
        except (ValueError, AttributeError, TypeError):
            reasons = set()
        if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"} or response.headers.get("X-RateLimit-Remaining") == "0" or response.headers.get("Retry-After"):
            raise DeliveryError("O armazenamento atingiu um limite temporário de chamadas.",
                                retryable=True, retry_after=_retry_seconds(response))
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            raise DeliveryError("Ative a API do Google Drive no projeto Google Cloud e conecte novamente.")
        if "storageQuotaExceeded" in reasons:
            raise DeliveryError("O Google Drive está sem espaço disponível para este arquivo.")
        raise DeliveryError("O armazenamento recusou a permissão. Confira a conta, a pasta ou Contents: write no GitHub.")
    if code == 404:
        raise DeliveryError("O destino não existe mais ou esta conexão não pode acessá-lo.")
    if 300 <= code < 400:
        raise DeliveryError("O armazenamento redirecionou a API. Reconecte o destino para conferir o endereço.")
    raise DeliveryError(f"O armazenamento recusou o envio (HTTP {code}). Confira a conexão e tente novamente.")


def _request(client, method, url, **kwargs):
    try:
        return client.request(method, url, **kwargs)
    except (httpx.HTTPError, OSError):
        raise DeliveryError("A conexão com o armazenamento foi interrompida. O resultado será conferido antes de tentar novamente.",
                            retryable=True, retry_after=10) from None


def _valid_drive_session(url: str):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "www.googleapis.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path == "/upload/drive/v3/files" and not parsed.fragment)
    except ValueError:
        return False


def _safe_component(value, name, *, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", value) or value in {".", ".."}:
        raise DeliveryError(f"Identificador de {name} inválido.")
    return value


class StorageDelivery:
    def __init__(self, root: Path, vault, *, client_factory=None, clock=None):
        self.root = Path(root).resolve()
        self.vault = vault
        self.folder = self.root / "storage_outbox"
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs_path = self.folder / "jobs.enc"
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.worker = None
        self._client_factory = client_factory or (lambda: httpx.Client(
            timeout=httpx.Timeout(30, read=120, write=120), follow_redirects=False, trust_env=False))
        self.clock = clock or time.time
        if self.jobs_path.exists():
            try:
                self.jobs = json.loads(vault.cipher.decrypt(self.jobs_path.read_bytes()))
                if not isinstance(self.jobs, dict):
                    raise ValueError()
            except Exception:
                raise DeliveryError("A fila de envios não pôde ser lida. Restaure o arquivo e a chave do cofre.") from None
        else:
            self.jobs = {}
        changed = False
        for job in self.jobs.values():
            if job.get("status") == "running":
                job.update(status="queued", next_attempt=0, updated_at=_utc())
                changed = True
            elif job.get("status") in {"succeeded", "cancelled"}:
                self._release_pin(job)
        if changed:
            self._save_jobs()

    def _save_jobs(self):
        encoded = self.vault.cipher.encrypt(json.dumps(self.jobs, ensure_ascii=False).encode())
        fd, temporary = tempfile.mkstemp(prefix=".jobs-", dir=self.folder)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.jobs_path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _owner_data(self, owner):
        _safe_component(owner, "conta")
        return self.vault.read().get("storage", {}).get(owner, {})

    def _save_connection(self, owner, provider, connection):
        with self.vault.lock:
            state = self.vault.read()
            entry = state.setdefault("storage", {}).setdefault(owner, {})
            entry[provider] = connection
            self.vault.save(state)

    def public_settings(self, owner):
        data = self._owner_data(owner)
        settings = {**DEFAULT_SETTINGS, **data.get("settings", {})}
        drive, github = data.get("drive", {}), data.get("github", {})
        drive_ok = bool(drive.get("refresh_token") and drive.get("folder_id"))
        github_ok = bool(github.get("token") and github.get("repo_id") and github.get("release_id"))
        folder_url = f"https://drive.google.com/drive/folders/{drive['folder_id']}" if drive_ok else None
        repo_url = f"https://github.com/{github['repository']}" if github_ok else None
        return {**settings,
                "drive": {"configured": bool(os.getenv("AUTOCLIPS_GOOGLE_CLIENT_ID") and os.getenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET")),
                          "connected": drive_ok, "folder_name": "AutoClips" if drive_ok else None,
                          "folder_url": folder_url, "url": folder_url},
                "github": {"configured": True, "connected": github_ok,
                           "repository": github.get("repository") if github_ok else None,
                           "private": True if github_ok else None, "url": repo_url}}

    def configure(self, owner, patch):
        _safe_component(owner, "conta")
        if not isinstance(patch, dict) or set(patch) - set(DEFAULT_SETTINGS):
            raise DeliveryError("Configuração de envio inválida.")
        for key in ("auto_download", "auto_upload"):
            if key in patch and type(patch[key]) is not bool:
                raise DeliveryError("As opções de automação devem estar ativadas ou desativadas.")
        if "provider" in patch and patch["provider"] not in {"drive", "github"}:
            raise DeliveryError("Escolha Google Drive ou GitHub.")
        if "qualities" in patch and (not isinstance(patch["qualities"], list) or not patch["qualities"]
                                     or len(patch["qualities"]) > 2 or any(x not in {"preview", "final"} for x in patch["qualities"])):
            raise DeliveryError("Escolha prévia, final ou os dois arquivos.")
        with self.vault.lock:
            state = self.vault.read()
            entry = state.setdefault("storage", {}).setdefault(owner, {})
            settings = {**DEFAULT_SETTINGS, **entry.get("settings", {}), **patch}
            settings["qualities"] = list(dict.fromkeys(settings["qualities"]))
            if settings["auto_upload"] and not self.public_settings(owner)[settings["provider"]]["connected"]:
                raise DeliveryError("Conecte o armazenamento escolhido antes de ativar o envio automático.")
            entry["settings"] = settings
            self.vault.save(state)
        return self.public_settings(owner)

    def connect_drive(self, owner, token, identity):
        _safe_component(owner, "conta")
        if not isinstance(token, dict) or not isinstance(identity, dict):
            raise DeliveryError("O Google não confirmou a conexão com o Drive.")
        scope = token.get("scope", "")
        scopes = set(scope.split()) if isinstance(scope, str) else set()
        if DRIVE_SCOPE not in scopes:
            raise DeliveryError("Autorize o acesso aos arquivos criados pelo AutoClips no Google Drive.")
        account_id = str(identity.get("sub") or identity.get("id") or "")
        if not account_id or not isinstance(token.get("access_token"), str):
            raise DeliveryError("O Google não confirmou a conexão com o Drive.")
        old = self._owner_data(owner).get("drive", {})
        refresh = token.get("refresh_token") or (old.get("refresh_token") if old.get("account_id") == account_id else None)
        if not refresh:
            raise DeliveryError("Reconecte o Drive e autorize acesso offline para enviar com o navegador fechado.")
        headers = {"Authorization": "Bearer " + token["access_token"]}
        with self._client_factory() as client:
            query = ("trashed = false and mimeType = 'application/vnd.google-apps.folder' "
                     f"and appProperties has {{ key='autoclips_owner' and value='{owner}' }} "
                     "and appProperties has { key='autoclips_role' and value='exports' }")
            response = _request(client, "GET", DRIVE_API + "/files", headers=headers,
                                params={"q": query, "fields": "files(id,name)", "pageSize": 100})
            files = _json(_check(response)).get("files", [])
            if files:
                folder_id = files[0].get("id")
            else:
                response = _request(client, "POST", DRIVE_API + "/files", headers=headers,
                                    params={"fields": "id"}, json={"name": "AutoClips",
                                    "mimeType": "application/vnd.google-apps.folder",
                                    "appProperties": {"autoclips_owner": owner, "autoclips_role": "exports"}})
                folder_id = _json(_check(response, (200, 201))).get("id")
        _safe_component(folder_id, "pasta")
        self._save_connection(owner, "drive", {"account_id": account_id, "folder_id": folder_id,
                             "access_token": token["access_token"], "refresh_token": refresh,
                             "expires_at": self.clock() + float(token.get("expires_in", 3600)), "scope": DRIVE_SCOPE})
        return self.public_settings(owner)

    @staticmethod
    def _github_headers(token):
        return {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": GITHUB_VERSION}

    def _github_repo(self, client, headers, repository):
        info = _json(_check(_request(client, "GET", GITHUB_API + "/repos/" + repository, headers=headers)))
        if info.get("private") is not True:
            raise DeliveryError("Use um repositório privado para guardar seus cortes. Nenhum vídeo será enviado a um repositório público.")
        if info.get("permissions", {}).get("push") is not True:
            raise DeliveryError("O token precisa de acesso de gravação ao repositório escolhido (Contents: write).")
        if not info.get("id"):
            raise DeliveryError("O GitHub não confirmou o repositório.")
        return info

    def _find_release(self, client, headers, repository, tag):
        for page in range(1, 11):
            items = _json(_check(_request(client, "GET", GITHUB_API + f"/repos/{repository}/releases",
                                          headers=headers, params={"per_page": 100, "page": page})))
            if not isinstance(items, list):
                raise DeliveryError("O GitHub retornou uma lista de arquivos inválida.")
            found = next((r for r in items if r.get("tag_name") == tag), None)
            if found:
                if found.get("draft") is not True:
                    raise DeliveryError("A Release de armazenamento foi publicada. Use uma nova conexão com uma Release em rascunho.")
                return found
            if len(items) < 100:
                return None
        raise DeliveryError("Há muitas Releases para localizar o destino automaticamente. Use um repositório dedicado aos cortes.")

    def connect_github(self, owner, token, repo):
        _safe_component(owner, "conta")
        if not isinstance(token, str) or not 8 <= len(token.strip()) <= 4096:
            raise DeliveryError("Informe um token GitHub válido, limitado ao repositório de armazenamento.")
        repository = (repo or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", repository) or repository.rsplit("/", 1)[1] in {".", ".."}:
            raise DeliveryError("Informe o repositório no formato proprietário/nome.")
        token = token.strip()
        headers = self._github_headers(token)
        tag = "autoclips-exports-" + datetime.now(timezone.utc).strftime("%Y-%m")
        with self._client_factory() as client:
            account = _json(_check(_request(client, "GET", GITHUB_API + "/user", headers=headers)))
            if not account.get("id"):
                raise DeliveryError("O GitHub não confirmou a conta autorizada.")
            info = self._github_repo(client, headers, repository)
            release = self._find_release(client, headers, repository, tag)
            if release is None:
                response = _request(client, "POST", GITHUB_API + f"/repos/{repository}/releases", headers=headers,
                                    json={"tag_name": tag, "target_commitish": info.get("default_branch", "main"),
                                          "name": "AutoClips · arquivos", "draft": True, "prerelease": False,
                                          "body": "Arquivos privados gerados pelo AutoClips. Esta Release deve permanecer em rascunho."})
                if response.status_code == 422:
                    release = self._find_release(client, headers, repository, tag)
                    if release is None:
                        raise DeliveryError("O GitHub não criou a Release. Confira Contents: write e se o repositório já possui um commit.")
                else:
                    release = _json(_check(response, (201,)))
        if not release.get("id") or release.get("draft") is not True:
            raise DeliveryError("O GitHub não confirmou a Release em rascunho.")
        self._save_connection(owner, "github", {"token": token, "repository": repository,
                             "account_id": str(account["id"]), "repo_id": str(info["id"]),
                             "release_id": str(release["id"]), "tag": tag})
        return self.public_settings(owner)

    def disconnect(self, owner, provider):
        _safe_component(owner, "conta")
        if provider not in {"drive", "github"}:
            raise DeliveryError("Armazenamento desconhecido.")
        with self.vault.lock:
            state = self.vault.read()
            entry = state.setdefault("storage", {}).setdefault(owner, {})
            entry.pop(provider, None)
            settings = {**DEFAULT_SETTINGS, **entry.get("settings", {})}
            if settings["provider"] == provider:
                settings["auto_upload"] = False
            entry["settings"] = settings
            self.vault.save(state)
        self.wake.set()
        return self.public_settings(owner)

    @staticmethod
    def _destination(provider, connection):
        if provider == "drive":
            return [connection.get("account_id"), connection.get("folder_id")]
        return [connection.get("account_id"), connection.get("repo_id"), connection.get("release_id")]

    def enqueue(self, owner, artifact, provider=None, automatic=False):
        settings = self.public_settings(owner)
        if automatic and (not settings["auto_upload"] or artifact.get("quality") not in settings["qualities"]):
            return None
        provider = provider or settings["provider"]
        if provider not in {"drive", "github"} or not settings[provider]["connected"]:
            raise DeliveryError("Conecte o armazenamento antes de enviar o corte.")
        connection = self._owner_data(owner)[provider]
        if artifact.get("kind") not in {"clip", "timeline"} or artifact.get("quality") not in {"preview", "final"}:
            raise DeliveryError("Tipo de exportação inválido.")
        for key in ("video_id", "render_id"):
            _safe_component(artifact.get(key), key)
        _safe_component(artifact.get("clip_id"), "clipe", nullable=True)
        source = Path(artifact.get("path", ""))
        if not source.is_absolute() or source.is_symlink():
            raise DeliveryError("Arquivo de exportação inválido.")
        try:
            relative = source.relative_to(self.root)
            cursor = self.root
            for component in relative.parts:
                cursor = cursor / component
                if cursor.is_symlink():
                    raise ValueError()
            source = source.resolve(strict=True)
            source.relative_to(self.root)
        except (OSError, ValueError):
            raise DeliveryError("Arquivo de exportação não encontrado.") from None
        if not source.is_file() or source.is_relative_to(self.folder):
            raise DeliveryError("Arquivo de exportação inválido.")
        size = source.stat().st_size
        if size <= 0:
            raise DeliveryError("O arquivo exportado está vazio.")
        if provider == "github" and size >= 2 * 1024 ** 3:
            raise DeliveryError("Cada arquivo em GitHub Releases deve ter menos de 2 GiB. Use Google Drive para este corte.")
        digest = _sha(source)
        target = self._destination(provider, connection)
        key = hashlib.sha256(json.dumps([owner, provider, target, artifact["render_id"], digest], separators=(",", ":")).encode()).hexdigest()
        with self.lock:
            existing = next((j for j in self.jobs.values() if j.get("dedupe") == key and j["status"] != "cancelled"), None)
            if existing:
                return self._public_job(existing)
            previous = next((j for j in reversed(list(self.jobs.values()))
                             if j.get("dedupe") == key and j["status"] == "cancelled"), None)
            if sum(j["status"] not in {"succeeded", "cancelled"} for j in self.jobs.values()) >= 100:
                raise DeliveryError("A fila tem 100 envios pendentes. Conclua ou cancele envios anteriores antes de acrescentar outros.")
            job_id = uuid4().hex
            pin = self.folder / (job_id + ".mp4")
            try:
                os.link(source, pin)
                if pin.is_symlink() or pin.stat().st_size != size or _sha(pin) != digest:
                    raise DeliveryError("A exportação mudou durante a preparação. Tente novamente.")
                name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(artifact.get("filename") or "autoclips.mp4"))[:100].strip("._") or "autoclips.mp4"
                if not name.lower().endswith(".mp4"):
                    name += ".mp4"
                job = {"id": job_id, "owner": owner, "dedupe": key, "provider": provider, "destination": target,
                       "video_id": artifact["video_id"], "clip_id": artifact.get("clip_id"), "kind": artifact["kind"],
                       "quality": artifact["quality"], "render_id": artifact["render_id"], "filename": name,
                       "asset_name": name[:-4] + "-" + digest + ".mp4", "path": str(pin), "sha256": digest,
                       "total_bytes": size, "bytes_sent": 0, "status": "queued", "progress": 0,
                       "attempts": 0, "next_attempt": 0, "created_at": _utc(), "updated_at": _utc(),
                       # Cancelling never deletes remote data. A later explicit
                       # enqueue reconciles the same reserved remote ID first.
                       "url": None, "error": None, "remote": dict(previous["remote"]) if previous else {}}
                self.jobs[job_id] = job
                try:
                    self._save_jobs()
                except Exception:
                    self.jobs.pop(job_id, None)
                    raise
            except Exception:
                pin.unlink(missing_ok=True)
                raise DeliveryError("Não foi possível reservar o arquivo para envio. Confira o espaço e tente novamente.") from None
        self.wake.set()
        return self._public_job(job)

    @staticmethod
    def _public_job(job):
        names = ("id", "provider", "video_id", "clip_id", "kind", "quality", "render_id", "filename", "status",
                 "progress", "bytes_sent", "total_bytes", "url", "error", "attempts", "created_at", "updated_at")
        return {key: job.get(key) for key in names}

    def list_jobs(self, owner, video_id=None):
        _safe_component(owner, "conta")
        with self.lock:
            return [self._public_job(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True)
                    if j["owner"] == owner and (video_id is None or j["video_id"] == video_id)][:100]

    def retry(self, owner, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["owner"] != owner:
                raise DeliveryError("Envio não encontrado.")
            if job["status"] in ACTIVE_STATES or job["status"] == "succeeded":
                return self._public_job(job)
            if job["status"] == "cancelled":
                raise DeliveryError("Este envio foi cancelado. Use Enviar novamente na exportação para criar um novo envio.")
            self._connection(job)
            self._verify_pin(job)
            self._update(job, status="queued", attempts=0, next_attempt=0, error=None)
        self.wake.set()
        return self._public_job(job)

    def cancel(self, owner, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["owner"] != owner:
                raise DeliveryError("Envio não encontrado.")
            if job["status"] in {"succeeded", "cancelled"}:
                return self._public_job(job)
            if job["status"] == "running":
                raise DeliveryError("O arquivo está sendo enviado. Aguarde a tentativa terminar para cancelar as próximas tentativas.")
            self._update(job, status="cancelled", error=None, next_attempt=0)
            self._release_pin(job)
        return self._public_job(job)

    def _connection(self, job):
        connection = self._owner_data(job["owner"]).get(job["provider"], {})
        if not connection or self._destination(job["provider"], connection) != job["destination"]:
            raise DeliveryError("O destino foi desconectado ou alterado. Reconecte a mesma conta e o mesmo destino para repetir este envio.")
        return connection

    def _verify_pin(self, job):
        path = Path(job["path"])
        if (path.parent != self.folder or path.is_symlink() or not path.is_file()
                or path.stat().st_size != job["total_bytes"] or _sha(path) != job["sha256"]):
            raise DeliveryError("O arquivo reservado não está mais íntegro. Gere uma nova exportação.")

    def _release_pin(self, job):
        """A failed local cleanup must never turn confirmed delivery into failure."""
        path = Path(job.get("path", ""))
        if path.parent == self.folder and path.name == job.get("id", "") + ".mp4":
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass  # Retried when the durable outbox is loaded again.

    def _update(self, job, **values):
        with self.lock:
            previous = dict(job)
            job.update(values, updated_at=_utc())
            try:
                self._save_jobs()
            except Exception:
                job.clear()
                job.update(previous)
                raise DeliveryError("Não foi possível salvar a fila de envios. Confira o espaço no armazenamento do servidor.",
                                    retryable=True, retry_after=60) from None

    def _drive_token(self, owner, connection):
        if connection.get("expires_at", 0) > self.clock() + 60:
            return connection["access_token"]
        with self._client_factory() as client:
            response = _request(client, "POST", "https://oauth2.googleapis.com/token", data={
                "client_id": os.getenv("AUTOCLIPS_GOOGLE_CLIENT_ID", ""),
                "client_secret": os.getenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET", ""),
                "refresh_token": connection["refresh_token"], "grant_type": "refresh_token"})
            if response.status_code in {400, 401}:
                raise DeliveryError("A autorização do Google Drive expirou. Reconecte sua conta.")
            token = _json(_check(response))
        if not token.get("access_token"):
            raise DeliveryError("O Google não renovou a autorização do Drive.")
        with self.vault.lock:
            state = self.vault.read()
            current = state.get("storage", {}).get(owner, {}).get("drive", {})
            if current.get("account_id") != connection.get("account_id") or current.get("refresh_token") != connection.get("refresh_token"):
                raise DeliveryError("A conexão com o Drive mudou durante a renovação. Tente novamente.")
            current.update(access_token=token["access_token"], expires_at=self.clock() + float(token.get("expires_in", 3600)))
            if token.get("refresh_token"):
                current["refresh_token"] = token["refresh_token"]
            self.vault.save(state)
        return token["access_token"]

    def _drive_existing(self, client, headers, job):
        file_id = job["remote"].get("file_id")
        if not file_id:
            return None
        response = _request(client, "GET", DRIVE_API + "/files/" + quote(file_id, safe=""), headers=headers,
                            params={"fields": "id,size,sha256Checksum,appProperties,trashed,parents"})
        if response.status_code == 404:
            return None
        item = _json(_check(response))
        if item.get("trashed"):
            raise DeliveryError("O arquivo remoto deste envio foi movido para a lixeira. Confira-o antes de repetir.")
        checksum = item.get("sha256Checksum")
        properties = item.get("appProperties", {})
        if not checksum and str(item.get("size")) == str(job["total_bytes"]):
            self._drive_not_confirmed()
        if str(item.get("size")) != str(job["total_bytes"]) or checksum != job["sha256"]:
            raise DeliveryError("Já existe um arquivo remoto para este envio, mas a integridade não foi confirmada. Confira o destino antes de tentar novamente.")
        if properties.get("autoclips_job") != job["dedupe"] or job["destination"][1] not in item.get("parents", []):
            raise DeliveryError("O arquivo remoto não corresponde ao destino autorizado para este envio.")
        return {"url": f"https://drive.google.com/file/d/{quote(file_id, safe='')}/view", "remote_id": file_id}

    def _drive_upload(self, job, connection):
        token = self._drive_token(job["owner"], connection)
        headers = {"Authorization": "Bearer " + token}
        with self._client_factory() as client:
            remote = dict(job["remote"])
            if not remote.get("file_id"):
                data = _json(_check(_request(client, "GET", DRIVE_API + "/files/generateIds", headers=headers,
                                             params={"count": 1, "space": "drive", "type": "files"})))
                ids = data.get("ids", [])
                if not ids:
                    raise DeliveryError("O Google Drive não reservou um identificador para o arquivo.")
                _safe_component(ids[0], "arquivo remoto")
                remote["file_id"] = ids[0]
                self._update(job, remote=remote)
            existing = self._drive_existing(client, headers, job)
            if existing:
                return existing
            folder = _json(_check(_request(client, "GET", DRIVE_API + "/files/" + quote(connection["folder_id"], safe=""),
                                            headers=headers, params={"fields": "id,mimeType,trashed,capabilities(canAddChildren)"})))
            if folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder" or folder.get("capabilities", {}).get("canAddChildren") is False:
                raise DeliveryError("A pasta AutoClips do Drive não está disponível para receber arquivos.")
            session = remote.get("session_url")
            if session and not _valid_drive_session(session):
                raise DeliveryError("O endereço de envio do Drive é inválido. Reconecte o armazenamento.")
            offset = 0
            if session:
                response = _request(client, "PUT", session, headers={**headers, "Content-Length": "0",
                                    "Content-Range": f"bytes */{job['total_bytes']}"}, content=b"")
                if response.status_code in {200, 201}:
                    return self._drive_existing(client, headers, job) or self._drive_not_confirmed()
                if response.status_code == 404:
                    remote.pop("session_url", None)
                    self._update(job, remote=remote)
                    session = None
                elif response.status_code == 308:
                    offset = self._drive_offset(response, job["total_bytes"])
                else:
                    _check(response)
            if not session:
                response = _request(client, "POST", DRIVE_UPLOAD, headers={**headers,
                                    "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(job["total_bytes"])},
                                    params={"uploadType": "resumable", "fields": "id,size,sha256Checksum,appProperties,parents"},
                                    json={"id": remote["file_id"], "name": job["filename"], "parents": [connection["folder_id"]],
                                          "mimeType": "video/mp4", "appProperties": {"autoclips_job": job["dedupe"]}})
                if response.status_code == 409:
                    return self._drive_existing(client, headers, job) or self._drive_not_confirmed()
                _check(response, (200, 201))
                session = response.headers.get("Location", "")
                if not _valid_drive_session(session):
                    raise DeliveryError("O Google Drive retornou um endereço de envio inválido.")
                remote["session_url"] = session
                self._update(job, remote=remote)
            no_progress = 0
            with Path(job["path"]).open("rb") as stream:
                while offset < job["total_bytes"]:
                    if self.stopping.is_set():
                        raise DeliveryError("O envio será retomado após reiniciar o servidor.", retryable=True)
                    self._connection(job)
                    stream.seek(offset)
                    chunk = stream.read(min(CHUNK, job["total_bytes"] - offset))
                    end = offset + len(chunk) - 1
                    response = _request(client, "PUT", session, headers={**headers, "Content-Type": "video/mp4",
                                        "Content-Length": str(len(chunk)), "Content-Range": f"bytes {offset}-{end}/{job['total_bytes']}"}, content=chunk)
                    if response.status_code in {200, 201}:
                        return self._drive_existing(client, headers, job) or self._drive_not_confirmed()
                    if response.status_code == 308:
                        acknowledged = self._drive_offset(response, job["total_bytes"])
                        if acknowledged > end + 1:
                            raise DeliveryError("O Google Drive informou um progresso inválido.")
                        no_progress = no_progress + 1 if acknowledged <= offset else 0
                        if no_progress >= 3:
                            raise DeliveryError("O Drive não confirmou novos dados. O envio será retomado.", retryable=True)
                        offset = acknowledged
                        self._update(job, bytes_sent=offset, progress=min(99, int(offset * 100 / job["total_bytes"])))
                    elif response.status_code == 404:
                        remote.pop("session_url", None)
                        self._update(job, remote=remote)
                        raise DeliveryError("A sessão de upload expirou. O arquivo será conferido e o envio retomado.", retryable=True)
                    else:
                        _check(response)
            return self._drive_existing(client, headers, job) or self._drive_not_confirmed()

    @staticmethod
    def _drive_not_confirmed():
        raise DeliveryError("O Drive ainda não confirmou a integridade do arquivo. O resultado será conferido novamente.", retryable=True)

    @staticmethod
    def _drive_offset(response, total):
        value = response.headers.get("Range")
        if not value:
            return 0
        found = re.fullmatch(r"bytes=0-(\d+)", value.strip())
        if not found or not 0 <= int(found[1]) < total:
            raise DeliveryError("O Google Drive informou um progresso inválido.")
        return int(found[1]) + 1

    def _github_assets(self, client, headers, connection):
        items = []
        for page in range(1, 11):
            part = _json(_check(_request(client, "GET", GITHUB_API + f"/repos/{connection['repository']}/releases/{connection['release_id']}/assets",
                                         headers=headers, params={"per_page": 100, "page": page})))
            if not isinstance(part, list):
                raise DeliveryError("O GitHub retornou uma lista de arquivos inválida.")
            items.extend(part)
            if len(part) < 100:
                break
        return items

    @staticmethod
    def _github_existing(items, job, connection):
        found = next((asset for asset in items if asset.get("name") == job["asset_name"]), None)
        if found is None:
            return None
        if (found.get("state") != "uploaded" or found.get("size") != job["total_bytes"]
                or found.get("digest") != "sha256:" + job["sha256"]):
            raise DeliveryError("O GitHub já tem um arquivo deste envio, mas sua integridade não foi confirmada. Confira a Release antes de repetir; o arquivo remoto não será substituído.")
        # An API-provided arbitrary URL must never become an executable UI link.
        return {"url": f"https://github.com/{connection['repository']}/releases", "remote_id": str(found["id"])}

    def _github_upload(self, job, connection):
        headers = self._github_headers(connection["token"])
        with self._client_factory() as client:
            repo = self._github_repo(client, headers, connection["repository"])
            if str(repo["id"]) != connection["repo_id"]:
                raise DeliveryError("O repositório de destino mudou. Reconecte-o antes de enviar arquivos.")
            release = _json(_check(_request(client, "GET", GITHUB_API + f"/repos/{connection['repository']}/releases/{connection['release_id']}", headers=headers)))
            if release.get("draft") is not True:
                raise DeliveryError("A Release de destino não está mais em rascunho. O envio foi interrompido.")
            assets = self._github_assets(client, headers, connection)
            existing = self._github_existing(assets, job, connection)
            if existing:
                return existing
            if len(assets) >= 1000:
                raise DeliveryError("A Release atingiu 1.000 arquivos. Conecte outro destino para novos cortes.")
            upload_url = str(release.get("upload_url", "")).split("{", 1)[0]
            parsed = urlsplit(upload_url)
            expected = f"/repos/{connection['repository']}/releases/{connection['release_id']}/assets"
            if (parsed.scheme != "https" or parsed.hostname != "uploads.github.com" or parsed.port not in {None, 443}
                    or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != expected):
                raise DeliveryError("O GitHub retornou um endereço de upload inválido.")
            self._connection(job)
            with Path(job["path"]).open("rb") as stream:
                response = _request(client, "POST", upload_url, headers={**headers, "Content-Type": "video/mp4",
                                    "Content-Length": str(job["total_bytes"])}, params={"name": job["asset_name"]},
                                    content=self._file_chunks(stream, job))
            if response.status_code == 422:
                return self._github_existing(self._github_assets(client, headers, connection), job, connection) or self._github_not_confirmed()
            _check(response, (201,))
            # Re-read the authoritative asset collection, including hash/size.
            return self._github_existing(self._github_assets(client, headers, connection), job, connection) or self._github_not_confirmed()

    def _file_chunks(self, stream, job):
        sent = 0
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                return
            if self.stopping.is_set():
                raise DeliveryError("O envio será conferido e retomado após reiniciar o servidor.", retryable=True)
            self._connection(job)
            sent += len(chunk)
            self._update(job, bytes_sent=sent, progress=min(99, int(sent * 100 / job["total_bytes"])))
            yield chunk

    @staticmethod
    def _github_not_confirmed():
        raise DeliveryError("O GitHub ainda não confirmou o arquivo. O resultado será conferido antes de uma nova tentativa.", retryable=True)

    def run_once(self):
        """Process one due item. Useful for deterministic tests and the worker."""
        if not self.run_lock.acquire(blocking=False):
            return False
        try:
            return self._run_once()
        finally:
            self.run_lock.release()

    def _run_once(self):
        with self.lock:
            job = next((j for j in sorted(self.jobs.values(), key=lambda j: j["created_at"])
                        if j["status"] in {"queued", "retry_wait"} and j.get("next_attempt", 0) <= self.clock()), None)
            if job is None:
                return False
            try:
                self._update(job, status="running", attempts=job["attempts"] + 1, error=None)
            except DeliveryError:
                # Do not send any bytes until the attempt is durable. Keep the
                # previous state eligible when disk space becomes available.
                logger.warning("Storage outbox could not persist a worker claim; delivery paused.")
                return False
        try:
            connection = self._connection(job)
            self._verify_pin(job)
            result = self._drive_upload(job, connection) if job["provider"] == "drive" else self._github_upload(job, connection)
            self._update(job, status="succeeded", progress=100, bytes_sent=job["total_bytes"], url=result["url"],
                         remote_id=result["remote_id"], error=None)
            self._release_pin(job)
        except Exception as error:
            safe = error if isinstance(error, DeliveryError) else DeliveryError("Não foi possível concluir o envio. Confira a conexão e tente novamente.")
            again = safe.retryable and job["attempts"] < MAX_ATTEMPTS
            message = str(safe)
            if safe.retryable and not again:
                message += " As tentativas automáticas terminaram. Use Tentar novamente para retomar."
            try:
                self._update(job, status="retry_wait" if again else "failed", error=message,
                             next_attempt=self.clock() + max(safe.retry_after, 10 * 2 ** (job["attempts"] - 1)) if again else 0)
            except DeliveryError as persistence_error:
                # The persisted running state is safe to reconcile on restart.
                # Keep this live worker usable too, without dropping the pin or
                # inventing success after an uncertain remote operation.
                with self.lock:
                    job.update(status="retry_wait", error=str(persistence_error),
                               next_attempt=self.clock() + 60, updated_at=_utc())
        return True

    def start(self):
        with self.lock:
            if self.worker is not None and self.worker.is_alive():
                return
            self.stopping.clear()
            self.worker = threading.Thread(target=self._loop, name="autoclips-storage", daemon=True)
            self.worker.start()

    def _loop(self):
        while not self.stopping.is_set():
            try:
                processed = self.run_once()
            except Exception:
                logger.warning("Storage worker paused after a local queue error; it will retry.")
                processed = False
            if not processed:
                self.wake.wait(2)
                self.wake.clear()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        thread = self.worker
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
