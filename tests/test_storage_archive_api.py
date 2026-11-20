"""Exercise storage optimization through the studio's authenticated API.

Signed test sessions and a simulated Drive replace real accounts. Upload,
verification, eviction and subsequent media reads use the production paths.
"""
import hashlib
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import server
import studio_auth
from integrations import Vault
from storage_delivery import StorageDelivery


VID = "e" * 32
EMAIL = "archive-owner@example.test"
OTHER_EMAIL = "other-owner@example.test"
OWNER = studio_auth.storage_owner(EMAIL)
ORIGIN = "https://archive-studio.example.test"
CONTENT = b"0123456789-completed-final-video"
ACCESS = "test-archive-access-private"
REFRESH = "test-archive-refresh-private"


class MediaBody(httpx.SyncByteStream):
    def __init__(self, value):
        self.value = value

    def __iter__(self):
        yield self.value


class DriveAPI:
    def __init__(self):
        self.requests = []
        self.files = {}
        self.reserved = 0
        self.quota = {"limit": "10000", "usage": "1000", "usageInDrive": "100"}
        self.quota_error = None

    def __call__(self, request):
        self.requests.append(request)
        assert request.url.host == "www.googleapis.com"
        assert request.headers["Authorization"] == "Bearer " + ACCESS
        path = request.url.path
        if path == "/drive/v3/about":
            assert request.method == "GET"
            assert request.url.params["fields"] == "storageQuota,maxUploadSize"
            if self.quota_error:
                return httpx.Response(self.quota_error, json={"error": "provider-private-error"})
            return httpx.Response(200, json={"storageQuota": self.quota, "maxUploadSize": "100000"})
        if path == "/drive/v3/files/generateIds":
            self.reserved += 1
            return httpx.Response(200, json={"ids": [f"remote-{self.reserved}"]})
        if path == "/drive/v3/files/archive-folder":
            return httpx.Response(200, json={"id": "archive-folder", "mimeType": "application/vnd.google-apps.folder",
                                            "capabilities": {"canAddChildren": True}})
        if path == "/upload/drive/v3/files" and request.method == "POST":
            metadata = json.loads(request.content)
            self.files[metadata["id"]] = {"metadata": metadata, "body": b"", "complete": False}
            return httpx.Response(200, headers={"Location":
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&upload_id=" + metadata["id"]})
        if path == "/upload/drive/v3/files" and request.method == "PUT":
            item = self.files[request.url.params["upload_id"]]
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers["Content-Range"])
            assert match, "This small fixture is transferred in one complete chunk"
            start, end, total = map(int, match.groups())
            assert start == len(item["body"]) and end - start + 1 == len(request.content)
            item["body"] += request.content
            item["complete"] = len(item["body"]) == total
            assert item["complete"]
            return httpx.Response(200, json={"id": request.url.params["upload_id"]})
        if path.startswith("/drive/v3/files/remote-"):
            remote_id = path.rsplit("/", 1)[-1]
            item = self.files.get(remote_id)
            if not item or not item["complete"]:
                return httpx.Response(404, json={})
            data = item["body"]
            if request.url.params.get("alt") == "media":
                status = 200
                headers = {"X-Provider-Secret": "do-not-forward"}
                if "Range" in request.headers:
                    first, last = map(int, request.headers["Range"].split("=", 1)[1].split("-"))
                    headers["Content-Range"] = f"bytes {first}-{last}/{len(data)}"
                    data = data[first:last + 1]
                    status = 206
                headers["Content-Length"] = str(len(data))
                return httpx.Response(status, headers=headers, stream=MediaBody(data))
            return httpx.Response(200, json={**item["metadata"], "id": remote_id,
                "size": str(len(data)), "sha256Checksum": hashlib.sha256(data).hexdigest(),
                "trashed": False, "capabilities": {"canDownload": True}})
        raise AssertionError(f"Unexpected simulated Drive endpoint: {request.method} {path}")


def login(context, email=EMAIL):
    context.client.cookies.clear()
    context.client.cookies.set(studio_auth.SESSION_COOKIE,
        studio_auth.serializer("studio-session").dumps({"email": email}),
        domain="archive-studio.example.test", path="/")


def connect(context):
    context.backend._save_connection(OWNER, "drive", {
        "account_id": "archive-account", "folder_id": "archive-folder",
        "access_token": ACCESS, "refresh_token": REFRESH, "expires_at": 10000,
        "scope": studio_auth.DRIVE_SCOPE,
    })


def edit_manifest(context, update):
    path = context.work / "manifest.json"
    document = json.loads(path.read_text())
    update(document)
    server.write_json(path, document)


@pytest.fixture
def studio(tmp_path, monkeypatch):
    for key, value in {
        "AUTOCLIPS_GOOGLE_CLIENT_ID": "test-studio.apps.googleusercontent.com",
        "AUTOCLIPS_GOOGLE_CLIENT_SECRET": "test-client-secret-private",
        "AUTOCLIPS_SESSION_SECRET": "test-storage-session-" * 4,
        "AUTOCLIPS_ALLOWED_EMAILS": f"{EMAIL},{OTHER_EMAIL}",
        "AUTOCLIPS_PUBLIC_URL": ORIGIN,
    }.items():
        monkeypatch.setenv(key, value)
    for name, value in {"ROOT": tmp_path, "INDEX": tmp_path / "catalog.json",
                        "TASKS_FILE": tmp_path / "tasks.json", "TASKS": {}, "PROCESSING_VIDEOS": {},
                        "RENDERING": set(), "PENDING_PUBLISH": {}}.items():
        monkeypatch.setattr(server, name, value)
    original = tmp_path / "source.mp4"
    original.write_bytes(b"original source retained for editing")
    work = tmp_path / "renders"
    work.mkdir()
    output = work / "final.mp4"
    output.write_bytes(CONTENT)
    preview = work / "preview.mp4"
    preview.write_bytes(b"preview remains available")
    server.write_json(work / "manifest.json", {
        "source": str(original), "sha256": server.sha256_file(original), "probe": {"duration": 10},
        "transcription_engine": "none", "ranking": "manual", "clips": [{
            "id": "clip_01", "title": "Corte de teste", "start": 0, "end": 10,
            "preview": {"path": str(preview), "render_id": "preview-1", "burn": False, "layout": "fit"},
            "final": {"path": str(output), "render_id": "final-1", "burn": False, "layout": "fit"},
        }],
    })
    server.write_json(server.INDEX, {VID: {"id": VID, "path": str(original), "name": "source.mp4",
        "duration": 10, "size": original.stat().st_size, "created_at": "2026-01-01T00:00:00+00:00",
        "job_path": str(work)}})
    api = DriveAPI()
    backend = StorageDelivery(tmp_path, Vault(tmp_path), clock=lambda: 100,
        client_factory=lambda: httpx.Client(transport=httpx.MockTransport(api),
                                            trust_env=False, follow_redirects=False))
    monkeypatch.setattr(server, "STORAGE_DELIVERY", backend)
    monkeypatch.setattr(server, "CONNECTION_VAULT", backend.vault)
    client = TestClient(server.app, base_url=ORIGIN)
    context = SimpleNamespace(client=client, backend=backend, api=api, original=original,
                              preview=preview, output=output, work=work)
    login(context)
    yield context
    client.close()


def optimize(context):
    response = context.client.post("/api/storage/optimize-drive", headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    return response.json()


def archive_final(context):
    connect(context)
    assert optimize(context)["summary"]["queued"] == 1
    assert context.backend.run_once()
    job = next(iter(context.backend.jobs.values()))
    assert job["status"] == "succeeded" and job["archived"] is True, job.get("archive_error")
    assert not context.output.exists()
    return job


def test_capacity_requires_login_and_never_exposes_credentials(studio):
    studio.client.cookies.clear()
    assert studio.client.get("/api/storage/capacity").status_code == 401
    assert studio.client.post("/api/storage/optimize-drive").status_code == 401
    assert not studio.api.requests
    login(studio)
    connect(studio)
    response = studio.client.get("/api/storage/capacity")
    assert response.status_code == 200
    assert response.json()["drive"]["free_bytes"] == 9000
    assert response.json()["archive"]["archived_count"] == 0
    assert response.json()["archive"]["archived_bytes"] == 0
    assert "no-store" in response.headers["Cache-Control"]
    for private in (ACCESS, REFRESH, "test-client-secret-private", str(studio.work), "archive-account"):
        assert private not in response.text


@pytest.mark.parametrize("headers", [{"Origin": "https://untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_optimize_rejects_cross_site_requests_before_changing_policy(studio, headers):
    connect(studio)
    response = studio.client.post("/api/storage/optimize-drive", headers=headers)
    assert response.status_code == 403
    assert not studio.api.requests and not studio.backend.jobs
    assert studio.backend.public_settings(OWNER)["archive_after_upload"] is False


@pytest.mark.parametrize("failure", ["disconnected", "full", "quota-error"])
def test_optimize_does_not_enable_archiving_when_drive_is_unavailable(studio, failure):
    if failure != "disconnected":
        connect(studio)
    if failure == "full":
        studio.api.quota["usage"] = studio.api.quota["limit"]
    elif failure == "quota-error":
        studio.api.quota_error = 401
    response = studio.client.post("/api/storage/optimize-drive", headers={"Origin": ORIGIN})
    assert response.status_code == 422
    assert "provider-private-error" not in response.text
    settings = studio.backend.public_settings(OWNER)
    assert not settings["auto_upload"] and not settings["archive_after_upload"]
    assert not studio.backend.jobs and studio.output.read_bytes() == CONTENT


def test_optimize_queues_only_final_with_exact_metadata_then_keeps_download_url(studio):
    connect(studio)
    url = f"/api/videos/{VID}/rendered/clip_01/final?v=final-1&download=1"
    local = studio.client.get(url, headers={"Range": "bytes=2-8"})
    assert local.status_code == 206 and local.content == CONTENT[2:9]
    result = optimize(studio)
    assert result["settings"]["provider"] == "drive"
    assert result["settings"]["auto_upload"] and result["settings"]["archive_after_upload"]
    assert result["settings"]["qualities"] == ["final"]
    assert result["summary"] == {"queued": 1, "already_archived": 0, "skipped_busy": 0, "errors": []}
    job = next(iter(studio.backend.jobs.values()))
    assert (job["owner"], job["video_id"], job["kind"], job["clip_id"], job["quality"], job["render_id"]) == (
        OWNER, VID, "clip", "clip_01", "final", "final-1")
    assert job["sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert job["source_path"] == str(studio.output) and Path(job["path"]).read_bytes() == CONTENT
    assert studio.output.exists(), "Enqueue alone must preserve the local file"
    assert studio.client.get("/api/storage/capacity").json()["archive"]["pending_count"] == 1
    assert studio.backend.run_once()
    assert job["archived"] and not studio.output.exists()
    assert studio.original.is_file() and studio.preview.is_file()
    remote = studio.client.get(url, headers={"Range": "bytes=2-8"})
    assert remote.status_code == 206 and remote.content == local.content
    assert remote.headers["Content-Range"] == local.headers["Content-Range"] == f"bytes 2-8/{len(CONTENT)}"
    assert remote.headers["Content-Disposition"] == local.headers["Content-Disposition"]
    assert "attachment;" in remote.headers["Content-Disposition"]
    assert remote.headers["Content-Type"].startswith("video/mp4")
    assert "X-Provider-Secret" not in remote.headers
    assert studio.client.get(url.replace("final-1", "obsolete")).status_code == 409
    assert studio.client.get(url).content == CONTENT
    report = studio.client.get("/api/storage/capacity").json()
    assert report["archive"]["archived_count"] == 1 and report["archive"]["archived_bytes"] == len(CONTENT)
    assert report["archive"]["pending_count"] == 0
    repeated = optimize(studio)
    assert repeated["summary"]["already_archived"] == 1 and repeated["summary"]["queued"] == 0
    assert len(studio.backend.jobs) == 1


def test_legacy_render_url_survives_optimization_and_archival(studio):
    edit_manifest(studio, lambda doc: doc["clips"][0]["final"].pop("render_id"))
    original_url = studio.client.get(f"/api/videos/{VID}/manifest").json()["clips"][0]["final_url"]
    assert "legacy-" in original_url
    archive_final(studio)
    response = studio.client.get(original_url, headers={"Range": "bytes=-5"})
    assert response.status_code == 206 and response.content == CONTENT[-5:]
    current = studio.client.get(f"/api/videos/{VID}/manifest").json()["clips"][0]
    assert current["final_url"] == original_url


def test_legacy_revision_changed_after_optimization_keeps_new_bytes_and_rejects_old_url(studio):
    edit_manifest(studio, lambda doc: doc["clips"][0]["final"].pop("render_id"))
    original_url = studio.client.get(f"/api/videos/{VID}/manifest").json()["clips"][0]["final_url"]
    connect(studio)
    optimize(studio)
    replacement = studio.work / 'replacement.mp4'
    replacement.write_bytes(b'a different completed legacy revision')
    replacement.replace(studio.output)
    assert studio.client.get(original_url).status_code == 409
    assert studio.backend.run_once()
    job = next(iter(studio.backend.jobs.values()))
    assert job['status'] == 'succeeded' and not job['archived']
    assert studio.output.read_bytes() == b'a different completed legacy revision'
    current_url = studio.client.get(f"/api/videos/{VID}/manifest").json()["clips"][0]["final_url"]
    assert current_url != original_url
    assert studio.client.get(current_url).content == studio.output.read_bytes()


def test_timeline_export_stays_listed_and_downloadable_after_archiving(studio):
    edit_manifest(studio, lambda doc: doc["clips"][0].pop("final"))
    document, path = server._timeline_document(VID)
    server.write_json(path.parent / "timeline_exports.json", {"final": {
        "path": str(studio.output), "version": document["version"], "render_id": "timeline-final-1",
        "layout": "fit", "subtitles": False, "probe": {"duration": 10},
    }})
    job = archive_final(studio)
    assert job["kind"] == "timeline"
    exports = studio.client.get(f"/api/videos/{VID}/timeline/exports").json()["exports"]
    assert exports["final"]["render_id"] == "timeline-final-1"
    response = studio.client.get(exports["final"]["url"] + "&download=1", headers={"Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == CONTENT[:4]
    assert "attachment;" in response.headers["Content-Disposition"]
    assert studio.client.get(exports["final"]["url"].replace("timeline-final-1", "obsolete")).status_code == 409


def test_different_signed_in_owner_cannot_read_private_archive(studio):
    archive_final(studio)
    before = len(studio.api.requests)
    login(studio, OTHER_EMAIL)
    response = studio.client.get(f"/api/videos/{VID}/rendered/clip_01/final?v=final-1&download=1")
    assert response.status_code == 404
    assert studio.client.get("/api/storage/jobs").json()["jobs"] == []
    report = studio.client.get("/api/storage/capacity").json()
    assert report["drive"]["status"] == "disconnected"
    assert report["archive"]["archived_count"] == 0 and report["archive"]["archived_bytes"] == 0
    assert report["archive"]["pending_count"] == 0
    assert len(studio.api.requests) == before


@pytest.mark.parametrize("blocker", ["processing", "rendering", "publishing", "preview", "source", "other-source"])
def test_application_guard_preserves_busy_media_previews_and_sources(studio, blocker):
    if blocker == "preview":
        edit_manifest(studio, lambda doc: doc["clips"][0]["preview"].update(path=str(studio.output)))
    elif blocker == "source":
        edit_manifest(studio, lambda doc: doc["clips"][0]["final"].update(path=str(studio.original)))
    elif blocker == "other-source":
        catalog = server.catalog()
        catalog["f" * 32] = {"id": "f" * 32, "path": str(studio.output), "duration": 10}
        server.write_json(server.INDEX, catalog)
    connect(studio)
    optimize(studio)
    if blocker == "processing":
        server.PROCESSING_VIDEOS[VID] = "active-processing"
    elif blocker == "rendering":
        server.RENDERING.add((VID, "clip_01"))
    elif blocker == "publishing":
        server.PENDING_PUBLISH[(VID, "clip_01")] = "active-publish"
    assert studio.backend.run_once()
    job = next(iter(studio.backend.jobs.values()))
    assert job["status"] == "succeeded" and not job["archived"]
    assert Path(job["source_path"]).is_file()
    assert studio.original.is_file() and studio.preview.is_file()
    with server._archive_guard(job) as allowed:
        assert allowed is False


def test_optimize_reports_busy_projects_without_queuing_their_exports(studio):
    connect(studio)
    server.PROCESSING_VIDEOS[VID] = "active-processing"
    result = optimize(studio)
    assert result["summary"]["skipped_busy"] == 1 and result["summary"]["queued"] == 0
    assert not studio.backend.jobs and studio.output.exists()


def _upload_only_first_final_and_check_shared_file(studio, other_url):
    # Two manifest references to one pathname are not filesystem hard links.
    # Even st_nlink == 1 must not make an export safe to remove.
    assert studio.output.stat().st_nlink == 1
    connect(studio)
    settings = studio.client.patch("/api/storage/settings", headers={"Origin": ORIGIN}, json={
        "provider": "drive", "auto_upload": True, "archive_after_upload": True, "qualities": ["final"]})
    assert settings.status_code == 200
    response = studio.client.post(f"/api/videos/{VID}/deliveries", headers={"Origin": ORIGIN}, json={
        "kind": "clip", "clip_id": "clip_01", "quality": "final", "render_id": "final-1", "provider": "drive"})
    assert response.status_code == 200, response.text
    assert len(studio.backend.jobs) == 1
    assert studio.backend.run_once()
    job = next(iter(studio.backend.jobs.values()))
    assert job["status"] == "succeeded" and not job["archived"]
    assert studio.api.reserved == 1 and len(studio.api.files) == 1
    assert studio.output.read_bytes() == CONTENT and studio.output.stat().st_nlink == 1
    retained = studio.client.get(other_url, headers={"Range": "bytes=0-4"})
    assert retained.status_code == 206 and retained.content == CONTENT[:5]
    assert len(studio.backend.jobs) == 1, "The other export must remain readable without its own archive"
    assert studio.original.exists() and studio.preview.exists()


def _other_project_with_shared_export(studio, quality, path):
    other_id = "f" * 32
    other_work = studio.work.parent / "other-renders"
    other_work.mkdir()
    other_source = studio.work.parent / "other-source.mp4"
    other_source.write_bytes(b"independent source video")
    server.write_json(other_work / "manifest.json", {
        "source": str(other_source), "sha256": server.sha256_file(other_source), "probe": {"duration": 10},
        "transcription_engine": "none", "ranking": "manual", "clips": [{
            "id": "clip_02", "title": "Outro projeto", "start": 0, "end": 10,
            quality: {"path": str(path), "render_id": "other-render-1", "burn": False, "layout": "fit"},
        }],
    })
    catalog = server.catalog()
    catalog[other_id] = {"id": other_id, "path": str(other_source), "name": "other-source.mp4",
        "duration": 10, "created_at": "2026-01-02T00:00:00+00:00", "job_path": str(other_work)}
    server.write_json(server.INDEX, catalog)
    return f"/api/videos/{other_id}/rendered/clip_02/{quality}?v=other-render-1"


def test_shared_path_between_two_final_clips_is_preserved_with_only_one_upload(studio):
    edit_manifest(studio, lambda doc: doc["clips"].append({
        "id": "clip_02", "title": "Segundo corte", "start": 0, "end": 10,
        "final": {"path": str(studio.output), "render_id": "second-final-1", "burn": False, "layout": "fit"},
    }))
    _upload_only_first_final_and_check_shared_file(studio,
        f"/api/videos/{VID}/rendered/clip_02/final?v=second-final-1")


def test_shared_path_between_timeline_and_final_clip_is_preserved_with_one_upload(studio):
    document, path = server._timeline_document(VID)
    server.write_json(path.parent / "timeline_exports.json", {"final": {
        "path": str(studio.output), "version": document["version"], "render_id": "shared-timeline-1",
        "layout": "fit", "subtitles": False, "probe": {"duration": 10},
    }})
    _upload_only_first_final_and_check_shared_file(studio,
        f"/api/videos/{VID}/timeline/rendered/final?render_id=shared-timeline-1")


def test_shared_path_used_by_other_project_preview_is_preserved_with_one_upload(studio):
    other_url = _other_project_with_shared_export(studio, "preview", studio.output)
    _upload_only_first_final_and_check_shared_file(studio, other_url)


def test_shared_path_with_parent_segments_in_other_project_final_is_preserved(studio):
    alias = studio.work.parent / "other-renders" / ".." / "renders" / "final.mp4"
    assert str(alias) != str(studio.output) and alias.resolve() == studio.output.resolve()
    other_url = _other_project_with_shared_export(studio, "final", alias)
    _upload_only_first_final_and_check_shared_file(studio, other_url)


@pytest.mark.parametrize("upload_fails", [False, True])
def test_youtube_publication_restores_archived_bytes_only_for_the_upload(studio, monkeypatch, upload_fails):
    archive_final(studio)
    finished = threading.Event()
    restored_paths = []
    save = server._save_tasks
    def watch_save():
        save()
        if any(task.get("status") in {"done", "error"} for task in server.TASKS.values()):
            finished.set()
    monkeypatch.setattr(server, "_save_tasks", watch_save)
    def upload(_vault, path, **kwargs):
        restored_paths.append(Path(path))
        assert Path(path).read_bytes() == CONTENT
        assert Path(path) != studio.output and Path(path).parent == studio.backend.folder
        assert kwargs["privacy_status"] == "private"
        assert (VID, "clip_01") in server.PENDING_PUBLISH
        if upload_fails:
            raise RuntimeError("Simulated YouTube upload interruption")
        return {"youtube_video_id": "test-upload-id", "privacy_status": "private"}
    monkeypatch.setattr(server, "upload_youtube_video", upload)
    response = studio.client.post(f"/api/videos/{VID}/clips/clip_01/publish/youtube",
        json={"approved_publish": True, "privacy_status": "private"})
    assert response.status_code == 200, response.text
    assert finished.wait(5), "Publication task did not complete"
    task = studio.client.get("/api/tasks/" + response.json()["task_id"]).json()
    assert task["status"] == ("error" if upload_fails else "done"), task
    assert len(restored_paths) == 1 and not restored_paths[0].exists()
    assert not list(studio.backend.folder.glob("restore-*"))
    assert not studio.output.exists() and studio.original.exists()
    assert (VID, "clip_01") not in server.PENDING_PUBLISH
    assert studio.client.get(f"/api/videos/{VID}/rendered/clip_01/final?v=final-1").content == CONTENT
