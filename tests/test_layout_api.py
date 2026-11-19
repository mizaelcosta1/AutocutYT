"""Layout persistence and approvals through HTTP, using real local MP4 exports."""
import importlib
import io
import sys
import subprocess
import time

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


def task_done(client, response):
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        task = client.get("/api/tasks/" + response.json()["task_id"]).json()
        if task["status"] not in {"queued", "running"}:
            assert task["status"] == "done", task
            return task["result"]
        time.sleep(.05)
    pytest.fail("Render did not finish")


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
    assert client.get("/api/layout-presets").status_code == 401


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
