"""Detection contract tests using local video and opt-in real YuNet fixtures."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import struct
import subprocess

import pytest

import face_detection as faces


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required")
    source = tmp_path_factory.mktemp("face-detection") / "sample.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=10:d=4",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source),
    ], check=True, timeout=20)
    return source


def test_repeated_corner_detection_ranks_above_one_frame_game_face(sample_video, monkeypatch):
    call_count = 0

    def detect(png):
        nonlocal call_count
        assert struct.unpack(">II", png[16:24]) == (320, 180)
        call_count += 1
        corner = (0.84 + 0.001 * call_count, 0.06, 0.09, 0.14, 0.82)
        return [corner, (0.40, 0.35, 0.15, 0.22, 0.98)] if call_count == 1 else [corner]

    monkeypatch.setattr(faces, "_open_detector", lambda: detect)
    progress = []
    before = hashlib.sha256(sample_video.read_bytes()).hexdigest()
    result = faces.analyze_faces(sample_video, start=0.5, end=3.5, progress=progress.append)
    assert hashlib.sha256(sample_video.read_bytes()).hexdigest() == before

    assert result["status"] == "multiple"
    assert (result["width"], result["height"]) == (320, 180)
    assert 3 <= result["samples"] <= 8
    repeated, transient = result["candidates"]
    assert repeated["likely_facecam"] is True
    assert repeated["samples"] == result["samples"]
    assert repeated["confidence"] == 0.82
    assert transient["samples"] == 1
    assert transient["confidence"] == 0.98
    assert transient["likely_facecam"] is False
    assert repeated["id"] != transient["id"]
    assert 0.5 < repeated["timestamp"] < 3.5
    assert progress[-1]["progress"] == 100
    assert {step["phase"] for step in progress} == {"detecting_faces"}

    box, region = repeated["box"], repeated["region"]
    assert region["x"] <= box["x"] < box["x"] + box["w"] <= region["x"] + region["w"] <= 1.0
    assert region["y"] <= box["y"] < box["y"] + box["h"] <= region["y"] + region["h"] <= 1.0
    assert region["w"] > box["w"] and region["h"] > box["h"]


def test_empty_detection_is_a_successful_analysis(sample_video, monkeypatch):
    monkeypatch.setattr(faces, "_open_detector", lambda: lambda png: [])
    result = faces.analyze_faces(sample_video)
    assert result["status"] == "none"
    assert result["candidates"] == []
    assert result["samples"] > 0


def test_context_around_a_small_detection_can_be_used_in_layout(sample_video, monkeypatch):
    from composition import normalize_layout_config

    monkeypatch.setattr(faces, "_open_detector", lambda: lambda png: [(0.99, 0.99, 0.003, 0.004, 0.8)])
    result = faces.analyze_faces(sample_video, end=0.5)
    region = result["candidates"][0]["region"]
    config = normalize_layout_config({"mode": "split", "face": region})
    assert config["face"] == region


def test_display_geometry_matches_rotated_frame(sample_video, tmp_path, monkeypatch):
    rotated = tmp_path / "rotated.mp4"
    remux = ["ffmpeg", "-v", "error", "-i", str(sample_video), "-c", "copy"]
    options = subprocess.run(["ffmpeg", "-hide_banner", "-h", "full"],
                             capture_output=True, text=True, check=True, timeout=20).stdout
    if "-display_rotation" in options:
        remux[3:3] = ["-display_rotation:v:0", "90"]
    else:
        remux.extend(["-metadata:s:v:0", "rotate=90"])
    subprocess.run([*remux, str(rotated)], check=True, timeout=20)

    def detect(png):
        assert struct.unpack(">II", png[16:24]) == (180, 320)
        return [(0.1, 0.1, 0.2, 0.2, 0.9)]

    monkeypatch.setattr(faces, "_open_detector", lambda: detect)
    result = faces.analyze_faces(rotated)
    assert (result["width"], result["height"]) == (180, 320)
    assert result["status"] == "single"
    assert faces.display_frame_geometry(rotated, max_side=160) == (180, 320, 90, 160)


def test_display_geometry_uses_square_pixels_for_anamorphic_source(sample_video, tmp_path, monkeypatch):
    anamorphic = tmp_path / "anamorphic.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(sample_video), "-t", "0.5",
        "-vf", "setsar=2/1", "-c:v", "libx264", "-threads", "1", str(anamorphic),
    ], check=True, timeout=20)

    def detect(png):
        assert struct.unpack(">II", png[16:24]) == (640, 180)
        return []

    monkeypatch.setattr(faces, "_open_detector", lambda: detect)
    result = faces.analyze_faces(anamorphic)
    assert (result["width"], result["height"]) == (640, 180)
    assert faces.display_frame_geometry(anamorphic, max_side=320) == (640, 180, 320, 90)


def test_short_video_with_one_frame_can_be_analyzed(sample_video, tmp_path, monkeypatch):
    short = tmp_path / "one-frame.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(sample_video), "-frames:v", "1",
        "-c:v", "libx264", "-threads", "1", str(short),
    ], check=True, timeout=20)
    monkeypatch.setattr(faces, "_open_detector", lambda: lambda png: [])
    result = faces.analyze_faces(short)
    assert result["status"] == "none"
    assert result["samples"] == 1


@pytest.mark.parametrize("start,end", [(3, 2), (-1, 1), (0, 5), (4, None), (float("nan"), 2)])
def test_invalid_ranges_are_rejected(sample_video, start, end):
    with pytest.raises(ValueError, match="Intervalo"):
        faces.analyze_faces(sample_video, start=start, end=end)


def test_unavailable_model_reports_a_distinct_error(sample_video, tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOCLIPS_FACE_MODEL", str(tmp_path / "missing.onnx"))
    with pytest.raises(faces.FaceDetectionUnavailable, match="YuNet"):
        faces.analyze_faces(sample_video)


def test_undecodable_source_reports_a_distinct_error(tmp_path):
    source = tmp_path / "corrupt.mp4"
    source.write_bytes(b"not a video container")
    with pytest.raises(faces.FaceDetectionError):
        faces.analyze_faces(source)


def test_model_preparer_rejects_download_that_fails_integrity(tmp_path, monkeypatch):
    from scripts import prepare_face_model

    destination = tmp_path / "yunet.onnx"
    destination.write_bytes(b"existing model file")
    monkeypatch.setattr(prepare_face_model, "urlopen", lambda url, timeout: io.BytesIO(b"invalid data"))
    with pytest.raises(RuntimeError, match="integrity"):
        prepare_face_model.prepare_model(destination)
    assert destination.read_bytes() == b"existing model file"


@pytest.mark.parametrize("left,top", [(12, 12), (830, 390)])
def test_real_yunet_detects_nasa_portrait_in_gameplay_corner(sample_video, tmp_path, left, top):
    # NASA portrait by way of scikit-image's public-domain astronaut image.
    # https://scikit-image.org/docs/stable/api/skimage.data.html#skimage.data.astronaut
    portrait = os.environ.get("AUTOCLIPS_FACE_QA_PORTRAIT")
    if not portrait or not Path(portrait).is_file() or not faces.face_detection_available():
        pytest.skip("Set AUTOCLIPS_FACE_QA_PORTRAIT and prepare the YuNet model for this smoke test")

    source = tmp_path / "gameplay.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-filter_complex_threads", "1",
        "-i", str(sample_video), "-i", portrait,
        "-filter_complex", f"[0:v]scale=960:540[bg];[1:v]crop=180:194:135:22,scale=108:116[cam];"
        f"[bg][cam]overlay={left}:{top}",
        "-an", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(source),
    ], check=True, timeout=20)

    result = faces.analyze_faces(source)
    assert result["status"] == "single"
    candidate, = result["candidates"]
    box = candidate["box"]
    assert left / 960 < box["x"] < (left + 108) / 960
    assert top / 540 < box["y"] < (top + 116) / 540
    assert candidate["likely_facecam"] is True
    assert candidate["samples"] == result["samples"]
