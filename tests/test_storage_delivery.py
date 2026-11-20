"""Exercise durable delivery with simulated provider APIs; no external upload.

The cases cover uncertain remote outcomes, render replacement, authorization
boundaries and restarts, rather than only successful local helper calls.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

import httpx
import pytest

from integrations import Vault
import storage_delivery as delivery
from storage_delivery import DeliveryError, StorageDelivery


OWNER = "owner-a"
DRIVE_CONNECTION = {
    "account_id": "google-user-1", "folder_id": "folder-1",
    "access_token": "private-access-token", "refresh_token": "private-refresh-token",
    "expires_at": 10_000, "scope": delivery.DRIVE_SCOPE,
}
GH_CONNECTION = {
    "account_id": "7", "token": "github-private-token", "repository": "author/cuts",
    "repo_id": "55", "release_id": "3", "tag": "autoclips-test",
}


def artifact(root, *, data=b"validated media", quality="final", render_id="render-1"):
    folder = root / "videos" / "video-1"
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / (render_id + ".mp4")
    source.write_bytes(data)
    return {"video_id": "video-1", "clip_id": "clip-1", "kind": "clip", "quality": quality,
            "render_id": render_id, "path": str(source), "filename": "my-cut.mp4"}


def service(root, handler=None, *, clock=None):
    def unexpected(request):
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url.path}")
    transport = httpx.MockTransport(handler or unexpected)
    vault = Vault(root)
    return StorageDelivery(root, vault, clock=clock or (lambda: 100), client_factory=lambda: httpx.Client(
        transport=transport, follow_redirects=False, trust_env=False))


def connected(backend, provider="drive", owner=OWNER):
    backend._save_connection(owner, provider, dict(DRIVE_CONNECTION if provider == "drive" else GH_CONNECTION))


class DriveAPI:
    def __init__(self):
        self.requests = []
        self.metadata = {}
        self.content = b""
        self.complete = False
        self.session = "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=private-session"
        self.generated = self.started = self.probes = self.puts = self.folder_creates = 0
        self.timeout_after_chunk = False
        self.folder_exists = True
        self.location = self.session
        self.range_override = None
        self.checksum_missing = False
        self.before_session = None

    def __call__(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/token":
            assert request.url.host == "oauth2.googleapis.com"
            return httpx.Response(200, json={"access_token": "new-access-token", "expires_in": 3600})
        if path == "/drive/v3/files/generateIds":
            self.generated += 1
            return httpx.Response(200, json={"ids": ["file-1"]})
        if path == "/drive/v3/files" and method == "GET":
            assert "autoclips_owner" in request.url.params["q"]
            return httpx.Response(200, json={"files": [{"id": "folder-1", "name": "AutoClips"}] if self.folder_exists else []})
        if path == "/drive/v3/files" and method == "POST":
            self.folder_creates += 1
            folder = json.loads(request.content)
            assert folder["mimeType"] == "application/vnd.google-apps.folder"
            assert folder["appProperties"]["autoclips_role"] == "exports"
            self.folder_exists = True
            return httpx.Response(200, json={"id": "folder-1"})
        if path == "/drive/v3/files/folder-1":
            return httpx.Response(200, json={"id": "folder-1", "mimeType": "application/vnd.google-apps.folder",
                                              "capabilities": {"canAddChildren": True}})
        if path == "/drive/v3/files/file-1":
            if not self.complete:
                return httpx.Response(404, json={})
            result = {**self.metadata, "id": "file-1", "size": str(len(self.content)),
                      "sha256Checksum": hashlib.sha256(self.content).hexdigest()}
            if self.checksum_missing:
                result.pop("sha256Checksum")
            return httpx.Response(200, json=result)
        if path == "/upload/drive/v3/files" and method == "POST":
            self.started += 1
            if self.before_session:
                self.before_session()
            self.metadata = json.loads(request.content)
            assert self.metadata["id"] == "file-1"
            assert self.metadata["parents"] == ["folder-1"]
            return httpx.Response(200, headers={"Location": self.location})
        if path == "/upload/drive/v3/files" and method == "PUT":
            content_range = request.headers["Content-Range"]
            if content_range.startswith("bytes */"):
                self.probes += 1
                assert request.content == b""
                return httpx.Response(201 if self.complete else 308, headers=self.acknowledged())
            self.puts += 1
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
            start, end, total = map(int, match.groups())
            assert start == len(self.content)
            assert end == start + len(request.content) - 1
            self.content += request.content
            self.complete = len(self.content) == total
            if self.timeout_after_chunk:
                self.timeout_after_chunk = False
                raise httpx.ReadTimeout("secret provider response unavailable", request=request)
            return httpx.Response(201 if self.complete else 308, headers=self.acknowledged())
        raise AssertionError(f"Unexpected Drive request {method} {path}")

    def acknowledged(self):
        if self.range_override:
            return {"Range": self.range_override}
        return {"Range": f"bytes=0-{len(self.content) - 1}"} if self.content else {}


class GithubAPI:
    def __init__(self):
        self.requests = []
        self.private = True
        self.push = True
        self.draft = True
        self.has_release = True
        self.timeout_after_upload = False
        self.release_creates = self.uploads = 0
        self.assets = []
        self.upload_url = "https://uploads.github.com/repos/author/cuts/releases/3/assets{?name,label}"
        self.expected = None
        self.digest_override = None

    def release(self):
        return {"id": 3, "draft": self.draft, "tag_name": "autoclips-exports-" + datetime.now(timezone.utc).strftime("%Y-%m"),
                "upload_url": self.upload_url}

    def __call__(self, request):
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "author"})
        if path == "/repos/author/cuts":
            return httpx.Response(200, json={"id": 55, "private": self.private, "permissions": {"push": self.push},
                                              "default_branch": "main"})
        if path == "/repos/author/cuts/releases" and method == "GET":
            return httpx.Response(200, json=[self.release()] if self.has_release else [])
        if path == "/repos/author/cuts/releases" and method == "POST":
            self.release_creates += 1
            payload = json.loads(request.content)
            assert payload["draft"] is True
            assert payload["prerelease"] is False
            self.has_release = True
            return httpx.Response(201, json=self.release())
        if path == "/repos/author/cuts/releases/3":
            return httpx.Response(200, json=self.release())
        if path == "/repos/author/cuts/releases/3/assets" and method == "GET":
            return httpx.Response(200, json=self.assets)
        if path == "/repos/author/cuts/releases/3/assets" and method == "POST":
            assert request.url.host == "uploads.github.com"
            self.uploads += 1
            data = request.read()
            assert request.headers["Content-Length"] == str(len(data))
            if self.expected is not None:
                assert data == self.expected
            self.assets.append({"id": 44, "name": request.url.params["name"], "state": "uploaded", "size": len(data),
                                "digest": self.digest_override or "sha256:" + hashlib.sha256(data).hexdigest(),
                                "browser_download_url": "javascript:must-never-be-used"})
            if self.timeout_after_upload:
                self.timeout_after_upload = False
                raise httpx.ReadTimeout("uncertain result and secret details", request=request)
            return httpx.Response(201, json=self.assets[-1])
        raise AssertionError(f"Unexpected GitHub request {method} {path}")


def test_defaults_are_safe_and_configuration_is_per_owner(tmp_path):
    backend = service(tmp_path)
    assert backend.worker is None
    settings = backend.public_settings(OWNER)
    assert not settings["auto_download"] and not settings["auto_upload"]
    assert settings["qualities"] == ["final"]
    assert not settings["drive"]["connected"] and not settings["github"]["connected"]
    with pytest.raises(DeliveryError, match="Conecte"):
        backend.configure(OWNER, {"auto_upload": True})
    backend.configure(OWNER, {"auto_download": True})
    assert backend.public_settings(OWNER)["auto_download"]
    assert not backend.public_settings("owner-b")["auto_download"]
    assert backend.enqueue(OWNER, {}, automatic=True) is None


@pytest.mark.parametrize("patch", [{"auto_upload": "true"}, {"auto_download": 1}, {"provider": "http://other"},
                                  {"qualities": []}, {"qualities": ["other"]}, {"folder_id": "not-app-owned"}])
def test_invalid_configuration_cannot_enable_external_delivery(tmp_path, patch):
    backend = service(tmp_path)
    with pytest.raises(DeliveryError):
        backend.configure(OWNER, patch)
    assert not backend.public_settings(OWNER)["auto_upload"]


def test_automatic_quality_filter_and_manual_delivery(tmp_path):
    backend = service(tmp_path)
    connected(backend)
    backend.configure(OWNER, {"auto_upload": True})
    preview = artifact(tmp_path, quality="preview")
    assert backend.enqueue(OWNER, preview, automatic=True) is None
    job = backend.enqueue(OWNER, preview)
    assert job["status"] == "queued" and job["quality"] == "preview"
    backend.disconnect(OWNER, "drive")
    assert not backend.public_settings(OWNER)["auto_upload"]
    assert backend.enqueue(OWNER, preview, automatic=True) is None


def test_drive_connection_requires_scoped_offline_authorization(tmp_path):
    api = DriveAPI()
    backend = service(tmp_path, api)
    for token in ({"access_token": "access", "refresh_token": "refresh", "scope": "openid email"},
                  {"access_token": "access", "scope": delivery.DRIVE_SCOPE}):
        with pytest.raises(DeliveryError):
            backend.connect_drive(OWNER, token, {"sub": "user-a"})
    assert not api.requests
    assert not backend.public_settings(OWNER)["drive"]["connected"]
    connected(backend)
    with pytest.raises(DeliveryError, match="offline"):
        backend.connect_drive(OWNER, {"access_token": "access", "scope": delivery.DRIVE_SCOPE}, {"sub": "different-user"})


def test_drive_creates_app_owned_folder_and_encrypts_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_ID", "client.apps.googleusercontent.com")
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET", "environment-only-secret")
    api = DriveAPI()
    api.folder_exists = False
    backend = service(tmp_path, api)
    token = {"access_token": "private-access-token", "refresh_token": "private-refresh-token",
             "scope": "openid email " + delivery.DRIVE_SCOPE, "expires_in": 3600}
    result = backend.connect_drive(OWNER, token, {"sub": "google-user-1"})
    assert result["drive"]["connected"] and result["drive"]["configured"]
    assert result["drive"]["folder_url"] == "https://drive.google.com/drive/folders/folder-1"
    backend.connect_drive(OWNER, {key: value for key, value in token.items() if key != "refresh_token"}, {"sub": "google-user-1"})
    assert api.folder_creates == 1
    public = json.dumps(result)
    for secret in (token["access_token"], token["refresh_token"], "google-user-1"):
        assert secret not in public
        assert secret.encode() not in backend.vault.path.read_bytes()


@pytest.mark.parametrize("failure", ["public", "readonly", "published"])
def test_github_connect_rejects_unapproved_visibility_or_permissions(tmp_path, failure):
    api = GithubAPI()
    if failure == "public":
        api.private = False
    elif failure == "readonly":
        api.push = False
    else:
        api.draft = False
    backend = service(tmp_path, api)
    with pytest.raises(DeliveryError):
        backend.connect_github(OWNER, "github-private-token", "author/cuts")
    assert not backend.public_settings(OWNER)["github"]["connected"]
    assert api.uploads == api.release_creates == 0


def test_github_connect_creates_only_draft_in_private_repository(tmp_path):
    api = GithubAPI()
    api.has_release = False
    backend = service(tmp_path, api)
    result = backend.connect_github(OWNER, "github-private-token", "author/cuts")
    assert result["github"] == {"configured": True, "connected": True, "repository": "author/cuts", "private": True,
                                "url": "https://github.com/author/cuts"}
    backend.connect_github(OWNER, "github-private-token", "author/cuts")
    assert api.release_creates == 1
    assert "github-private-token" not in json.dumps(result)
    assert b"github-private-token" not in backend.vault.path.read_bytes()


def test_pin_survives_render_replacement_and_duplicate_enqueue(tmp_path):
    backend = service(tmp_path)
    connected(backend)
    render = artifact(tmp_path)
    job = backend.enqueue(OWNER, render)
    assert backend.enqueue(OWNER, render)["id"] == job["id"]
    original = Path(render["path"])
    pin = Path(backend.jobs[job["id"]]["path"])
    assert original.stat().st_ino == pin.stat().st_ino
    original.unlink()
    original.write_bytes(b"new preview replaced the old pathname")
    assert pin.read_bytes() == b"validated media"
    assert "path" not in job and "owner" not in job and "destination" not in job
    assert str(pin).encode() not in backend.jobs_path.read_bytes()
    assert backend.list_jobs("owner-b") == []
    with pytest.raises(DeliveryError, match="não encontrado"):
        backend.retry("owner-b", job["id"])


@pytest.mark.parametrize("kind", ["outside", "symlink", "parent-symlink", "empty"])
def test_untrusted_or_missing_artifacts_never_enter_outbox(tmp_path, kind):
    backend = service(tmp_path)
    connected(backend)
    render = artifact(tmp_path)
    if kind == "outside":
        render["path"] = "/etc/hosts"
    elif kind == "symlink":
        link = tmp_path / "link.mp4"
        link.symlink_to(render["path"])
        render["path"] = str(link)
    elif kind == "parent-symlink":
        link = tmp_path / "linked-folder"
        link.symlink_to(Path(render["path"]).parent, target_is_directory=True)
        render["path"] = str(link / Path(render["path"]).name)
    else:
        Path(render["path"]).write_bytes(b"")
    with pytest.raises(DeliveryError):
        backend.enqueue(OWNER, render)
    assert not backend.jobs and not list(backend.folder.glob("*.mp4"))


def test_drive_chunked_upload_persists_id_before_remote_creation_and_keeps_original(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "CHUNK", 256 * 1024)
    data = b"a" * (256 * 1024) + b"last chunk"
    api = DriveAPI()
    backend = service(tmp_path, api)
    connected(backend)
    render = artifact(tmp_path, data=data)
    job = backend.enqueue(OWNER, render)
    def check_persisted():
        persisted = json.loads(backend.vault.cipher.decrypt(backend.jobs_path.read_bytes()))
        assert persisted[job["id"]]["remote"]["file_id"] == "file-1"
    api.before_session = check_persisted
    assert backend.run_once()
    result = backend.list_jobs(OWNER)[0]
    assert result["status"] == "succeeded" and result["progress"] == 100
    assert result["url"] == "https://drive.google.com/file/d/file-1/view"
    assert api.content == data and api.puts == 2 and api.generated == api.started == 1
    assert Path(render["path"]).read_bytes() == data
    assert not Path(backend.jobs[job["id"]]["path"]).exists()
    assert not backend.run_once()


def test_drive_uncertain_partial_upload_resumes_after_restart_without_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "CHUNK", 256 * 1024)
    data = b"a" * (256 * 1024) + b"rest of recording"
    now = [100]
    api = DriveAPI()
    api.timeout_after_chunk = True
    backend = service(tmp_path, api, clock=lambda: now[0])
    connected(backend)
    render = artifact(tmp_path, data=data)
    job = backend.enqueue(OWNER, render)
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "retry_wait"
    assert b"private-session" not in backend.jobs_path.read_bytes()
    Path(render["path"]).unlink()  # A newer render cleans the original export.
    now[0] += 61
    resumed = service(tmp_path, api, clock=lambda: now[0])
    resumed.run_once()
    assert resumed.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert api.content == data
    assert api.generated == api.started == 1 and api.probes == 1 and api.puts == 2
    assert not Path(resumed.jobs[job["id"]]["path"]).exists()


def test_crashed_running_job_is_reconciled_after_reboot(tmp_path):
    api = GithubAPI()
    api.timeout_after_upload = True
    backend = service(tmp_path, api)
    connected(backend, "github")
    job = backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    backend.run_once()
    # Simulate process death between upload and writing the recovery state.
    backend.jobs[job["id"]]["status"] = "running"
    backend._save_jobs()
    resumed = service(tmp_path, api)
    assert resumed.list_jobs(OWNER)[0]["status"] == "queued"
    resumed.run_once()
    assert resumed.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert api.uploads == 1


@pytest.mark.parametrize("location", ["https://attacker.example/upload", "http://www.googleapis.com/upload/drive/v3/files",
                                     "https://www.googleapis.com:bad/upload/drive/v3/files",
                                     "https://www.googleapis.com/upload/drive/v3/filesevil"])
def test_drive_refuses_foreign_or_invalid_resumable_session(tmp_path, location):
    api = DriveAPI()
    api.location = location
    backend = service(tmp_path, api)
    connected(backend)
    backend.enqueue(OWNER, artifact(tmp_path))
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "failed"
    assert api.puts == 0
    assert all(r.url.host == "www.googleapis.com" for r in api.requests)


def test_drive_rejects_server_progress_beyond_sent_data(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "CHUNK", 256 * 1024)
    api = DriveAPI()
    api.range_override = "bytes=0-400000"
    backend = service(tmp_path, api)
    connected(backend)
    backend.enqueue(OWNER, artifact(tmp_path, data=b"v" * (512 * 1024)))
    backend.run_once()
    job = backend.list_jobs(OWNER)[0]
    assert job["status"] == "failed" and "progresso inválido" in job["error"]
    assert api.puts == 1


def test_drive_checksum_not_yet_available_is_rechecked_without_reupload(tmp_path):
    now = [100]
    api = DriveAPI()
    api.checksum_missing = True
    backend = service(tmp_path, api, clock=lambda: now[0])
    connected(backend)
    backend.enqueue(OWNER, artifact(tmp_path))
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "retry_wait"
    api.checksum_missing = False
    now[0] += 61
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert api.puts == api.started == 1


def test_expired_drive_access_token_is_refreshed_offline(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("AUTOCLIPS_GOOGLE_CLIENT_SECRET", "server-secret")
    api = DriveAPI()
    backend = service(tmp_path, api)
    backend._save_connection(OWNER, "drive", {**DRIVE_CONNECTION, "expires_at": 1})
    backend.enqueue(OWNER, artifact(tmp_path))
    backend.run_once()
    token_calls = [r for r in api.requests if r.url.path == "/token"]
    assert len(token_calls) == 1
    assert b"grant_type=refresh_token" in token_calls[0].content
    assert backend._owner_data(OWNER)["drive"]["access_token"] == "new-access-token"
    assert all(r.headers.get("Authorization") == "Bearer new-access-token" for r in api.requests if r.url.path != "/token")


@pytest.mark.parametrize("code", [401, 429])
def test_auth_failure_stops_and_rate_limits_retry_with_bounded_backoff(tmp_path, code):
    calls = []
    now = [100]
    def fail(request):
        calls.append(request)
        return httpx.Response(code, headers={"Retry-After": "120"}, json={"secret": "provider-private-details"})
    backend = service(tmp_path, fail, clock=lambda: now[0])
    connected(backend)
    render = artifact(tmp_path)
    job = backend.enqueue(OWNER, render)
    backend.run_once()
    result = backend.list_jobs(OWNER)[0]
    assert result["status"] == ("failed" if code == 401 else "retry_wait")
    assert "provider-private-details" not in result["error"]
    assert "private-access-token" not in result["error"]
    assert not backend.run_once()
    if code == 429:
        now[0] += 119
        assert not backend.run_once()
        for _ in range(2):
            now[0] += 121
            backend.run_once()
        assert backend.list_jobs(OWNER)[0]["status"] == "failed"
        assert len(calls) == 3
    assert Path(backend.jobs[job["id"]]["path"]).exists()
    assert Path(render["path"]).exists()
    assert backend.retry(OWNER, job["id"])["status"] == "queued"


def test_github_upload_streams_exact_pinned_bytes_and_verifies_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "CHUNK", 256 * 1024)
    api = GithubAPI()
    api.expected = b"v" * (256 * 1024) + b"end"
    backend = service(tmp_path, api)
    connected(backend, "github")
    render = artifact(tmp_path, data=api.expected)
    job = backend.enqueue(OWNER, render, provider="github")
    backend.run_once()
    result = backend.list_jobs(OWNER)[0]
    assert result["status"] == "succeeded" and result["bytes_sent"] == len(api.expected)
    assert result["url"] == "https://github.com/author/cuts/releases"
    assert api.uploads == 1
    assert hashlib.sha256(api.expected).hexdigest() in api.assets[0]["name"]
    assert Path(render["path"]).exists()
    assert not Path(backend.jobs[job["id"]]["path"]).exists()


@pytest.mark.parametrize("changed", ["public-repo", "published-release", "foreign-upload-url", "digest-mismatch"])
def test_github_checks_current_visibility_and_integrity_before_success(tmp_path, changed):
    api = GithubAPI()
    backend = service(tmp_path, api)
    connected(backend, "github")
    job = backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    if changed == "public-repo":
        api.private = False
    elif changed == "published-release":
        api.draft = False
    elif changed == "foreign-upload-url":
        api.upload_url = "https://attacker.example/assets"
    else:
        api.digest_override = "sha256:wrong"
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "failed"
    assert api.uploads == (1 if changed == "digest-mismatch" else 0)
    assert Path(backend.jobs[job["id"]]["path"]).exists()


@pytest.mark.parametrize("state,digest", [("starter", None), ("uploaded", None), ("uploaded", "sha256:different")])
def test_github_unknown_existing_asset_is_never_overwritten_or_duplicated(tmp_path, state, digest):
    api = GithubAPI()
    backend = service(tmp_path, api)
    connected(backend, "github")
    job = backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    private = backend.jobs[job["id"]]
    api.assets.append({"id": 44, "name": private["asset_name"], "state": state, "size": private["total_bytes"], "digest": digest})
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "failed"
    assert api.uploads == 0
    assert all(r.method == "GET" for r in api.requests)


@pytest.mark.parametrize("provider", ["drive", "github"])
def test_switching_account_cannot_redirect_queued_or_retried_delivery(tmp_path, provider):
    backend = service(tmp_path)
    connected(backend, provider)
    job = backend.enqueue(OWNER, artifact(tmp_path), provider=provider)
    original = dict(DRIVE_CONNECTION if provider == "drive" else GH_CONNECTION)
    backend._save_connection(OWNER, provider, {**original, "account_id": "different-user"})
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "failed"
    with pytest.raises(DeliveryError, match="destino"):
        backend.retry(OWNER, job["id"])
    backend._save_connection(OWNER, provider, original)
    assert backend.retry(OWNER, job["id"])["status"] == "queued"


def test_in_place_file_change_stops_before_any_external_request(tmp_path):
    backend = service(tmp_path)
    connected(backend)
    render = artifact(tmp_path)
    backend.enqueue(OWNER, render)
    Path(render["path"]).write_bytes(b"modified render")
    backend.run_once()
    job = backend.list_jobs(OWNER)[0]
    assert job["status"] == "failed" and "íntegro" in job["error"]


def test_github_rejects_two_gib_before_hashing_or_pinning(tmp_path):
    backend = service(tmp_path)
    connected(backend, "github")
    render = artifact(tmp_path)
    with Path(render["path"]).open("wb") as stream:
        stream.truncate(2 * 1024 ** 3)  # Sparse file: does not allocate two GiB.
    with pytest.raises(DeliveryError, match="2 GiB"):
        backend.enqueue(OWNER, render, provider="github")
    assert not backend.jobs


def test_confirmed_remote_success_survives_pin_cleanup_failure(tmp_path, monkeypatch):
    api = GithubAPI()
    backend = service(tmp_path, api)
    connected(backend, "github")
    job = backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    pin = Path(backend.jobs[job["id"]]["path"])
    real_unlink = Path.unlink
    def fail_pin(path, *args, **kwargs):
        if path == pin:
            raise OSError("cleanup unavailable")
        return real_unlink(path, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_pin)
        backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert pin.exists()
    resumed = service(tmp_path, api)
    assert resumed.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert not pin.exists() and api.uploads == 1


def test_unreadable_outbox_fails_closed_without_losing_pending_files(tmp_path):
    backend = service(tmp_path)
    connected(backend)
    backend.enqueue(OWNER, artifact(tmp_path))
    backend.jobs_path.write_bytes(b"corrupt or wrong encryption key")
    with pytest.raises(DeliveryError, match="fila"):
        service(tmp_path)
    assert len(list(backend.folder.glob("*.mp4"))) == 1


@pytest.mark.parametrize("status", ["queued", "retry_wait", "failed"])
def test_cancel_releases_only_its_pin_and_allows_new_explicit_enqueue(tmp_path, status):
    backend = service(tmp_path)
    connected(backend)
    render = artifact(tmp_path)
    job = backend.enqueue(OWNER, render)
    backend._update(backend.jobs[job["id"]], status=status)
    pin = Path(backend.jobs[job["id"]]["path"])
    unrelated = backend.folder / "keep.txt"
    unrelated.write_text("unrelated")
    assert backend.cancel(OWNER, job["id"])["status"] == "cancelled"
    assert not pin.exists()
    assert Path(render["path"]).read_bytes() == b"validated media"
    assert unrelated.read_text() == "unrelated"
    assert backend.cancel(OWNER, job["id"])["status"] == "cancelled"
    with pytest.raises(DeliveryError, match="cancelado"):
        backend.retry(OWNER, job["id"])
    replacement = backend.enqueue(OWNER, render)
    assert replacement["id"] != job["id"] and replacement["status"] == "queued"
    assert len(service(tmp_path).list_jobs(OWNER)) == 2


def test_cancel_checks_owner_and_does_not_interrupt_inflight_upload(tmp_path):
    backend = service(tmp_path)
    connected(backend)
    job = backend.enqueue(OWNER, artifact(tmp_path))
    with pytest.raises(DeliveryError, match="não encontrado"):
        backend.cancel("owner-b", job["id"])
    backend._update(backend.jobs[job["id"]], status="running")
    with pytest.raises(DeliveryError, match="Aguarde"):
        backend.cancel(OWNER, job["id"])
    assert backend.list_jobs(OWNER)[0]["status"] == "running"
    assert Path(backend.jobs[job["id"]]["path"]).exists()


def test_cancel_after_uncertain_outcome_reconciles_same_remote_id_on_new_enqueue(tmp_path):
    api = DriveAPI()
    api.timeout_after_chunk = True
    backend = service(tmp_path, api)
    connected(backend)
    render = artifact(tmp_path)
    first = backend.enqueue(OWNER, render)
    backend.run_once()
    assert api.complete and backend.list_jobs(OWNER)[0]["status"] == "retry_wait"
    backend.cancel(OWNER, first["id"])
    second = backend.enqueue(OWNER, render)
    assert second["id"] != first["id"]
    backend.run_once()
    assert backend.jobs[second["id"]]["status"] == "succeeded"
    assert api.puts == api.started == api.generated == 1
    assert backend.cancel(OWNER, second["id"])["status"] == "succeeded"


def test_failure_to_persist_cancellation_preserves_state_and_pin(tmp_path, monkeypatch):
    backend = service(tmp_path)
    connected(backend)
    job = backend.enqueue(OWNER, artifact(tmp_path))
    def no_space():
        raise OSError(28, "No space left on device, /private/path")
    with monkeypatch.context() as patch:
        patch.setattr(backend, "_save_jobs", no_space)
        with pytest.raises(DeliveryError, match="espaço"):
            backend.cancel(OWNER, job["id"])
    assert backend.list_jobs(OWNER)[0]["status"] == "queued"
    assert Path(backend.jobs[job["id"]]["path"]).exists()
    assert service(tmp_path).list_jobs(OWNER)[0]["status"] == "queued"


def test_disk_full_before_claim_cannot_upload_and_worker_can_recover(tmp_path, monkeypatch):
    api = GithubAPI()
    backend = service(tmp_path, api)
    connected(backend, "github")
    backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    def no_space():
        raise OSError(28, "No space left on device")
    with monkeypatch.context() as patch:
        patch.setattr(backend, "_save_jobs", no_space)
        assert backend.run_once() is False
    assert backend.list_jobs(OWNER)[0]["status"] == "queued"
    assert backend.list_jobs(OWNER)[0]["attempts"] == 0
    assert not api.requests
    assert backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "succeeded"


def test_disk_full_after_upload_keeps_pin_and_reconciles_when_storage_recovers(tmp_path, monkeypatch):
    now = [100]
    api = GithubAPI()
    backend = service(tmp_path, api, clock=lambda: now[0])
    connected(backend, "github")
    job = backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    real_save = backend._save_jobs
    def disk_full_after_remote_acceptance():
        if api.assets:
            raise OSError(28, "No space left on device")
        real_save()
    with monkeypatch.context() as patch:
        patch.setattr(backend, "_save_jobs", disk_full_after_remote_acceptance)
        assert backend.run_once()
    assert api.uploads == 1
    assert backend.list_jobs(OWNER)[0]["status"] == "retry_wait"
    assert Path(backend.jobs[job["id"]]["path"]).exists()
    # On disk the attempt remains running, so both a restart and this live
    # worker can resume by reconciling the already accepted remote asset.
    saved = json.loads(backend.vault.cipher.decrypt(backend.jobs_path.read_bytes()))
    assert saved[job["id"]]["status"] == "running"
    now[0] += 61
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert api.uploads == 1 and not Path(backend.jobs[job["id"]]["path"]).exists()


def test_drive_expired_session_reuses_reserved_file_id(tmp_path):
    api = DriveAPI()
    now = [100]
    backend = service(tmp_path, api, clock=lambda: now[0])
    connected(backend)
    job = backend.enqueue(OWNER, artifact(tmp_path))
    backend._update(backend.jobs[job["id"]], remote={"file_id": "file-1", "session_url": api.session + "expired"})
    real_handler = api.__call__
    def expired(request):
        if request.method == "PUT" and str(request.url).endswith("expired"):
            return httpx.Response(404)
        return real_handler(request)
    backend._client_factory = lambda: httpx.Client(transport=httpx.MockTransport(expired), follow_redirects=False)
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "succeeded"
    assert api.generated == 0 and api.started == 1
    assert api.metadata["id"] == "file-1"


def test_github_api_redirect_never_receives_credentials(tmp_path):
    hosts = []
    def redirect(request):
        hosts.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://attacker.example/collect"})
    backend = service(tmp_path, redirect)
    connected(backend, "github")
    backend.enqueue(OWNER, artifact(tmp_path), provider="github")
    backend.run_once()
    assert backend.list_jobs(OWNER)[0]["status"] == "failed"
    assert hosts == ["api.github.com"]
