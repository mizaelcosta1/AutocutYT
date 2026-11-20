"""Verified Drive archives for completed exports, with bounded media streaming.

An archive is a durable reference to the exact export, not permission to remove
arbitrary source media. The application supplies a guard which serializes the
final local-file check with rendering and publication. Nothing in this module
removes a remote file or changes its sharing permissions.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from storage_delivery import CHUNK, DRIVE_API, DeliveryError, _check, _sha


RESTORE_RESERVE = 64 * 1024 * 1024
MEDIA_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Accept-Ranges": "bytes"}


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _byte_range(value, size):
    """Accept one byte range, including an open end or a suffix range."""
    if value is None:
        return 0, size - 1, False

    def invalid():
        raise HTTPException(416, "Intervalo de vídeo inválido.",
                            headers={**MEDIA_HEADERS, "Content-Range": f"bytes */{size}"})

    if not isinstance(value, str) or len(value) > 128 or size < 1:
        invalid()
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip(), re.IGNORECASE)
    if not match or not any(match.groups()):
        invalid()
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix < 1:
            invalid()
        return max(0, size - suffix), size - 1, True
    start = int(first)
    end = min(size - 1, int(last)) if last else size - 1
    if start >= size or start > end:
        invalid()
    return start, end, True


def _media_headers(size, start, end, partial, filename, download):
    # Callers usually supply an ASCII export filename, but also handle Unicode
    # safely without allowing control characters or paths into a header.
    name = str(filename or "autoclips.mp4").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "_", name)[:180] or "autoclips.mp4"
    fallback = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "autoclips.mp4"
    disposition = "attachment" if download else "inline"
    headers = {**MEDIA_HEADERS, "Content-Length": str(max(0, end - start + 1)),
               "Content-Disposition": f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{quote(name, safe="")}' }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return headers


def local_response(path, range_header=None, download=False, filename=None):
    """Open the inode now so a concurrent archive cannot break an active read.

    The caller holds the application's media lock while constructing this
    response. The descriptor stays valid if the pathname is subsequently
    removed after verified archival; the kernel releases its space on close.
    """
    try:
        stream = Path(path).open("rb")
    except OSError:
        raise HTTPException(404, "O arquivo exportado não está disponível no servidor.") from None
    try:
        size = os.fstat(stream.fileno()).st_size
        start, end, partial = _byte_range(range_header, size)
        stream.seek(start)
        headers = _media_headers(size, start, end, partial, filename, download)
    except BaseException:
        stream.close()
        raise

    def chunks():
        remaining = end - start + 1
        try:
            while remaining > 0:
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    raise DeliveryError("O arquivo local mudou durante o download. Tente novamente.")
                remaining -= len(block)
                yield block
        finally:
            stream.close()

    return StreamingResponse(chunks(), status_code=206 if partial else 200,
                             media_type="video/mp4", headers=headers,
                             background=BackgroundTask(stream.close))


class StorageArchive:
    def __init__(self, service, guard=None):
        self.service = service
        self.guard = guard or (lambda _job: nullcontext(True))
        self.restore_lock = threading.Lock()

    def _snapshot(self, job):
        with self.service.lock:
            current = self.service.jobs.get(job.get("id")) if isinstance(job, dict) else None
            if (not current or current.get("owner") != job.get("owner")
                    or current.get("dedupe") != job.get("dedupe")
                    or current.get("provider") != "drive" or current.get("status") != "succeeded"
                    or current.get("quality") != "final" or current.get("kind") not in {"clip", "timeline"}):
                raise DeliveryError("Arquivo arquivado não encontrado para esta conta.")
            return {**current, "remote": dict(current.get("remote", {})),
                    "destination": list(current.get("destination", []))}

    def _source(self, job):
        source = Path(job.get("source_path") or "")
        if not source.is_absolute() or source.suffix.lower() != ".mp4":
            raise DeliveryError("Esta exportação não tem um arquivo local válido para arquivar.")
        try:
            relative = source.relative_to(self.service.root)
            cursor = self.service.root
            for component in relative.parts:
                cursor = cursor / component
                if cursor.is_symlink():
                    raise ValueError()
            resolved = source.resolve()
            resolved.relative_to(self.service.root)
            if resolved.is_relative_to(self.service.folder):
                raise ValueError()
        except (ValueError, OSError):
            raise DeliveryError("Esta exportação não tem um arquivo local válido para arquivar.") from None
        return resolved

    def _save_error(self, job, error):
        message = str(error) if isinstance(error, DeliveryError) else (
            "O envio terminou, mas não foi possível liberar a cópia local. Tente arquivar novamente.")
        try:
            with self.service.lock:
                current = self.service.jobs.get(job.get("id"))
                if current and current.get("owner") == job.get("owner"):
                    self.service._update(current, archive_error=message)
        except Exception:
            # Failure to save cannot undo a successful upload or justify an
            # unlink. A previously persisted verified reference remains valid.
            pass

    def _verified(self, client, headers, job):
        result = self.service._drive_existing(client, headers, job)
        if not result:
            raise DeliveryError("O arquivo arquivado não foi encontrado no Drive. Confira a conexão e a lixeira.")
        if result.get("can_download") is not True:
            raise DeliveryError("O Drive não confirmou a permissão de download. Confira as permissões do arquivo.")
        return result

    def archive(self, job):
        """Try to release a completed local export, preserving upload success.

        Returns False for deferred or failed archival. The upload's status is
        never changed. The verified state is persisted *before* unlink, so a
        restart between unlink and the final accounting update is recoverable.
        """
        try:
            snapshot = self._snapshot(job)
            if not snapshot.get("archive_requested"):
                return False
            if snapshot.get("archived"):
                return True
            source = self._source(snapshot)
            connection = self.service._connection(snapshot)
            token = self.service._drive_token(snapshot["owner"], connection)
            # Refresh may legitimately rotate the refresh token. Use the
            # connection after renewal when checking for a concurrent change.
            connection = self.service._connection(snapshot)
            fingerprint = (connection.get("account_id"), connection.get("folder_id"),
                           connection.get("refresh_token"))
            with self.service._client_factory() as client:
                result = self._verified(client, {"Authorization": "Bearer " + token}, snapshot)
            with self.guard(snapshot) as allowed:
                if not allowed:
                    return False
                with self.service.lock, self.service.vault.lock:
                    current = self.service.jobs.get(snapshot["id"])
                    if (not current or current.get("status") != "succeeded"
                            or not current.get("archive_requested")
                            or current.get("sha256") != snapshot.get("sha256")
                            or current.get("source_path") != snapshot.get("source_path")):
                        return False
                    settings = self.service.public_settings(current["owner"])
                    if (not settings.get("archive_after_upload") or not settings.get("auto_upload")
                            or settings.get("provider") != "drive"):
                        return False
                    live = self.service._connection(current)
                    if (live.get("account_id"), live.get("folder_id"), live.get("refresh_token")) != fingerprint:
                        raise DeliveryError("A conexão do Drive mudou durante o arquivamento. A cópia local foi preservada.")
                    source = self._source(current)
                    if not source.exists():
                        if current.get("archive_state") != "verified":
                            raise DeliveryError("O arquivo local mudou antes de ser arquivado. A referência remota foi preservada.")
                    else:
                        before = source.stat()
                        if (not source.is_file() or before.st_size != current["total_bytes"]
                                or _sha(source) != current["sha256"]):
                            raise DeliveryError("A exportação local foi substituída. O arquivo atual foi preservado.")
                        if before.st_nlink > 1:
                            raise DeliveryError("A cópia local ainda está reservada para outro envio. O espaço será liberado quando ele terminar.")
                        after = source.stat()
                        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                            raise DeliveryError("A exportação mudou durante a verificação. O arquivo atual foi preservado.")
                        # This durable record contains remote ID, owner, exact
                        # render ID, checksum and size from the succeeded job.
                        self.service._update(current, archive_state="verified", archive_error=None,
                                             archive_verified_at=_utc(), remote_id=result["remote_id"])
                        source.unlink()
                    self.service._release_pin(current)
                    self.service._update(current, archive_state="archived", archived=True,
                                         archived_bytes=current["total_bytes"], archived_at=_utc(), archive_error=None)
            return True
        except Exception as error:
            self._save_error(job, error)
            return False

    def found(self, owner, video_id, kind, quality, render_id, clip_id=None):
        if not owner or not render_id or quality != "final":
            return None
        with self.service.lock:
            for job in reversed(list(self.service.jobs.values())):
                if (job.get("owner") == owner and job.get("video_id") == video_id
                        and job.get("kind") == kind and job.get("quality") == quality
                        and job.get("render_id") == render_id and job.get("clip_id") == clip_id
                        and job.get("provider") == "drive" and job.get("status") == "succeeded"
                        and (job.get("archived") or job.get("archive_state") in {"verified", "archived"})):
                    return {**job, "remote": dict(job.get("remote", {})),
                            "destination": list(job.get("destination", []))}
        return None

    @contextmanager
    def _open_remote(self, job, range_header=None):
        snapshot = self._snapshot(job)
        if not (snapshot.get("archived") or snapshot.get("archive_state") in {"verified", "archived"}):
            raise DeliveryError("Esta exportação ainda não foi arquivada no Drive.")
        size = int(snapshot["total_bytes"])
        start, end, partial = _byte_range(range_header, size)
        connection = self.service._connection(snapshot)
        token = self.service._drive_token(snapshot["owner"], connection)
        headers = {"Authorization": "Bearer " + token, "Accept-Encoding": "identity"}
        with self.service._client_factory() as client:
            result = self._verified(client, headers, snapshot)
            self.service._connection(snapshot)
            if partial:
                headers["Range"] = f"bytes={start}-{end}"
            try:
                with client.stream("GET", DRIVE_API + "/files/" + quote(result["remote_id"], safe=""),
                                   params={"alt": "media"}, headers=headers, follow_redirects=False) as response:
                    expected_status = 206 if partial else 200
                    if response.status_code != expected_status:
                        if response.status_code in {200, 206}:
                            raise DeliveryError("O Drive não confirmou o intervalo solicitado. Tente reproduzir novamente.")
                        # Do not read a provider error body or pass private
                        # response headers into the user's download.
                        _check(httpx.Response(response.status_code, json={}), accepted=(expected_status,))
                    expected_length = end - start + 1
                    length = response.headers.get("Content-Length")
                    if length is not None and (not length.isdigit() or int(length) != expected_length):
                        raise DeliveryError("O Drive retornou um tamanho inesperado. Tente baixar novamente.")
                    if partial and response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise DeliveryError("O Drive retornou um intervalo inesperado. Tente reproduzir novamente.")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise DeliveryError("O Drive retornou um formato de transferência inesperado. Tente novamente.")
                    yield response, snapshot, start, end, partial
            except httpx.HTTPError:
                raise DeliveryError("A conexão com o Drive foi interrompida. Tente baixar novamente.", retryable=True) from None

    @staticmethod
    def _remote_chunks(response, expected):
        count = 0
        try:
            for block in response.iter_bytes(chunk_size=CHUNK):
                count += len(block)
                if count > expected:
                    raise DeliveryError("O Drive retornou mais bytes que a exportação verificada.")
                yield block
            if count != expected:
                raise DeliveryError("O download do Drive foi interrompido antes de terminar.")
        except httpx.HTTPError:
            raise DeliveryError("A conexão com o Drive foi interrompida. Tente baixar novamente.", retryable=True) from None

    def response(self, job, range_header=None, download=False, filename=None):
        stack = ExitStack()
        try:
            response, snapshot, start, end, partial = stack.enter_context(self._open_remote(job, range_header))
            headers = _media_headers(snapshot["total_bytes"], start, end, partial,
                                     filename or snapshot.get("filename"), download)
        except BaseException:
            stack.close()
            raise

        def chunks():
            try:
                yield from self._remote_chunks(response, end - start + 1)
            finally:
                stack.close()

        return StreamingResponse(chunks(), status_code=206 if partial else 200, media_type="video/mp4",
                                 headers=headers, background=BackgroundTask(stack.close))

    def cleanup_interrupted_restores(self):
        """Remove derived restoration files once, before workers start.

        Do not call this while publication workers are active. On a new server
        process the old tasks cannot continue, and every such file can be
        restored again from its durable, verified Drive archive.
        """
        removed = 0
        for path in self.service.folder.glob("restore-*"):
            if not re.fullmatch(r"restore-[a-z0-9_]+\.(?:part|mp4)", path.name):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed

    @contextmanager
    def temporary_local(self, job):
        """Restore verified bytes only while an operation needs a local path."""
        with self.restore_lock:
            snapshot = self._snapshot(job)
            size = int(snapshot["total_bytes"])
            if shutil.disk_usage(self.service.root).free < size + RESTORE_RESERVE:
                raise DeliveryError("Não há espaço de edição para recuperar este corte. Libere espaço ou amplie o volume do Railway; o arquivo continua no Drive.")
            part = final = None
            try:
                try:
                    fd, temporary = tempfile.mkstemp(prefix="restore-", suffix=".part", dir=self.service.folder)
                    part = Path(temporary)
                    final = part.with_suffix(".mp4")
                    digest = hashlib.sha256()
                    with os.fdopen(fd, "wb") as stream:
                        with self._open_remote(snapshot) as (response, _job, _start, _end, _partial):
                            for block in self._remote_chunks(response, size):
                                stream.write(block)
                                digest.update(block)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if digest.hexdigest() != snapshot["sha256"]:
                        raise DeliveryError("A integridade do corte recuperado do Drive não foi confirmada. O arquivo não será usado.")
                    os.replace(part, final)
                except OSError:
                    raise DeliveryError("Não foi possível recuperar o corte no disco de edição. Confira o espaço disponível; o arquivo continua no Drive.") from None
                yield final
            finally:
                for path in (part, final):
                    if path is not None:
                        path.unlink(missing_ok=True)
