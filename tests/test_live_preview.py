"""Real local media tests for the single-input live preview pipeline.

Fixtures exercise FFmpeg, not a public YouTube/Twitch/Kick live transmission.
No provider key, media download or external network is used.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import live_preview


@pytest.fixture(scope="module")
def media_fixtures(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for real media validation")
    root = tmp_path_factory.mktemp("live-preview-media")
    result = {}
    for kind in ("av", "video", "audio"):
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if kind != "audio":
            args += ["-f", "lavfi", "-i", "testsrc2=size=160x90:rate=10"]
        if kind != "video":
            args += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000"]
        args += ["-t", "18", "-c:v", "libx264", "-preset", "ultrafast", "-g", "20",
                 "-c:a", "aac", "-f", "mpegts", str(root / f"{kind}.ts")]
        subprocess.run(args, check=True, capture_output=True, timeout=30)
        result[kind] = root / f"{kind}.ts"
    return result


def probe(path):
    output = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        timeout=20,
    )
    return json.loads(output)


def mux_fixture(source, target):
    directory = live_preview.preview_directory(target)
    directory.mkdir(exist_ok=True)
    with source.open("rb") as media:
        return subprocess.run(
            live_preview.build_ffmpeg_command(target), cwd=target.parent,
            stdin=media, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=30,
        )


def test_real_copy_keeps_recording_and_bounded_playable_hls(tmp_path, media_fixtures):
    target = tmp_path / "live_fixture.ts"
    completed = mux_fixture(media_fixtures["av"], target)
    assert completed.returncode == 0, completed.stderr.decode()
    directory = live_preview.preview_directory(target)
    status = live_preview.preview_status(directory)
    assert status["ready"] is True
    assert status["has_video"] is True
    assert status["ended"] is True
    assert status["segment_count"] == live_preview.PLAYLIST_SEGMENTS
    # One recently expired segment is retained for in-flight player requests.
    assert len(list(directory.glob("segment_*.ts"))) <= live_preview.PLAYLIST_SEGMENTS + 1
    recording = probe(target)
    assert float(recording["format"]["duration"]) >= 17.8
    assert {stream["codec_name"] for stream in recording["streams"]} == {"h264", "aac"}
    playlist = directory / "index.m3u8"
    assert {stream["codec_name"] for stream in probe(playlist)["streams"]} == {"h264", "aac"}
    assert "https://" not in playlist.read_text()
    assert str(tmp_path) not in playlist.read_text()
    segment = next(directory.glob("segment_*.ts"))
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(segment), "-f", "null", "-"],
                   capture_output=True, check=True, timeout=20)


@pytest.mark.parametrize("kind,has_video", [("video", True), ("audio", False)])
def test_missing_audio_or_video_does_not_discard_recording(tmp_path, media_fixtures, kind, has_video):
    target = tmp_path / f"live_{kind}.ts"
    completed = mux_fixture(media_fixtures[kind], target)
    assert completed.returncode == 0, completed.stderr.decode()
    assert float(probe(target)["format"]["duration"]) >= 17.8
    status = live_preview.preview_status(live_preview.preview_directory(target))
    assert status["has_video"] is has_video
    assert status["ready"] is has_video
    if not has_video:
        assert "apenas áudio" in status["message"]


def test_preview_output_failure_preserves_full_recording(tmp_path, media_fixtures):
    target = tmp_path / "live_preview_failure.ts"
    directory = live_preview.preview_directory(target)
    directory.mkdir()
    # Refuse a segment write while keeping the recording volume writable.
    (directory / "segment_000000.ts.tmp").mkdir()
    completed = mux_fixture(media_fixtures["av"], target)
    assert completed.returncode == 0, completed.stderr.decode()
    assert float(probe(target)["format"]["duration"]) >= 17.8
    assert live_preview.preview_status(directory)["ready"] is False


@pytest.mark.parametrize("name", ["../recording.ts", "/etc/passwd", "index.m3u8/other", "master.m3u8",
                                  "segment_000000.ts.tmp", "capture.log", "index.m3u8?token=x",
                                  "https://provider/segment.ts", "segment_000000.ts\x00"])
def test_asset_resolution_rejects_non_preview_files(tmp_path, name):
    assert live_preview.resolve_preview_asset(tmp_path, name) is None


def test_asset_resolution_rejects_symlinks_and_accepts_only_existing_segments(tmp_path):
    directory = tmp_path / "preview"
    directory.mkdir()
    (directory / "segment_000000.ts").write_bytes(b"fixture")
    assert live_preview.resolve_preview_asset(directory, "segment_000000.ts") == directory / "segment_000000.ts"
    private = tmp_path / "private.txt"
    private.write_text("private")
    (directory / "segment_000001.ts").symlink_to(private)
    assert live_preview.resolve_preview_asset(directory, "segment_000001.ts") is None
    assert live_preview.resolve_preview_asset(directory, "segment_000002.ts") is None


def test_cleanup_removes_only_known_preview_files(tmp_path):
    directory = tmp_path / "live_preview"
    directory.mkdir()
    for name in ("index.m3u8", "master.m3u8", "segment_000001.ts", "segment_000002.ts.tmp"):
        (directory / name).write_text("fixture")
    (directory / "keep.txt").write_text("other")
    assert live_preview.cleanup_preview(directory) == 4
    assert (directory / "keep.txt").read_text() == "other"
    (directory / "keep.txt").unlink()
    assert live_preview.cleanup_preview(directory) == 0
    assert not directory.exists()


def test_mux_command_uses_copy_and_only_safe_generated_basenames(tmp_path):
    root = tmp_path / "data root: with spaces"
    root.mkdir()
    command = live_preview.build_ffmpeg_command(root / "live_abc.ts")
    assert command[command.index("-c") + 1] == "copy"
    assert "pipe:0" in command
    assert str(root) not in command[-1]
    assert "onfail=ignore" in command[-1]
    with pytest.raises(ValueError):
        live_preview.build_ffmpeg_command(root / "live|other.ts")
    with pytest.raises(ValueError):
        live_preview.build_ffmpeg_command(root / "live.ts", tmp_path / "elsewhere")


def test_stop_finishes_real_pipeline_and_playlist(tmp_path, media_fixtures, monkeypatch):
    original_popen = subprocess.Popen
    calls = []

    def local_capture_command(command, *args, **kwargs):
        if command[0] == "streamlink":
            calls.append(command)
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1",
                       "-i", str(media_fixtures["av"]), "-c", "copy", "-f", "mpegts", "pipe:1"]
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(live_preview.subprocess, "Popen", local_capture_command)
    target = tmp_path / "live_stop.ts"
    with (tmp_path / "capture.log").open("w") as log:
        capture = live_preview.start_capture("https://example.invalid/live", target, log)
        try:
            deadline = time.monotonic() + 15
            while not live_preview.preview_status(capture.preview_dir)["ready"]:
                if capture.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail("Local capture did not publish an HLS preview")
                time.sleep(0.05)
            assert capture.poll() is None
            capture.terminate()
            assert capture.wait(timeout=15) == 0
            assert capture.poll() == 0
            assert live_preview.preview_status(capture.preview_dir)["ended"] is True
            assert float(probe(target)["format"]["duration"]) > 1
        finally:
            if capture.poll() is None:
                capture.kill()
                capture.wait(timeout=5)
    assert len(calls) == 1
    assert "--stdout" in calls[0]


def test_mux_start_failure_reaps_source(tmp_path, monkeypatch):
    original_popen = subprocess.Popen
    source = None

    def fail_mux(command, *args, **kwargs):
        nonlocal source
        if command[0] == "streamlink":
            source = original_popen(["python", "-c", "import time; time.sleep(30)"], **kwargs)
            return source
        raise FileNotFoundError("ffmpeg unavailable")

    monkeypatch.setattr(live_preview.subprocess, "Popen", fail_mux)
    with (tmp_path / "capture.log").open("w") as log:
        with pytest.raises(FileNotFoundError):
            live_preview.start_capture("https://example.invalid/live", tmp_path / "live.ts", log)
    assert source.poll() is not None
