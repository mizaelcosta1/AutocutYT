"""Failure boundaries must not publish partial media or erase approved exports.

FFmpeg failures are injected after creating partial output. Existing media,
caller-owned captions and unrelated files must survive every failed attempt.
Real codec/render validation is covered by test_subtitles/test_timeline_export.
"""
from pathlib import Path
from fractions import Fraction
import json
import shutil
import subprocess

import pytest

import autoclips
import timeline_export
from timeline import new_timeline


def fixture_files(tmp_path, previous):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original footage")
    destination = tmp_path / "approved.mp4"
    if previous:
        destination.write_bytes(b"approved export")
    captions = tmp_path / "caller-captions.srt"
    captions.write_text("1\n00:00:00,100 --> 00:00:00,900\nOlá\n", encoding="utf-8")
    unrelated = tmp_path / "keep.ass"
    unrelated.write_text("unrelated subtitle file", encoding="utf-8")
    return source, destination, captions, unrelated


def assert_previous(destination, previous):
    if previous:
        assert destination.read_bytes() == b"approved export"
    else:
        assert not destination.exists()


@pytest.mark.parametrize("previous", [False, True], ids=["new-destination", "approved-destination"])
@pytest.mark.parametrize("failure", ["encode", "timeout", "probe", "duration", "decode", "source", "publish"])
def test_clip_failure_cleans_only_its_attempt(tmp_path, monkeypatch, previous, failure):
    source, destination, captions, unrelated = fixture_files(tmp_path, previous)
    original_names = {item.name for item in tmp_path.iterdir()}
    original_captions = captions.read_bytes()
    monkeypatch.setattr(autoclips, "require_executables", lambda *_: None)

    def fake_run(args, **kwargs):
        assert_previous(destination, previous)
        if "-filter_complex" in args:
            partial = Path(args[-1])
            assert partial != destination
            assert partial.parent.parent == destination.parent
            # ASS is prepared beside the candidate, not beside a prior export.
            assert list(partial.parent.glob("*.ass"))
            partial.write_bytes(b"partial encoded media")
            if failure == "encode":
                raise OSError(28, "No space left on device")
            if failure == "timeout":
                raise subprocess.TimeoutExpired("ffmpeg", 1)
            if failure == "source":
                source.write_bytes(b"changed source while encoding")
        elif failure == "decode":
            raise RuntimeError("invalid packet during full decode")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_probe(_):
        assert_previous(destination, previous)
        return {"width": 1 if failure == "probe" else 720, "height": 1280,
                "duration": 9.0 if failure == "duration" else 1.0, "audio_codec": None}

    monkeypatch.setattr(autoclips, "run", fake_run)
    monkeypatch.setattr(autoclips, "probe", fake_probe)
    if failure == "publish":
        original_replace = Path.replace
        def fail_publication(path, target):
            if path.name == "render.mp4":
                raise OSError(28, "No space left on device")
            return original_replace(path, target)
        monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises((RuntimeError, ValueError, OSError, subprocess.TimeoutExpired)):
        autoclips.render_clip(source, destination, {"start": 0, "end": 1},
                              quality="preview", layout="fit", burn=True,
                              srt=captions, has_audio=False)
    assert_previous(destination, previous)
    assert captions.read_bytes() == original_captions
    assert unrelated.read_text() == "unrelated subtitle file"
    assert {item.name for item in tmp_path.iterdir()} == original_names


def test_clip_publishes_new_media_only_after_all_checks(tmp_path, monkeypatch):
    source, destination, captions, _ = fixture_files(tmp_path, True)
    calls = []
    monkeypatch.setattr(autoclips, "require_executables", lambda *_: None)

    def fake_run(args, **kwargs):
        assert destination.read_bytes() == b"approved export"
        if "-filter_complex" in args:
            Path(args[-1]).write_bytes(b"validated new export")
            calls.append("encode")
        else:
            calls.append("decode")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_probe(_):
        assert destination.read_bytes() == b"approved export"
        calls.append("probe")
        return {"width": 720, "height": 1280, "duration": 1.0, "audio_codec": None}

    monkeypatch.setattr(autoclips, "run", fake_run)
    monkeypatch.setattr(autoclips, "probe", fake_probe)
    autoclips.render_clip(source, destination, {"start": 0, "end": 1},
                          quality="preview", layout="fit", burn=True,
                          srt=captions, has_audio=False)
    assert calls == ["encode", "probe", "decode"]
    assert destination.read_bytes() == b"validated new export"
    assert source.read_bytes() == b"original footage"
    assert captions.exists()
    assert not list(tmp_path.glob("autoclips-render-*"))
    assert sorted(path.name for path in tmp_path.glob("*.ass")) == ["keep.ass"]


@pytest.mark.parametrize("previous", [False, True], ids=["new-destination", "approved-destination"])
@pytest.mark.parametrize("failure", ["part", "concat", "probe", "decode", "source", "publish"])
def test_timeline_failure_cleans_parts_and_generated_captions(tmp_path, monkeypatch, previous, failure):
    source, destination, captions, unrelated = fixture_files(tmp_path, previous)
    original_names = {item.name for item in tmp_path.iterdir()}
    original_captions = captions.read_bytes()
    document = new_timeline("a" * 32, 1, autoclips.sha256_file(source))

    def fake_part(_source, part, _clip, **kwargs):
        assert_previous(destination, previous)
        assert kwargs["srt"].is_file()
        part.write_bytes(b"partial video part")
        # Even an interrupted nested renderer's leftover is scoped to this
        # timeline attempt and must disappear with its temporary directory.
        (part.parent / "interrupted.ass").write_text("temporary subtitle")
        if failure == "part":
            raise OSError(28, "No space left on device")
        return {}

    def fake_run(args, **kwargs):
        assert_previous(destination, previous)
        if "concat" in args:
            pending = Path(args[-1])
            assert pending != destination
            pending.write_bytes(b"partial joined video")
            if failure == "concat":
                raise OSError(28, "No space left on device")
            if failure == "source":
                source.write_bytes(b"source replaced during join")
        elif failure == "decode":
            raise RuntimeError("bad packet in final media")
        return subprocess.CompletedProcess(args, 0, "", "")

    def fake_probe(path):
        assert_previous(destination, previous)
        return {"width": 1 if failure == "probe" and path != source else 720,
                "height": 1280, "duration": 1.0, "audio_codec": None}

    monkeypatch.setattr(timeline_export, "render_clip", fake_part)
    monkeypatch.setattr(timeline_export, "run", fake_run)
    monkeypatch.setattr(timeline_export, "probe", fake_probe)
    if failure == "publish":
        original_replace = Path.replace
        def fail_publication(path, target):
            if path.name == "joined.mp4":
                raise OSError(28, "No space left on device")
            return original_replace(path, target)
        monkeypatch.setattr(Path, "replace", fail_publication)

    with pytest.raises((RuntimeError, ValueError, OSError)):
        timeline_export.render_project(source, destination, document,
            words=[{"start": .1, "end": .9, "text": "Olá"}],
            quality="preview", layout="fit", subtitles=True)
    assert_previous(destination, previous)
    assert captions.read_bytes() == original_captions
    assert unrelated.read_text() == "unrelated subtitle file"
    assert {item.name for item in tmp_path.iterdir()} == original_names


@pytest.mark.parametrize("renderer", ["clip", "timeline"])
@pytest.mark.parametrize("alias", ["source", "symlink"])
def test_renderer_rejects_destinations_that_could_replace_the_original(tmp_path, monkeypatch, renderer, alias):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"irreplaceable footage")
    destination = source
    if alias == "symlink":
        destination = tmp_path / "alias.mp4"
        destination.symlink_to(source)
    monkeypatch.setattr(autoclips, "require_executables", lambda *_: None)
    if renderer == "clip":
        with pytest.raises(ValueError, match="original|simbólico"):
            autoclips.render_clip(source, destination, {"start": 0, "end": 1},
                quality="preview", layout="fit", burn=False, srt=None, has_audio=False)
    else:
        document = new_timeline("a" * 32, 1, autoclips.sha256_file(source))
        with pytest.raises(ValueError, match="original|simbólico"):
            timeline_export.render_project(source, destination, document, words=[],
                quality="preview", layout="fit", subtitles=False)
    assert source.read_bytes() == b"irreplaceable footage"
    assert not list(tmp_path.glob("autoclips-*"))


def assert_bounded_video_pipeline(command):
    """Input decoder and output encoder options have separate FFmpeg scopes."""
    input_index = command.index("-i")
    thread_indices = [index for index, value in enumerate(command) if value == "-threads:v"]
    assert len(thread_indices) == 2
    assert thread_indices[0] < input_index < thread_indices[1]
    assert all(1 <= int(command[index + 1]) <= 2 for index in thread_indices)
    for flag in ("-filter_threads", "-filter_complex_threads"):
        assert int(command[command.index(flag) + 1]) == 1


def test_render_and_full_decode_bound_decoder_encoder_and_filter_threads(tmp_path, monkeypatch):
    source, destination, captions, _ = fixture_files(tmp_path, True)
    commands = []
    monkeypatch.setattr(autoclips, "require_executables", lambda *_: None)
    monkeypatch.setattr(autoclips, "probe", lambda _: {
        "width": 1080, "height": 1920, "duration": 1.0, "audio_codec": "aac"})

    def fake_run(args, **kwargs):
        commands.append(args)
        if "-filter_complex" in args:
            Path(args[-1]).write_bytes(b"validated new export")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(autoclips, "run", fake_run)
    autoclips.render_clip(source, destination, {"start": 0, "end": 1},
        quality="final", layout="fit", burn=True, srt=captions, has_audio=True)
    assert len(commands) == 2
    for command in commands:
        assert_bounded_video_pipeline(command)
        assert "-r" not in command
    encoder = commands[0]
    assert encoder[encoder.index("-preset") + 1] == "veryfast"
    assert encoder[encoder.index("-crf") + 1] == "23"
    assert encoder.index("-c:v") < [i for i, value in enumerate(encoder) if value == "-threads:v"][1]


def test_timeline_full_decode_obeys_same_thread_limits(tmp_path, monkeypatch):
    source, destination, _, _ = fixture_files(tmp_path, True)
    document = new_timeline("a" * 32, 1, autoclips.sha256_file(source))
    commands = []
    monkeypatch.setattr(timeline_export, "probe", lambda _: {
        "width": 720, "height": 1280, "duration": 1.0, "audio_codec": None})
    monkeypatch.setattr(timeline_export, "render_clip", lambda _source, part, *_args, **_kw: part.write_bytes(b"part"))

    def fake_run(args, **kwargs):
        commands.append(args)
        if "concat" in args:
            Path(args[-1]).write_bytes(b"joined media")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(timeline_export, "run", fake_run)
    timeline_export.render_project(source, destination, document, words=[],
        quality="preview", layout="fit", subtitles=False)
    assert len(commands) == 2
    assert_bounded_video_pipeline(commands[-1])


@pytest.mark.parametrize("returncode", [-9, 1])
def test_run_preserves_returncode_without_asserting_an_unverified_cause(monkeypatch, returncode):
    monkeypatch.setattr(autoclips.subprocess, "run", lambda args, **kwargs:
        subprocess.CompletedProcess(args, returncode, "", ""))
    with pytest.raises(autoclips.CommandError) as failure:
        autoclips.run(["/usr/bin/ffmpeg", "-hide_banner", "-loglevel", "error"])
    assert isinstance(failure.value, RuntimeError)
    assert failure.value.returncode == returncode
    assert failure.value.program == "ffmpeg"
    assert "OOM" not in str(failure.value)


def test_real_split_caption_final_keeps_1080p60_and_audio(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are required for real media validation")
    source = tmp_path / "source1080p60.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-filter_threads", "1", "-filter_complex_threads", "1",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=60",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
        "-t", "1", "-c:v", "libx264", "-threads:v", "2", "-preset", "ultrafast",
        "-c:a", "aac", "-threads:a", "1", "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True, timeout=30)
    captions = tmp_path / "captions.srt"
    autoclips.generate_srt([{"start": .1, "end": .9, "text": "Transmissão em 60 FPS"}], 0., 1., captions)
    config = {"mode": "split_caption", "aspect": "9:16", "game_fit": "cover",
              "face": {"x": .07, "y": .073, "w": .136, "h": .207},
              "gameplay": {"x": 0., "y": .28, "w": 1., "h": .72}}
    destination = tmp_path / "final.mp4"
    result = autoclips.render_clip(source, destination, {"start": 0., "end": 1.},
        quality="final", layout="fit", burn=True, srt=captions, has_audio=True,
        layout_config=config,
        subtitle_style={"font_family": "Liberation Sans", "font_size": 52, "color": "#00FFFF"})
    assert (result["width"], result["height"]) == (1080, 1920)
    assert Fraction(result["fps"]) == 60
    assert result["audio_codec"] == "aac"
    assert result["video_codec"] == "h264"
    assert result["duration"] == pytest.approx(1., abs=.05)
    details = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-threads:v", "2", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "json", str(destination)], timeout=20))
    assert int(details["streams"][0]["nb_read_frames"]) == 60
    assert not list(tmp_path.glob("autoclips-render-*"))
