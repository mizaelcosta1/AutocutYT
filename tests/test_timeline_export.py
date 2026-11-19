"""Actual FFmpeg render + API gating for two-source-window export."""
from pathlib import Path
import subprocess
import json
import shutil
import pytest
from autoclips import probe, sha256_file
from timeline import new_timeline, apply_command
from timeline_export import render_project

VIDEO_ID = "b" * 32


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg absent")
    src = tmp_path_factory.mktemp("renders") / "synthetic.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050",
        "-t", "3", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(src)],
        check=True, timeout=30)
    return src


def test_preview_renders_two_segments_with_audio_and_expected_duration(sample_video, tmp_path):
    doc = new_timeline(VIDEO_ID, 3, sha256_file(sample_video))
    doc = apply_command(doc, {"action": "trim", "id": "seg_001", "source_in": 0, "source_out": 1})
    doc = apply_command(doc, {"action": "add", "source_in": 1.9, "source_out": 2.9})
    dest = tmp_path / "joined.mp4"
    result = render_project(sample_video, dest, doc, words=[], quality="preview", layout="fit", subtitles=False)
    video = probe(dest)
    assert result["version"] == doc["version"]
    assert video["width"] == 720 and video["height"] == 1280
    assert video["audio_codec"] is not None
    assert 1.6 <= video["duration"] <= 2.5
    assert dest.stat().st_size > 1000


def test_video_changed_since_project_rejected(sample_video, tmp_path):
    doc = new_timeline(VIDEO_ID, 3, "0"*64)
    with pytest.raises(ValueError, match="original"):
        render_project(sample_video, tmp_path / "fail.mp4", doc, words=[], quality="preview", layout="fit", subtitles=False)


def test_export_api_requires_explicit_preview_approval(monkeypatch, tmp_path, sample_video):
    from fastapi.testclient import TestClient
    import server
    monkeypatch.setattr(server, "ROOT", tmp_path)
    monkeypatch.setattr(server, "video_info", lambda vid: {
        "id": VIDEO_ID, "duration": 3, "path": str(sample_video), "name": "synthetic.mp4",
        "width": 160, "height": 90,
    } if vid == VIDEO_ID else (_ for _ in ()).throw(server.HTTPException(404)))
    client = TestClient(server.app)
    base = f"/api/videos/{VIDEO_ID}/timeline/render/"
    assert client.post(base + "preview", json={"layout":"fit","approved_selection":False}).status_code == 400
    assert client.post(base + "final", json={"layout":"fit","approved_preview":True}).status_code == 409
    preview = client.post(base + "preview", json={"layout":"fit","approved_selection":True})
    assert preview.status_code == 200, preview.text
    task_id = preview.json()["task_id"]
    import time
    for _ in range(150):
        task = client.get("/api/tasks/" + task_id).json()
        if task["status"] in ("done", "error", "interrupted"):
            break
        time.sleep(0.1)
    assert task["status"] == "done", task
    assert client.get(f"/api/videos/{VIDEO_ID}/timeline/rendered/preview").status_code == 200
    assert client.post(base + "final", json={"layout":"fit","approved_preview":False}).status_code == 400
    assert client.post(base + "final", json={"layout":"crop","approved_preview":True}).status_code == 409
    # Editing invalidates approval; stale URL must not serve as current.
    state = client.get(f"/api/videos/{VIDEO_ID}/timeline").json()["timeline"]
    assert client.post(f"/api/videos/{VIDEO_ID}/timeline", json={
        "action":"split","id": "seg_001","at": 1,"expected_version":state["version"]}).status_code == 200
    assert client.get(f"/api/videos/{VIDEO_ID}/timeline/rendered/preview").status_code == 404
    assert client.post(base + "final", json={"layout":"fit","approved_preview":True}).status_code == 409


def test_composed_horizontal_timeline_preserves_audio_and_canonical_layout(sample_video, tmp_path):
    doc = new_timeline(VIDEO_ID, 3, sha256_file(sample_video))
    doc = apply_command(doc, {"action": "trim", "id": "seg_001", "source_in": .2, "source_out": .9})
    doc = apply_command(doc, {"action": "add", "source_in": 2., "source_out": 2.7})
    config = {"mode": "split_caption", "aspect": "16:9", "face": {"x": 0, "y": 0, "w": .5, "h": .5}}
    result = render_project(sample_video, tmp_path / "split.mp4", doc, quality="preview",
                            layout="fit", layout_config=config, subtitles=True,
                            words=[{"start": .3, "end": .7, "text": "Olá"}])
    assert (result["output_width"], result["output_height"]) == (1280, 720)
    assert result["layout_config"]["face"] == config["face"]
    assert result["layout_config"]["game_fit"] == "contain"
    assert result["probe"]["audio_codec"] == "aac"
    assert result["probe"]["duration"] == pytest.approx(1.4, abs=.25)


def test_timeline_discards_render_if_source_replaced_during_export(sample_video, tmp_path, monkeypatch):
    import timeline_export

    source = tmp_path / "original.mp4"
    shutil.copyfile(sample_video, source)
    doc = new_timeline(VIDEO_ID, 3, sha256_file(source))
    doc = apply_command(doc, {"action": "trim", "id": "seg_001", "source_in": 0, "source_out": .3})
    real_render = timeline_export.render_clip

    def change_source_after_part(*args, **kwargs):
        result = real_render(*args, **kwargs)
        source.write_bytes(b"new upload")
        return result

    monkeypatch.setattr(timeline_export, "render_clip", change_source_after_part)
    dest = tmp_path / "joined.mp4"
    with pytest.raises(ValueError, match="original mudou"):
        render_project(source, dest, doc, words=[], quality="preview", layout="fit", subtitles=False)
    assert not dest.exists()
    assert not list(tmp_path.glob("autoclips-parts-*"))
