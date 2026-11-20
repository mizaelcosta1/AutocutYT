"""Read-only capacity for the working volume and the caller's Google Drive.

The caller authenticates the owner before invoking this report. Drive quota is
queried only here, never at import or while returning ordinary settings. A
remote destination does not add capacity to the local processing volume.
"""
from __future__ import annotations

import shutil

from storage_delivery import DRIVE_API, DeliveryError, _check, _json, _request


WORKING_RESERVE_BYTES = 64 * 1024 * 1024
_MAX_INT64 = (1 << 63) - 1
_CHANGED = "A conexão com o Google Drive mudou durante a consulta. Atualize o armazenamento para consultar a conta atual."
_INVALID = "O Google Drive retornou informações de espaço inválidas. Tente atualizar o armazenamento."
_UNAVAILABLE = "Não foi possível consultar o espaço do Google Drive. Confira a conexão e tente atualizar o armazenamento."


def _drive_result(*, connected, status, error=None, total=None, used=None, maximum=None):
    return {
        "connected": connected,
        "status": status,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": max(0, total - used) if total is not None and used is not None else None,
        "max_upload_bytes": maximum,
        "error": error,
    }


def _connection(service, owner):
    # Only inspect the explicitly authenticated owner's storage entry. Copy the
    # record so an in-memory provider implementation cannot mutate our snapshot.
    value = service._owner_data(owner).get("drive", {})
    return dict(value) if isinstance(value, dict) else {}


def _connected(connection):
    return bool(connection.get("refresh_token") and connection.get("folder_id"))


def _destination(connection):
    return connection.get("account_id"), connection.get("folder_id")


def _connection_revision(connection):
    # Private comparison only: credentials never enter the response or logs.
    return (*_destination(connection), connection.get("refresh_token"))


def _bytes(value):
    if value is None:
        return None
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 19:
        result = int(value)
    else:
        raise DeliveryError(_INVALID)
    if not 0 <= result <= _MAX_INT64:
        raise DeliveryError(_INVALID)
    return result


def _quota(payload):
    if not isinstance(payload, dict):
        raise DeliveryError(_INVALID)
    quota = payload.get("storageQuota")
    if quota is None:
        quota = {}
    if not isinstance(quota, dict):
        raise DeliveryError(_INVALID)
    # "usage" includes all services sharing this Google quota (or the pooled
    # organization quota). usageInDrive alone would overstate available space.
    return _drive_result(connected=True, status="ready", total=_bytes(quota.get("limit")),
                         used=_bytes(quota.get("usage")), maximum=_bytes(payload.get("maxUploadSize")))


def _drive_capacity(service, owner):
    try:
        connection = _connection(service, owner)
    except Exception:
        return _drive_result(connected=False, status="error", error=_UNAVAILABLE)
    if not _connected(connection):
        return _drive_result(connected=False, status="disconnected")

    baseline = connection
    try:
        token = service._drive_token(owner, connection)
        current = _connection(service, owner)
        if not _connected(current):
            return _drive_result(connected=False, status="disconnected")
        if _destination(current) != _destination(connection):
            raise DeliveryError(_CHANGED)
        # A normal offline renewal may rotate the refresh token. Compare the
        # post-renewal record when detecting reconnections during the GET.
        baseline = current
        with service._client_factory() as client:
            response = _request(client, "GET", DRIVE_API + "/about",
                                headers={"Authorization": "Bearer " + token},
                                params={"fields": "storageQuota,maxUploadSize"})
            report = _quota(_json(_check(response)))
    except DeliveryError as error:
        # DeliveryError messages from the shared transport/OAuth helpers are
        # fixed, safe application text; provider bodies are never returned.
        report = _drive_result(connected=True, status="error", error=str(error))
    except Exception:
        report = _drive_result(connected=True, status="error", error=_UNAVAILABLE)

    # Do not return the previous account's quota after disconnect/reconnect,
    # including a connection change while an unsuccessful request was pending.
    try:
        current = _connection(service, owner)
    except Exception:
        return _drive_result(connected=True, status="error", error=_UNAVAILABLE)
    if not _connected(current):
        return _drive_result(connected=False, status="disconnected")
    if _connection_revision(current) != _connection_revision(baseline):
        return _drive_result(connected=True, status="error", error=_CHANGED)
    return report


def capacity_report(service, owner):
    """Return a fresh, owner-scoped report without changing storage policies."""
    disk = shutil.disk_usage(service.root)
    return {
        "local": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "low_space": disk.free < WORKING_RESERVE_BYTES,
            "reserve_bytes": WORKING_RESERVE_BYTES,
        },
        "drive": _drive_capacity(service, owner),
    }
