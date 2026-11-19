"""Exercise real FastAPI timeline boundary and SRT, without external accounts."""
from pathlib import Path
import json
from fastapi.testclient import TestClient
import pytest
import server

VIDEO_ID = "a" * 32


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ROOT", tmp_path)
    original = tmp_path / "original.mp4"
    original.write_bytes(b"sample video placeholder")
    monkeypatch.setattr(server, "video_info", lambda video_id: {
        "id": VIDEO_ID, "duration": 50.0, "path": str(original),
        "name": "video.mp4", "width": 640, "height": 360,
    } if video_id == VIDEO_ID else (_ for _ in ()).throw(server.HTTPException(404)))
    return TestClient(server.app)


def test_timeline_created_persisted_and_version_conflict(client, tmp_path):
    base = f"/api/videos/{VIDEO_ID}/timeline"
    first = client.get(base)
    assert first.status_code == 200
    state = first.json()["timeline"]
    assert state["version"] == 1
    assert state["segments"] == [{"id": "seg_001", "source_in": 0, "source_out": 50.0}]
    assert "source_sha256" not in state
    post = client.post(base, json={"action": "split", "id": "seg_001", "at": 12, "expected_version": 1})
    assert post.status_code == 200, post.text
    assert [x["source_out"] for x in post.json()["timeline"]["segments"]] == [12, 50.0]
    assert client.get(base).json()["timeline"]["version"] == 2
    assert client.post(base, json={"action": "undo", "expected_version": 1}).status_code == 409
    assert client.post(base, json={"action": "split", "id": "seg_001", "at": 0, "expected_version": 2}).status_code == 400
    assert (tmp_path / "projects" / VIDEO_ID / "timeline.json").exists()


def test_transcript_and_srt_respects_ripple_cuts(client, tmp_path):
    project = tmp_path / "projects" / VIDEO_ID
    project.mkdir(parents=True)
    (project / "transcript.json").write_text(json.dumps({"words": [
        {"start": 1, "end": 1.5, "text": "Oi"},
        {"start": 10, "end": 10.6, "text": "pessoal"},
    ], "engine": "test"}), encoding="utf8")
    base = f"/api/videos/{VIDEO_ID}"
    assert client.get(base + "/transcript").json()["words"][1]["text"] == "pessoal"
    state = client.get(base + "/timeline").json()["timeline"]
    assert client.post(base + "/timeline", json={"action": "trim", "id": "seg_001", "source_in": 9, "source_out": 12, "expected_version": state["version"]}).status_code == 200
    rendered_srt = client.get(base + "/timeline.srt")
    assert rendered_srt.status_code == 200
    assert "pessoal" in rendered_srt.text
    assert "Oi" not in rendered_srt.text
    assert "00:00:01,000" in rendered_srt.text  # Original 10s now at 1s.


def test_missing_transcript_and_invalid_id(client):
    assert client.get(f"/api/videos/{VIDEO_ID}/timeline.srt").status_code == 404
    assert client.get("/api/videos/../timeline").status_code != 200


def test_advanced_editor_served(client):
    response = client.get(f"/studio/{VIDEO_ID}")
    assert response.status_code == 200
    assert "AutoClips" in response.text
    assert "<video" in response.text
    assert 'src="/static/timeline.js"' in response.text
    assert client.get("/static/timeline.js").status_code == 200
