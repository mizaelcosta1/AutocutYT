"""Capacity reports use the caller's Drive quota without changing storage."""
from collections import namedtuple
import json

import httpx
import pytest

from integrations import Vault
import storage_capacity as capacity
from storage_delivery import DRIVE_SCOPE, StorageDelivery


OWNER = "owner-a"
CONNECTION = {
    "account_id": "google-a", "folder_id": "folder-a",
    "access_token": "test-access-private", "refresh_token": "test-refresh-private",
    "expires_at": 10_000, "scope": DRIVE_SCOPE,
}


class DriveAPI:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {
            "storageQuota": {"limit": "1000", "usage": "750", "usageInDrive": "100"},
            "maxUploadSize": "500",
        }
        self.requests = []
        self.before_quota = None
        self.response = None
        self.rotate_token = False

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com" and request.url.path == "/token":
            assert request.method == "POST"
            token = {"access_token": "test-renewed-private", "expires_in": 3600}
            if self.rotate_token:
                token["refresh_token"] = "test-rotated-refresh-private"
            return httpx.Response(200, json=token)
        assert request.method == "GET"
        assert request.url.host == "www.googleapis.com"
        assert request.url.path == "/drive/v3/about"
        assert dict(request.url.params) == {"fields": "storageQuota,maxUploadSize"}
        if self.before_quota:
            self.before_quota()
        return self.response if self.response is not None else httpx.Response(200, json=self.payload)


def service(root, api):
    transport = httpx.MockTransport(api)
    return StorageDelivery(root, Vault(root), clock=lambda: 100,
                           client_factory=lambda: httpx.Client(transport=transport, follow_redirects=False,
                                                               trust_env=False))


def connect(backend, owner=OWNER, **changes):
    backend._save_connection(owner, "drive", {**CONNECTION, **changes})


def test_report_separates_working_volume_from_shared_google_quota(tmp_path, monkeypatch):
    api = DriveAPI()
    backend = service(tmp_path, api)
    connect(backend)
    disk = namedtuple("DiskUsage", "total used free")(500_000_000, 460_000_000, 40_000_000)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda root: disk if root == backend.root else pytest.fail("Wrong volume"))
    report = capacity.capacity_report(backend, OWNER)
    assert report["local"] == {
        "total_bytes": 500_000_000, "used_bytes": 460_000_000, "free_bytes": 40_000_000,
        "reserve_bytes": 64 * 1024 * 1024, "low_space": True,
    }
    assert report["drive"] == {
        "connected": True, "status": "ready", "total_bytes": 1000, "used_bytes": 750,
        "free_bytes": 250, "max_upload_bytes": 500, "error": None,
    }
    assert len(api.requests) == 1
    serialized = json.dumps(report)
    for private in (str(backend.root), CONNECTION["access_token"], CONNECTION["refresh_token"], "google-a", "folder-a"):
        assert private not in serialized
    assert backend.public_settings(OWNER)["auto_upload"] is False


@pytest.mark.parametrize("free,low", [(0, True), (64 * 1024 * 1024 - 1, True), (64 * 1024 * 1024, False)])
def test_local_working_reserve_boundary(tmp_path, monkeypatch, free, low):
    backend = service(tmp_path, DriveAPI())
    disk = namedtuple("DiskUsage", "total used free")(2 * capacity.WORKING_RESERVE_BYTES,
                                                          2 * capacity.WORKING_RESERVE_BYTES - free, free)
    monkeypatch.setattr(capacity.shutil, "disk_usage", lambda _: disk)
    assert capacity.capacity_report(backend, OWNER)["local"]["low_space"] is low


@pytest.mark.parametrize("connection", [{}, {"folder_id": "folder-a"}, {"refresh_token": "private"}])
def test_disconnected_drive_never_initializes_client_or_renews_tokens(tmp_path, monkeypatch, connection):
    backend = service(tmp_path, DriveAPI())
    backend._save_connection(OWNER, "drive", connection)
    monkeypatch.setattr(backend, "_client_factory", lambda: pytest.fail("No client while disconnected"))
    monkeypatch.setattr(backend, "_drive_token", lambda *_: pytest.fail("No token renewal while disconnected"))
    assert capacity.capacity_report(backend, OWNER)["drive"] == {
        "connected": False, "status": "disconnected", "total_bytes": None, "used_bytes": None,
        "free_bytes": None, "max_upload_bytes": None, "error": None,
    }


@pytest.mark.parametrize("payload,expected", [
    ({}, (None, None, None, None)),
    ({"storageQuota": None, "maxUploadSize": None}, (None, None, None, None)),
    ({"storageQuota": {"usage": "250"}}, (None, 250, None, None)),
    ({"storageQuota": {"limit": "1000", "usageInDrive": "100"}}, (1000, None, None, None)),
    ({"storageQuota": {"limit": "100", "usage": "150"}}, (100, 150, 0, None)),
    ({"storageQuota": {"limit": 0, "usage": 0}, "maxUploadSize": "0"}, (0, 0, 0, 0)),
    ({"storageQuota": {"limit": "9223372036854775807", "usage": "1"}},
     (9223372036854775807, 1, 9223372036854775806, None)),
])
def test_missing_and_boundary_quota_values_are_not_invented(tmp_path, payload, expected):
    backend = service(tmp_path, DriveAPI(payload))
    connect(backend)
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["status"] == "ready" and result["error"] is None
    assert tuple(result[key] for key in ("total_bytes", "used_bytes", "free_bytes", "max_upload_bytes")) == expected


@pytest.mark.parametrize("payload", [
    [], {"storageQuota": []}, {"storageQuota": {"usage": "provider-private-message"}},
    {"storageQuota": {"limit": -1}}, {"storageQuota": {"limit": True}},
    {"storageQuota": {"usage": 3.5}}, {"maxUploadSize": "1e20"},
    {"storageQuota": {"limit": "9223372036854775808"}},
])
def test_invalid_quota_is_an_error_without_echoing_provider_data(tmp_path, payload):
    backend = service(tmp_path, DriveAPI(payload))
    connect(backend)
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["connected"] is True and result["status"] == "error"
    assert "inválidas" in result["error"]
    assert all(result[key] is None for key in ("total_bytes", "used_bytes", "free_bytes", "max_upload_bytes"))
    assert "provider-private-message" not in json.dumps(result)


@pytest.mark.parametrize("code,body,expected", [
    (401, {"error": "provider-private-message"}, "Reconecte"),
    (403, {"error": {"message": "provider-private-message", "errors": [{"reason": "accessNotConfigured"}]}}, "Ative a API"),
    (429, {"error": "provider-private-message"}, "temporariamente"),
    (500, {"error": "provider-private-message"}, "temporariamente"),
    (302, {"error": "provider-private-message"}, "redirecionou"),
])
def test_provider_failures_have_safe_actionable_errors(tmp_path, code, body, expected):
    api = DriveAPI()
    api.response = httpx.Response(code, json=body)
    backend = service(tmp_path, api)
    connect(backend)
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["status"] == "error" and result["connected"]
    assert expected in result["error"]
    assert result["free_bytes"] is None
    assert "provider-private-message" not in json.dumps(result)


def test_malformed_json_and_unexpected_errors_never_expose_provider_or_token(tmp_path, monkeypatch):
    api = DriveAPI()
    api.response = httpx.Response(200, content=b"provider-private-message")
    backend = service(tmp_path, api)
    connect(backend)
    first = capacity.capacity_report(backend, OWNER)["drive"]
    assert first["status"] == "error" and "inválida" in first["error"]
    def failure():
        raise RuntimeError("provider-private-message " + CONNECTION["access_token"])
    monkeypatch.setattr(backend, "_client_factory", failure)
    second = capacity.capacity_report(backend, OWNER)["drive"]
    assert second["status"] == "error" and "Confira a conexão" in second["error"]
    for result in (first, second):
        assert "provider-private-message" not in json.dumps(result)
        assert CONNECTION["access_token"] not in json.dumps(result)


def test_expired_token_is_renewed_and_normal_rotation_is_accepted(tmp_path):
    api = DriveAPI()
    api.rotate_token = True
    backend = service(tmp_path, api)
    connect(backend, expires_at=1)
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["status"] == "ready" and result["free_bytes"] == 250
    assert [r.url.path for r in api.requests] == ["/token", "/drive/v3/about"]
    assert api.requests[-1].headers["Authorization"] == "Bearer test-renewed-private"
    assert "test-rotated-refresh-private" not in json.dumps(result)


def test_report_only_reads_the_requested_owner_and_does_not_cache_other_accounts(tmp_path, monkeypatch):
    api = DriveAPI()
    backend = service(tmp_path, api)
    connect(backend)
    connect(backend, owner="owner-b", account_id="google-b", access_token="owner-b-private")
    read_owner = backend._owner_data
    owners = []
    def scoped(owner):
        owners.append(owner)
        return read_owner(owner)
    monkeypatch.setattr(backend, "_owner_data", scoped)
    assert capacity.capacity_report(backend, OWNER)["drive"]["free_bytes"] == 250
    assert set(owners) == {OWNER}
    api.payload["storageQuota"]["usage"] = "900"
    assert capacity.capacity_report(backend, "owner-b")["drive"]["free_bytes"] == 100
    assert api.requests[-1].headers["Authorization"] == "Bearer owner-b-private"
    assert len(api.requests) == 2


@pytest.mark.parametrize("change", ["disconnect", "account", "folder", "reauthorize"])
def test_connection_changes_during_quota_request_discard_previous_result(tmp_path, change):
    api = DriveAPI()
    backend = service(tmp_path, api)
    connect(backend)
    def update():
        if change == "disconnect":
            backend.disconnect(OWNER, "drive")
        else:
            field = {"account": "account_id", "folder": "folder_id", "reauthorize": "refresh_token"}[change]
            connect(backend, **{field: "replacement-private"})
    api.before_quota = update
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["free_bytes"] is None and result["total_bytes"] is None
    if change == "disconnect":
        assert result["status"] == "disconnected" and result["connected"] is False and result["error"] is None
    else:
        assert result["status"] == "error" and result["connected"] is True and "mudou" in result["error"]
    assert "replacement-private" not in json.dumps(result)


def test_reconnection_during_token_lookup_skips_old_account_quota(tmp_path, monkeypatch):
    api = DriveAPI()
    backend = service(tmp_path, api)
    connect(backend)
    def token_lookup(*_):
        connect(backend, account_id="different-account", refresh_token="different-private")
        return CONNECTION["access_token"]
    monkeypatch.setattr(backend, "_drive_token", token_lookup)
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["status"] == "error" and "mudou" in result["error"]
    assert not api.requests


def test_later_disconnection_does_not_return_a_previous_success(tmp_path):
    api = DriveAPI()
    backend = service(tmp_path, api)
    connect(backend)
    assert capacity.capacity_report(backend, OWNER)["drive"]["status"] == "ready"
    backend.disconnect(OWNER, "drive")
    result = capacity.capacity_report(backend, OWNER)["drive"]
    assert result["status"] == "disconnected" and result["free_bytes"] is None
    assert len(api.requests) == 1
