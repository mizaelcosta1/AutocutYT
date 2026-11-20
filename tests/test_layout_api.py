"""Layout persistence and approvals through HTTP, using real local MP4 exports."""
import importlib
import io
from pathlib import Path
import sys
import subprocess
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_DATA_ROOT", str(tmp_path / "data"))
    for name in ("AUTOCLIPS_GOOGLE_CLIENT_ID", "AUTOCLIPS_GOOGLE_CLIENT_SECRET", "AUTOCLIPS_SESSION_SECRET"):
        monkeypatch.delenv(name, raising=False)
    previous_modules = {name: sys.modules.get(name) for name in ("studio_auth", "server")}
    for name in previous_modules:
        sys.modules.pop(name, None)
    server = importlib.import_module("server")
    source = server.MEDIA / "layout-fixture.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=6",
                    "-t", "1.3", "-c:v", "libx264", "-preset", "ultrafast", str(source)], check=True)
    vid = "d" * 32
    server.save_video({"id": vid, "path": str(source), "duration": 1.3, "width": 320, "height": 180,
                       "name": "layout-fixture.mp4", "created_at": server.utcnow(), "size": source.stat().st_size})
    client = TestClient(server.app)
    yield server, client, vid
    server.POOL.shutdown(wait=True)
    for name, previous in previous_modules.items():
        if previous is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


def terminal_task(client, response):
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        task = client.get("/api/tasks/" + response.json()["task_id"]).json()
        if task["status"] not in {"queued", "running"}:
            return task
        time.sleep(.05)
    pytest.fail("Task did not finish")


def task_done(client, response):
    task = terminal_task(client, response)
    assert task["status"] == "done", task
    return task["result"]


def composition():
    return {"mode": "pip", "aspect": "16:9", "face": {"x": 0., "y": 0., "w": .5, "h": .5}}


def test_saved_layout_and_custom_presets_are_reusable_and_validated(studio):
    server, client, vid = studio
    path = f"/api/videos/{vid}/layout"
    assert client.get(path).json()["layout_config"]["mode"] == "standard"
    payload = {"layout": "fit", "layout_config": composition()}
    saved = client.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert client.get(path).json() == saved.json()
    assert server.read_json(server._project_folder(vid) / "layout.json", {}) == saved.json()
    invalid = composition() | {"face": {"x": .9, "y": 0., "w": .5, "h": .5}}
    assert client.put(path, json={**payload, "layout_config": invalid}).status_code == 422
    assert client.put(path, json={**payload, "layout_config": {"mode": "shell", "command": "false"}}).status_code == 422
    preset = client.post("/api/layout-presets", json={**payload, "name": "  Meu game  ", "subtitles": False})
    assert preset.status_code == 200 and preset.json()["name"] == "Meu game"
    catalog = client.get("/api/layout-presets").json()
    assert len(catalog["presets"]) == 8
    assert catalog["custom_presets"][0]["layout_config"]["face"] == composition()["face"]
    assert client.post("/api/layout-presets", json={**payload, "name": "   "}).status_code == 422
    assert client.delete("/api/layout-presets/gaming-captions").status_code == 404
    assert client.delete("/api/layout-presets/" + preset.json()["id"]).status_code == 200
    assert not client.get("/api/layout-presets").json()["custom_presets"]


def test_layout_persists_subtitle_preference_and_legacy_writes_keep_it(studio):
    _, client, vid = studio
    path = f"/api/videos/{vid}/layout"
    assert client.get(path).json()["subtitles"] is None
    layout = {"layout": "fit", "layout_config": {"mode": "split_caption"}}
    for preference in (True, False):
        saved = client.put(path, json={**layout, "subtitles": preference})
        assert saved.status_code == 200, saved.text
        assert saved.json()["subtitles"] is preference
        assert client.get(path).json()["subtitles"] is preference
        legacy = client.put(path, json=layout)
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["subtitles"] is preference
        assert client.get(path).json()["subtitles"] is preference
    cleared = client.put(path, json={**layout, "subtitles": None})
    assert cleared.status_code == 200, cleared.text
    assert client.get(path).json()["subtitles"] is None


def test_frame_and_face_endpoints_have_clear_fallback_and_auth(studio, monkeypatch):
    server, client, vid = studio
    base = f"/api/videos/{vid}"
    frame = client.get(base + "/frame?at=0.2")
    assert frame.status_code == 200 and frame.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(frame.content)).size == (320, 180)
    assert "no-store" in frame.headers["cache-control"]
    for at in ("-1", "nan", "inf", "1.3"):
        assert client.get(base + "/frame?at=" + at).status_code == 400
    monkeypatch.setattr(server, "_face_detection_ready", lambda: False)
    response = client.post(base + "/faces", json={})
    assert response.status_code == 503 and "manualmente" in response.json()["detail"]
    assert client.post(base + "/faces", json={"start": 1.2, "end": .4}).status_code == 400
    monkeypatch.setattr(server, "_face_detection_ready", lambda: True)
    import face_detection
    def no_face(*args, **kwargs):
        kwargs["progress"]({"phase": "detecting_faces", "progress": 100, "message": "Quadros analisados."})
        return {"status": "none", "candidates": [], "samples": 8}
    monkeypatch.setattr(face_detection, "analyze_faces", no_face)
    assert task_done(client, client.post(base + "/faces", json={})) == {"status": "none", "candidates": [], "samples": 8}
    monkeypatch.setattr(server.studio_auth, "configured", lambda: True)
    monkeypatch.setattr(server.studio_auth, "read_session", lambda request: None)
    assert client.get(base + "/frame").status_code == 401
    assert client.get(base + "/activity").status_code == 401
    assert client.get("/api/layout-presets").status_code == 401


def test_video_activity_recovers_queued_and_running_detection_after_reload(studio, monkeypatch):
    server, client, vid = studio
    import face_detection

    base = f"/api/videos/{vid}"
    assert client.get(base + "/activity").json() == {"activity": None}
    assert client.get("/api/videos/unknown/activity").status_code == 404
    assert client.post(base + "/manual", json={"start": 0, "end": 1}).status_code == 200

    free_worker = threading.Event()
    detecting = threading.Event()
    finish_detection = threading.Event()

    def detect(*args, progress, **kwargs):
        progress({"phase": "detecting_faces", "progress": 50, "message": "Verificando o quadro."})
        detecting.set()
        assert finish_detection.wait(10)
        return {"status": "none", "candidates": [], "samples": 2}

    monkeypatch.setattr(server, "_face_detection_ready", lambda: True)
    monkeypatch.setattr(face_detection, "analyze_faces", detect)
    for _ in range(2):
        server.POOL.submit(free_worker.wait, 10)
    try:
        response = client.post(base + "/faces", json={})
        assert response.status_code == 200, response.text
        task_id = response.json()["task_id"]
        reloaded = TestClient(server.app)
        queued = {"task_id": task_id, "kind": "detect-faces", "status": "queued"}
        pending = reloaded.get(base + "/activity")
        assert pending.json() == {"activity": queued}
        assert "no-store" in pending.headers["cache-control"]
        preview = {"clip_id": "clip_01", "approved_selection": True}
        assert reloaded.post(base + "/render/preview", json=preview).status_code == 409

        free_worker.set()
        assert detecting.wait(10)
        assert reloaded.get(base + "/activity").json() == {"activity": {
            **queued, "status": "running", "phase": "detecting_faces", "progress": 50,
            "message": "Verificando o quadro.",
        }}
        finish_detection.set()
        assert task_done(reloaded, response) == {"status": "none", "candidates": [], "samples": 2}
        assert reloaded.get(base + "/activity").json() == {"activity": None}
    finally:
        free_worker.set()
        finish_detection.set()


def test_video_activity_is_released_when_worker_fails(studio, monkeypatch):
    server, client, vid = studio
    import face_detection

    def fail(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(server, "_face_detection_ready", lambda: True)
    monkeypatch.setattr(face_detection, "analyze_faces", fail)
    base = f"/api/videos/{vid}"
    response = client.post(base + "/faces", json={})
    assert response.status_code == 200, response.text
    assert terminal_task(client, response)["status"] == "error"
    assert client.get(base + "/activity").json() == {"activity": None}
    assert client.post(base + "/faces", json={}).status_code == 200


def test_interrupted_transcription_allows_retry_and_preserves_words(studio, monkeypatch):
    server, client, vid = studio
    base = f"/api/videos/{vid}"
    words = [{"start": 0.1, "end": 0.4, "text": "Exemplo"}]
    old_transcript = {"engine": "faster-whisper", "words": words}
    server.write_json(server._project_folder(vid) / "transcript.json", old_transcript)
    server._update_video(vid, transcription={"status": "running", "message": "Preparando transcrição."})
    stalled = client.get(base + "/transcript").json()
    assert stalled["state"]["status"] == "error"
    assert "novamente" in stalled["state"]["message"]
    assert stalled["available"] and stalled["words"] == words

    started = threading.Event()
    finish = threading.Event()

    def transcribe(*args, **kwargs):
        started.set()
        assert finish.wait(10)
        return old_transcript

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace())
    monkeypatch.setattr(server, "transcript_local", transcribe)
    try:
        retry = client.post(base + "/transcript")
        assert retry.status_code == 200, retry.text
        assert started.wait(10)
        active = client.get(base + "/transcript").json()
        assert active["state"]["status"] == "running" and active["words"] == words
        finish.set()
        assert task_done(client, retry)["words"] == 1
        assert client.get(base + "/transcript").json()["state"]["status"] == "done"
    finally:
        finish.set()


def test_preview_storage_estimate_accounts_for_timeline_join(studio, monkeypatch):
    server, client, vid = studio
    base = f"/api/videos/{vid}"
    assert client.post(base + "/manual", json={"start": 0, "end": 1}).status_code == 200
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: SimpleNamespace(free=9_200_000))
    payload = {"approved_selection": True}
    preview = client.post(base + "/timeline/render/preview", json=payload)
    assert preview.status_code == 507, preview.text
    assert "9,4 MB" in preview.json()["detail"] and "9,2 MB" in preview.json()["detail"]
    assert client.get(base + "/activity").json() == {"activity": None}
    task_done(client, client.post(base + "/render/preview", json={**payload, "clip_id": "clip_01"}))
    response = client.post(base + "/render/final", json={"clip_id": "clip_01", "approved_preview": True})
    assert response.status_code == 507, response.text


def test_preview_checks_space_again_after_queue_wait(studio, monkeypatch):
    server, client, vid = studio
    base = f"/api/videos/{vid}"
    free_worker = threading.Event()
    free_bytes = 60_000_000
    monkeypatch.setattr(server.shutil, "disk_usage", lambda path: SimpleNamespace(free=free_bytes))
    for _ in range(2):
        server.POOL.submit(free_worker.wait, 10)
    try:
        preview = client.post(base + "/timeline/render/preview", json={"approved_selection": True})
        assert preview.status_code == 200, preview.text
        free_bytes = 2_000_000
        assert client.post(base + "/timeline/render/preview", json={"approved_selection": True}).status_code == 409
        free_worker.set()
        task = terminal_task(client, preview)
        assert task["status"] == "error" and "2,0 MB livres" in task["error"]
        assert client.get(base + "/timeline/exports").json() == {"exports": {}}
        assert client.get(base + "/activity").json() == {"activity": None}
    finally:
        free_worker.set()


def test_failed_clip_render_preserves_previous_preview_and_its_subtitles(studio, monkeypatch):
    server, client, vid = studio
    base = f"/api/videos/{vid}"
    assert client.post(base + "/manual", json={"start": 0, "end": 1}).status_code == 200

    def generate_subtitle(words, start, end, srt):
        srt.write_text("1\n00:00:00,000 --> 00:00:00,500\nExemplo.\n", encoding="utf-8")
        return True

    monkeypatch.setattr(server, "generate_srt", generate_subtitle)
    payload = {"clip_id": "clip_01", "approved_selection": True}
    task_done(client, client.post(base + "/render/preview", json=payload))
    before = server.get_manifest(vid)[0]["clips"][0]["preview"]
    old_subtitle = Path(before["subtitles"])

    def fail(*args, srt, **kwargs):
        assert srt.is_file() and srt != old_subtitle
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(server, "render_clip", fail)
    task = terminal_task(client, client.post(base + "/render/preview", json=payload))
    assert task["status"] == "error" and task["error"] == "falha simulada"
    assert set(old_subtitle.parent.glob("*.srt")) == {old_subtitle}
    assert server.get_manifest(vid)[0]["clips"][0]["preview"] == before


def test_clip_requires_face_matching_regions_and_exact_approved_preview(studio):
    _, client, vid = studio
    base = f"/api/videos/{vid}"
    assert client.post(base + "/manual", json={"start": 0, "end": 1}).status_code == 200
    payload = {"clip_id": "clip_01", "layout": "fit", "layout_config": composition(),
               "approved_selection": True, "approved_preview": True}
    response = client.post(base + "/render/preview", json={**payload, "layout_config": {"mode": "split"}})
    assert response.status_code == 400 and "rosto" in response.json()["detail"]
    task_done(client, client.post(base + "/render/preview", json=payload))
    clip = client.get(base + "/manifest").json()["clips"][0]
    assert clip["preview_options"]["layout_config"]["aspect"] == "16:9"
    pid = clip["preview_options"]["render_id"]
    assert client.post(base + "/render/final", json={**payload, "approved_preview_id": "old"}).status_code == 409
    changed = composition() | {"pip_position": "bottom-left"}
    assert client.post(base + "/render/final", json={**payload, "layout_config": changed}).status_code == 409
    task_done(client, client.post(base + "/render/final", json={**payload, "approved_preview_id": pid}))
    clip = client.get(base + "/manifest").json()["clips"][0]
    assert clip["final_options"]["layout_config"] == clip["preview_options"]["layout_config"]
    assert client.get(clip["final_url"]).status_code == 200


def test_timeline_horizontal_final_then_new_preview_invalidates_old_final(studio):
    _, client, vid = studio
    base = f"/api/videos/{vid}/timeline"
    assert client.get(base).status_code == 200
    payload = {"layout": "fit", "layout_config": composition(), "approved_selection": True, "approved_preview": True}
    task_done(client, client.post(base + "/render/preview", json=payload))
    meta = client.get(base + "/exports").json()["exports"]["preview"]
    assert meta["probe"]["width"] == 1280 and meta["probe"]["height"] == 720
    assert client.post(base + "/render/final", json={**payload, "approved_preview_id": "old"}).status_code == 409
    task_done(client, client.post(base + "/render/final", json={**payload, "approved_preview_id": meta["render_id"]}))
    final = client.get(base + "/exports").json()["exports"]["final"]
    assert final["probe"]["width"] == 1920 and final["probe"]["height"] == 1080
    task_done(client, client.post(base + "/render/preview", json=payload))
    assert "final" not in client.get(base + "/exports").json()["exports"]
    assert client.get(base + "/rendered/final").status_code == 404
