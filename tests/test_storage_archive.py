"""Verify storage release, remote reads and recovery without provider accounts."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from integrations import Vault
import storage_archive
from storage_archive import RESTORE_RESERVE, StorageArchive, local_response
from storage_delivery import DeliveryError, StorageDelivery


OWNER = "archive-owner"
CONTENT = b"0123456789-finished-video-data"
CONNECTION = {"account_id": "account-1", "folder_id": "folder-1", "access_token": "sample-access",
              "refresh_token": "sample-refresh", "expires_at": 10000}


class Body(httpx.SyncByteStream):
    def __init__(self, content):
        self.content = content
        self.closed = False

    def __iter__(self):
        yield self.content

    def close(self):
        self.closed = True


class Drive:
    def __init__(self):
        self.job = None
        self.content = CONTENT
        self.media_content = None
        self.metadata_code = 200
        self.media_code = None
        self.can_download = True
        self.checksum = None
        self.parent = "folder-1"
        self.trashed = False
        self.after_metadata = None
        self.requests = []
        self.streams = []
        self.bad_range = False
        self.bad_length = False
        self.fail_read = False

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.url.path == "/drive/v3/files/file-1"
        assert request.headers["Authorization"] == "Bearer sample-access"
        if request.url.params.get("alt") != "media":
            payload = {"id": "file-1", "size": str(len(self.content)), "trashed": self.trashed,
                       "sha256Checksum": self.checksum or hashlib.sha256(self.content).hexdigest(),
                       "parents": [self.parent], "appProperties": {"autoclips_job": self.job["dedupe"]},
                       "capabilities": {"canDownload": self.can_download}}
            if self.after_metadata:
                self.after_metadata()
            return httpx.Response(self.metadata_code, json=payload)
        if self.fail_read:
            raise httpx.ReadTimeout("private-provider-body", request=request)
        data = self.media_content if self.media_content is not None else self.content
        headers = {"X-Provider-Secret": "do-not-forward"}
        status = 200
        if "Range" in request.headers:
            first, last = map(int, request.headers["Range"].split("=", 1)[1].split("-"))
            data = data[first:last + 1]
            headers["Content-Range"] = f"bytes {first}-{last}/{len(self.content)}"
            if self.bad_range:
                headers["Content-Range"] = "bytes 0-1/999"
            status = 206
        headers["Content-Length"] = str(len(data) + (1 if self.bad_length else 0))
        body = Body(data)
        self.streams.append(body)
        return httpx.Response(self.media_code or status, stream=body, headers=headers)


@pytest.fixture
def setup(tmp_path):
    drive = Drive()
    backend = StorageDelivery(tmp_path, Vault(tmp_path), clock=lambda: 100,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(drive), trust_env=False))
    backend._save_connection(OWNER, "drive", dict(CONNECTION))
    backend.configure(OWNER, {"auto_upload": True, "archive_after_upload": True})
    folder = tmp_path / "renders"
    folder.mkdir()
    source = folder / "final.mp4"
    source.write_bytes(CONTENT)
    item = {"video_id": "video-1", "clip_id": "clip_01", "kind": "clip", "quality": "final",
            "render_id": "render-1", "path": str(source), "filename": "video-final.mp4"}
    public = backend.enqueue(OWNER, item)
    job = backend.jobs[public["id"]]
    backend._update(job, status="succeeded", remote={"file_id": "file-1"}, remote_id="file-1",
                    source_path=str(source), archive_requested=True, url="https://drive.google.com/file/d/file-1/view")
    backend._release_pin(job)
    drive.job = job
    archive = StorageArchive(backend)
    return backend, archive, drive, job, source


def media_client(archive, job):
    app = FastAPI()

    @app.get("/media")
    def media(request: Request, download: bool = False):
        try:
            return archive.response(job, request.headers.get("range"), download=download)
        except DeliveryError as error:
            raise HTTPException(422, str(error)) from None

    return TestClient(app)


def test_verified_reference_is_durable_before_local_file_is_removed(setup, monkeypatch):
    backend, archive, drive, job, source = setup
    unlink = Path.unlink
    saw_durable_reference = []

    def check_unlink(path, *args, **kwargs):
        if path == source:
            saved = json.loads(backend.vault.cipher.decrypt(backend.jobs_path.read_bytes()))[job["id"]]
            saw_durable_reference.append(saved)
            assert saved["archive_state"] == "verified"
            assert saved["status"] == "succeeded"
            assert saved["remote"]["file_id"] == "file-1"
            assert saved["sha256"] == hashlib.sha256(CONTENT).hexdigest()
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", check_unlink)
    assert archive.archive(job) is True
    assert len(saw_durable_reference) == 1
    assert not source.exists()
    assert job["archived"] is True and job["archive_error"] is None
    assert job["archived_bytes"] == len(CONTENT)
    assert archive.found(OWNER, "video-1", "clip", "final", "render-1", "clip_01")["id"] == job["id"]
    assert archive.archive(job) is True  # An idempotent worker retry.
    assert len(drive.requests) == 1


@pytest.mark.parametrize("change", ["checksum", "parent", "permission", "trashed", "missing"])
def test_failed_remote_verification_preserves_local_and_upload_success(setup, change):
    _backend, archive, drive, job, source = setup
    if change == "checksum":
        drive.checksum = "0" * 64
    elif change == "parent":
        drive.parent = "different-folder"
    elif change == "permission":
        drive.can_download = False
    elif change == "trashed":
        drive.trashed = True
    else:
        drive.metadata_code = 404
    assert archive.archive(job) is False
    assert source.read_bytes() == CONTENT
    assert job["status"] == "succeeded"
    assert job["archive_error"]
    assert not job.get("archived")


def test_reference_persistence_failure_never_releases_original(setup, monkeypatch):
    backend, archive, _drive, job, source = setup
    monkeypatch.setattr(backend, "_save_jobs", lambda: (_ for _ in ()).throw(OSError("full")))
    assert archive.archive(job) is False
    assert source.read_bytes() == CONTENT
    assert job["status"] == "succeeded"
    assert job.get("archive_state") != "verified"


def test_restart_after_unlink_recovers_durable_reference(setup, monkeypatch):
    backend, archive, drive, job, source = setup
    save = backend._save_jobs

    def fail_final_accounting():
        if job.get("archive_state") == "archived":
            raise OSError("full")
        save()

    monkeypatch.setattr(backend, "_save_jobs", fail_final_accounting)
    assert archive.archive(job) is False
    assert not source.exists()
    assert job["archive_state"] == "verified"
    restarted = StorageDelivery(backend.root, backend.vault, client_factory=backend._client_factory, clock=lambda: 100)
    archive = StorageArchive(restarted)
    recovered = archive.found(OWNER, "video-1", "clip", "final", "render-1", "clip_01")
    assert recovered is not None
    drive.job = restarted.jobs[job["id"]]
    assert media_client(archive, recovered).get("/media").content == CONTENT
    assert archive.archive(recovered) is True
    assert restarted.jobs[job["id"]]["archived"] is True


def test_replaced_export_is_never_removed(setup):
    _backend, archive, _drive, job, source = setup
    source.write_bytes(b"a newer export")
    assert archive.archive(job) is False
    assert source.read_bytes() == b"a newer export"
    assert "substituída" in job["archive_error"]


def test_guard_prevents_archiving_a_video_in_use(setup):
    backend, _archive, _drive, job, source = setup

    @contextmanager
    def busy(_job):
        yield False

    assert StorageArchive(backend, guard=busy).archive(job) is False
    assert source.read_bytes() == CONTENT
    assert job.get("archive_state") is None


@pytest.mark.parametrize("change", ["disconnect", "switch", "disable"])
def test_connection_or_preference_change_during_verification_preserves_source(setup, change):
    backend, archive, drive, job, source = setup

    def modify():
        if change == "disconnect":
            backend.disconnect(OWNER, "drive")
        elif change == "switch":
            backend._save_connection(OWNER, "drive", {**CONNECTION, "account_id": "account-2"})
        else:
            backend.configure(OWNER, {"archive_after_upload": False})

    drive.after_metadata = modify
    assert archive.archive(job) is False
    assert source.read_bytes() == CONTENT
    assert job["status"] == "succeeded"


@pytest.mark.parametrize("change", ["source", "preview", "unrequested", "outside", "symlink"])
def test_archive_scope_cannot_remove_nonexport_or_unapproved_files(setup, tmp_path, change):
    backend, archive, _drive, job, source = setup
    if change == "source":
        job["kind"] = "source"
    elif change == "preview":
        job["quality"] = "preview"
    elif change == "unrequested":
        job["archive_requested"] = False
    elif change == "outside":
        job["source_path"] = str(tmp_path.parent / "outside.mp4")
    else:
        alias = tmp_path / "linked.mp4"
        alias.symlink_to(source)
        job["source_path"] = str(alias)
    assert archive.archive(job) is False
    assert source.read_bytes() == CONTENT
    assert not job.get("archived")


def test_found_requires_exact_owner_and_artifact_identity(setup):
    _backend, archive, _drive, job, _source = setup
    assert archive.found(OWNER, "video-1", "clip", "final", "render-1", "clip_01") is None
    assert archive.archive(job)
    correct = [OWNER, "video-1", "clip", "final", "render-1", "clip_01"]
    for index in range(len(correct)):
        wrong = correct.copy()
        wrong[index] = "other"
        assert archive.found(*wrong) is None
    assert archive.found(None, *correct[1:]) is None
    assert archive.found(*correct)["id"] == job["id"]


@pytest.mark.parametrize("header,expected,status,content_range", [
    (None, CONTENT, 200, None),
    ("bytes=2-7", CONTENT[2:8], 206, f"bytes 2-7/{len(CONTENT)}"),
    ("bytes=5-", CONTENT[5:], 206, f"bytes 5-{len(CONTENT)-1}/{len(CONTENT)}"),
    ("bytes=-4", CONTENT[-4:], 206, f"bytes {len(CONTENT)-4}-{len(CONTENT)-1}/{len(CONTENT)}"),
    ("bytes=0-999", CONTENT, 206, f"bytes 0-{len(CONTENT)-1}/{len(CONTENT)}"),
])
def test_archived_download_and_partial_preview_stream_without_disk_copy(setup, header, expected, status, content_range):
    backend, archive, drive, job, source = setup
    assert archive.archive(job)
    before_files = sorted(p.relative_to(backend.root).as_posix() for p in backend.root.rglob("*") if p.is_file())
    response = media_client(archive, job).get("/media?download=true", headers={"Range": header} if header else {})
    assert response.status_code == status, response.text
    assert response.content == expected
    assert response.headers.get("Content-Range") == content_range
    assert response.headers["Content-Length"] == str(len(expected))
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response.headers["Cache-Control"]
    assert response.headers["Accept-Ranges"] == "bytes"
    assert "X-Provider-Secret" not in response.headers
    assert "sample-access" not in str(response.headers)
    assert drive.streams[-1].closed
    assert not source.exists()
    assert sorted(p.relative_to(backend.root).as_posix() for p in backend.root.rglob("*") if p.is_file()) == before_files


@pytest.mark.parametrize("header", ["bytes=999-", "bytes=-0", "bytes=6-3", "bytes=", "items=0-1", "bytes=0-1,3-4", "bytes=-"])
def test_invalid_ranges_return_416_before_provider_download(setup, header):
    _backend, archive, drive, job, _source = setup
    assert archive.archive(job)
    before = len(drive.requests)
    response = media_client(archive, job).get("/media", headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["Content-Range"] == f"bytes */{len(CONTENT)}"
    assert len(drive.requests) == before


@pytest.mark.parametrize("failure", ["missing", "wrong_checksum", "permission", "redirect", "bad_range", "bad_length", "read_error"])
def test_remote_access_errors_are_safe_and_keep_archive_reference(setup, failure):
    _backend, archive, drive, job, _source = setup
    assert archive.archive(job)
    if failure == "missing":
        drive.metadata_code = 404
    elif failure == "wrong_checksum":
        drive.checksum = "1" * 64
    elif failure == "permission":
        drive.can_download = False
    elif failure == "redirect":
        drive.media_code = 302
    elif failure == "bad_range":
        drive.bad_range = True
    elif failure == "bad_length":
        drive.bad_length = True
    else:
        drive.fail_read = True
    response = media_client(archive, job).get("/media", headers={"Range": "bytes=0-4"})
    assert response.status_code == 422
    assert "private-provider-body" not in response.text and "sample-access" not in response.text
    assert job["status"] == "succeeded" and job["archived"]
    assert all(stream.closed for stream in drive.streams)


def test_disconnect_keeps_archive_but_requires_same_account_to_download(setup):
    backend, archive, _drive, job, _source = setup
    assert archive.archive(job)
    backend.disconnect(OWNER, "drive")
    assert archive.found(OWNER, "video-1", "clip", "final", "render-1", "clip_01")
    assert media_client(archive, job).get("/media").status_code == 422
    backend._save_connection(OWNER, "drive", {**CONNECTION, "account_id": "other-account"})
    assert media_client(archive, job).get("/media").status_code == 422
    backend._save_connection(OWNER, "drive", dict(CONNECTION))
    assert media_client(archive, job).get("/media").content == CONTENT


def test_temporary_restore_checks_bytes_and_cleans_after_publication(setup):
    backend, archive, drive, job, source = setup
    assert archive.archive(job)
    with archive.temporary_local(job) as restored:
        assert restored.is_relative_to(backend.folder)
        assert restored.suffix == ".mp4"
        assert restored.read_bytes() == CONTENT
        assert not source.exists()
        assert drive.streams[-1].closed
    assert not restored.exists()
    assert not list(backend.folder.glob("restore-*"))


def test_temporary_restore_cleans_when_publisher_fails(setup):
    backend, archive, _drive, job, _source = setup
    assert archive.archive(job)
    with pytest.raises(RuntimeError, match="publisher"):
        with archive.temporary_local(job) as restored:
            assert restored.exists()
            raise RuntimeError("publisher")
    assert not list(backend.folder.glob("restore-*"))


def test_temporary_restore_refuses_insufficient_workspace_before_download(setup, monkeypatch):
    _backend, archive, drive, job, _source = setup
    assert archive.archive(job)
    monkeypatch.setattr(storage_archive.shutil, "disk_usage", lambda _: SimpleNamespace(free=len(CONTENT) + RESTORE_RESERVE - 1))
    before = len(drive.requests)
    with pytest.raises(DeliveryError, match="espaço de edição"):
        with archive.temporary_local(job):
            pytest.fail("A file must not be supplied without enough workspace")
    assert len(drive.requests) == before


def test_temporary_restore_rejects_changed_content_and_cleans_partial(setup):
    backend, archive, drive, job, _source = setup
    assert archive.archive(job)
    drive.media_content = b"x" * len(CONTENT)
    with pytest.raises(DeliveryError, match="integridade"):
        with archive.temporary_local(job):
            pytest.fail("Unverified restored bytes must never reach the publisher")
    assert not list(backend.folder.glob("restore-*"))
    assert job["archived"] is True


def test_local_download_survives_unlink_after_response_opens_descriptor(tmp_path):
    path = tmp_path / "export.mp4"
    path.write_bytes(CONTENT)
    app = FastAPI()

    @app.get("/media")
    def media(request: Request):
        response = local_response(path, request.headers.get("range"), download=True, filename="clip.mp4")
        path.unlink()
        return response

    response = TestClient(app).get("/media", headers={"Range": "bytes=3-7"})
    assert response.status_code == 206
    assert response.content == CONTENT[3:8]
    assert response.headers["Content-Range"] == f"bytes 3-7/{len(CONTENT)}"
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert not path.exists()


def test_startup_cleanup_removes_only_interrupted_restore_files(setup):
    backend, archive, _drive, job, source = setup
    for name in ("restore-abc123.part", "restore-xyz_456.mp4"):
        (backend.folder / name).write_bytes(b"interrupted restoration")
    other = backend.folder / "important.mp4"
    other.write_bytes(b"keep this")
    pin = backend.folder / (job["id"] + ".mp4")
    pin.write_bytes(CONTENT)
    symlink = backend.folder / "restore-linked.mp4"
    symlink.symlink_to(source)
    assert archive.cleanup_interrupted_restores() == 2
    assert source.read_bytes() == CONTENT and other.exists() and pin.exists()
    assert backend.jobs_path.exists() and symlink.is_symlink()
    assert archive.cleanup_interrupted_restores() == 0


def test_publisher_io_error_is_not_misreported_as_restore_error(setup):
    backend, archive, _drive, job, _source = setup
    assert archive.archive(job)
    with pytest.raises(OSError, match="publisher IO"):
        with archive.temporary_local(job):
            raise OSError("publisher IO")
    assert not list(backend.folder.glob("restore-*"))


def test_another_delivery_pin_defers_archival_until_its_upload_finishes(setup):
    backend, archive, _drive, job, source = setup
    another_pin = backend.folder / "another-delivery.mp4"
    os.link(source, another_pin)
    assert archive.archive(job) is False
    assert source.read_bytes() == CONTENT and another_pin.exists()
    assert "reservada para outro envio" in job["archive_error"]
    assert not job.get("archived")
    another_pin.unlink()
    assert archive.archive(job) is True
    assert job["archive_error"] is None
    assert not source.exists()
