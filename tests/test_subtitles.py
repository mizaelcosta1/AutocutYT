"""Subtitle data safety and actual FFmpeg style/scale regressions."""
from pathlib import Path
import shutil
import subprocess

import pytest
from pydantic import ValidationError

from autoclips import render_clip
from subtitles import (SubtitleStyle, caption_cues, generate_srt, generate_vtt,
                       normalize_subtitle_style, prepared_ass, write_styled_ass)


@pytest.mark.parametrize("value", [
    {"font_family": "DejaVu Sans,Outline=0"},
    {"font_family": "' : movie=/etc/passwd"},
    {"color": "#FF0000,FontSize=300"},
    {"color": "red"}, {"font_size": 17}, {"font_size": 97},
    {"font_size": True}, {"font_size": 56.5}, {"outline": 0},
])
def test_style_rejects_unsupported_values_and_filter_injection(value):
    with pytest.raises(ValidationError):
        normalize_subtitle_style(value)


def test_style_is_canonical_and_uses_explicit_export_canvas():
    assert normalize_subtitle_style() == {
        "font_family": "DejaVu Sans", "font_size": 56, "color": "#FFFFFF"}
    assert normalize_subtitle_style(SubtitleStyle(color="#0aDcff"))["color"] == "#0ADCFF"


def test_clip_boundaries_silence_and_markup_are_portable_tracks(tmp_path):
    words = [
        {"start": .8, "end": 1.2, "text": "Olá"},
        {"start": 1.3, "end": 1.7, "text": "<b>pessoal</b>"},
        {"start": 3., "end": 3.6, "text": "novamente"},
    ]
    srt, vtt = tmp_path / "sample.srt", tmp_path / "sample.vtt"
    assert generate_srt(words, 1., 3.2, srt)
    assert generate_vtt(words, 1., 3.2, vtt)
    assert "00:00:00,000 --> 00:00:00,700" in srt.read_text()
    assert "00:00:02,000 --> 00:00:02,200" in srt.read_text()
    assert "&lt;b&gt;pessoal&lt;/b&gt;" in srt.read_text()
    assert vtt.read_text().startswith("WEBVTT\n\n1\n00:00:00.000")
    assert "&lt;b&gt;pessoal&lt;/b&gt;" in vtt.read_text()
    assert len(caption_cues(words, 1., 3.2)) == 2
    assert generate_srt([], 0., 1., srt) is False
    assert generate_vtt([], 0., 1., vtt) is False
    assert not srt.exists() and not vtt.exists()


def test_ass_escape_and_temp_file_cleanup_even_after_failure(tmp_path):
    srt = tmp_path / "quote's [filename]:test.srt"
    assert generate_srt([{"start": 0, "end": 1, "text": r"{\p1}m 0 0\N"}], 0, 1, srt)
    output = tmp_path / "test.ass"
    write_styled_ass(srt, output, {"color": "#12ABEF", "font_size": 72})
    contents = output.read_text()
    assert "PlayResX: 1080\nPlayResY: 1920" in contents
    assert "DejaVu Sans,72,&H00EFAB12" in contents
    assert r"{\p1}" not in contents and r"\N" not in contents
    assert "｛＼p1｝" in contents
    temporary_name = None
    with pytest.raises(RuntimeError):
        with prepared_ass(srt, tmp_path) as name:
            temporary_name = name
            assert "'" not in name and ":" not in name
            assert (tmp_path / name).is_file()
            raise RuntimeError("failed encoder")
    assert not (tmp_path / temporary_name).exists()


def _colored_bbox(video: Path, width: int, *, red: bool):
    raw = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(video), "-frames:v", "1", "-vf",
        "crop=iw:ih/4:0:ih*3/4", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], capture_output=True, check=True, timeout=30).stdout
    selected = []
    for i, (r, g, b) in enumerate(zip(raw[::3], raw[1::3], raw[2::3])):
        if (r > 150 and g < 70 and b < 70) if red else (r < 70 and g > 150 and b > 150):
            selected.append((i % width, i // width))
    assert len(selected) > 70, "Styled colored text was not burned into the video"
    xs, ys = zip(*selected)
    return (max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)


def test_real_ffmpeg_burns_requested_font_color_and_consistent_relative_size(tmp_path):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg is not installed")
    source = tmp_path / "source.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
        "color=c=black:s=160x90:r=5:d=0.6", "-an", "-c:v", "libx264", str(source),
    ], check=True, timeout=30)
    srt = tmp_path / "quote's [caption].srt"
    generate_srt([{"start": 0., "end": .6, "text": "Legenda"}], 0., .6, srt)
    boxes = []
    for quality, width in (("preview", 720), ("final", 1080)):
        destination = tmp_path / f"{quality}.mp4"
        render_clip(source, destination, {"start": 0., "end": .6},
                    quality=quality, layout="fit", burn=True, srt=srt, has_audio=False,
                    subtitle_style={"font_size": 72, "color": "#FF0000"})
        boxes.append(_colored_bbox(destination, width, red=True))
    assert boxes[1][0] / boxes[0][0] == pytest.approx(1.5, abs=.12)
    assert boxes[1][1] / boxes[0][1] == pytest.approx(1.5, abs=.12)
    alternate = tmp_path / "alternate.mp4"
    render_clip(source, alternate, {"start": 0., "end": .6},
                quality="preview", layout="fit", burn=True, srt=srt, has_audio=False,
                subtitle_style={"font_family": "DejaVu Serif", "font_size": 96,
                                "color": "#00FFFF"})
    larger_box = _colored_bbox(alternate, 720, red=False)
    assert larger_box[1] > boxes[0][1]
    assert not list(tmp_path.glob("autoclips-subs-*.ass"))
