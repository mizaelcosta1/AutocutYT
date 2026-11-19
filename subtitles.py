"""Safe subtitle documents and a single style contract for preview and export.

Font sizes use the full-resolution canvas in the selected orientation. ASS scales
the same document to the preview. SRT and WebVTT remain portable text tracks.
"""
from __future__ import annotations

from contextlib import contextmanager
import html
import math
from pathlib import Path
import re
import tempfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from composition import caption_anchor, normalize_layout_config, output_dimensions

SUBTITLE_FONTS = (
    "DejaVu Sans", "DejaVu Serif", "Liberation Sans", "Liberation Serif", "Noto Sans",
)


class SubtitleStyle(BaseModel):
    """Only allow supported fonts and literal colors, never FFmpeg expressions."""

    model_config = ConfigDict(extra="forbid")
    font_family: Literal[
        "DejaVu Sans", "DejaVu Serif", "Liberation Sans", "Liberation Serif", "Noto Sans"
    ] = "DejaVu Sans"
    font_size: int = Field(default=56, ge=18, le=96, strict=True)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")

    @field_validator("color")
    @classmethod
    def canonical_color(cls, value: str) -> str:
        return value.upper()


def normalize_subtitle_style(value: dict | SubtitleStyle | None = None) -> dict:
    if isinstance(value, SubtitleStyle):
        return value.model_dump()
    return SubtitleStyle.model_validate({} if value is None else value).model_dump()


def fmt_srt(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(total_ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    seconds, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _caption_text(value: object) -> str:
    # A word cannot create a new cue or an ASS section through line breaks.
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value))).strip()


def caption_cues(words: list[dict], start: float, end: float) -> list[dict]:
    """Group short readable phrases, clamping words intersecting an edit edge."""
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
        raise ValueError("Intervalo de legenda invalido")
    chosen = []
    for word in words:
        left, right = float(word["start"]), float(word["end"])
        text = _caption_text(word.get("text", ""))
        if not (math.isfinite(left) and math.isfinite(right)) or right <= left:
            continue
        if text and left < end and right > start:
            chosen.append({"start": max(start, left), "end": min(end, right), "text": text})
    chosen.sort(key=lambda word: (word["start"], word["end"]))
    blocks: list[list[dict]] = []
    pending: list[dict] = []
    for word in chosen:
        if pending and (
            len(pending) >= 3 or pending[-1]["text"].endswith(("!", "?"))
            or word["end"] - pending[0]["start"] > 2.6
            or word["start"] - pending[-1]["end"] > .65
            or sum(len(item["text"]) for item in pending) + len(word["text"]) > 27
        ):
            blocks.append(pending)
            pending = []
        pending.append(word)
    if pending:
        blocks.append(pending)
    cues = [{"start": max(0., block[0]["start"] - start),
             "end": min(end - start, max(word["end"] for word in block) - start),
             "text": " ".join(word["text"] for word in block)} for block in blocks]
    # Whisper timestamps can overlap slightly. Do not display two phrases at once.
    for left, right in zip(cues, cues[1:]):
        left["end"] = min(left["end"], right["start"])
    return [cue for cue in cues if round(cue["end"] * 1000) > round(cue["start"] * 1000)]


def generate_srt(words: list[dict], start: float, end: float, filename: Path) -> bool:
    cues = caption_cues(words, start, end)
    filename = Path(filename)
    if not cues:
        filename.unlink(missing_ok=True)
        return False
    blocks = [f"{index}\n{fmt_srt(cue['start'])} --> {fmt_srt(cue['end'])}\n"
              f"{html.escape(cue['text'], quote=False)}\n"
              for index, cue in enumerate(cues, 1)]
    filename.write_text("\n".join(blocks) + "\n", encoding="utf-8")
    return True


def generate_vtt(words: list[dict], start: float, end: float, filename: Path) -> bool:
    cues = caption_cues(words, start, end)
    filename = Path(filename)
    if not cues:
        filename.unlink(missing_ok=True)
        return False
    blocks = [f"{index}\n{fmt_srt(cue['start']).replace(',', '.')} --> "
              f"{fmt_srt(cue['end']).replace(',', '.')}\n"
              f"{html.escape(cue['text'], quote=False)}\n"
              for index, cue in enumerate(cues, 1)]
    filename.write_text("WEBVTT\n\n" + "\n".join(blocks) + "\n", encoding="utf-8")
    return True


_SRT_TIME = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})$")


def _read_srt_cues(source: Path) -> list[dict]:
    cues = []
    contents = source.read_text(encoding="utf-8-sig").replace("\r\n", "\n").strip()
    for block in re.split(r"\n\s*\n", contents):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines.pop(0)
        if len(lines) < 2 or " --> " not in lines[0]:
            raise ValueError("Arquivo SRT invalido")
        times = []
        for value in lines[0].split(" --> "):
            match = _SRT_TIME.fullmatch(value.strip())
            if not match:
                raise ValueError("Timestamp SRT invalido")
            h, m, s, ms = map(int, match.groups())
            times.append(h * 3600 + m * 60 + s + ms / 1000)
        if len(times) != 2 or times[1] <= times[0]:
            raise ValueError("Intervalo SRT invalido")
        cues.append({"start": times[0], "end": times[1],
                     "text": _caption_text(html.unescape(" ".join(lines[1:])))})
    return cues


def _fmt_ass(seconds: float) -> str:
    total_cs = max(0, round(seconds * 100))
    hours, rest = divmod(total_cs, 360000)
    minutes, rest = divmod(rest, 6000)
    seconds, centis = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def write_styled_ass(source_srt: Path, destination: Path,
                     style: dict | SubtitleStyle | None = None, *, layout_config=None) -> None:
    settings = normalize_subtitle_style(style)
    config = normalize_layout_config(layout_config)
    width, height = output_dimensions("final", config["aspect"])
    anchor = caption_anchor(config, width, height)
    alignment = 8 if config["caption_position"] == "top" else 2
    margin = round(height * 112 / 1920)
    placement = ""
    if anchor is not None:
        alignment = 5
        placement = r"{\an5\pos(%d,%d)}" % anchor
    rgb = settings["color"][1:]
    color = "&H00" + rgb[4:6] + rgb[2:4] + rgb[0:2]
    header = (
        f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{settings['font_family']},{settings['font_size']},{color},"
        f"&H000000FF,&H00181210,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,{alignment},64,64,{margin},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for cue in _read_srt_cues(Path(source_srt)):
        # Braces and backslashes introduce ASS commands. Full-width literal
        # equivalents preserve readable punctuation without executing overrides.
        literal = cue["text"].translate(str.maketrans({"{": "｛", "}": "｝", "\\": "＼"}))
        events.append(f"Dialogue: 0,{_fmt_ass(cue['start'])},{_fmt_ass(cue['end'])},"
                      f"Default,,0,0,0,,{placement}{literal}\n")
    Path(destination).write_text(header + "".join(events), encoding="utf-8")


@contextmanager
def prepared_ass(source_srt: Path | None, directory: Path,
                 style: dict | SubtitleStyle | None = None, *, layout_config=None):
    """Yield a controlled basename; no user path is interpolated into FFmpeg."""
    normalize_subtitle_style(style)
    if source_srt is None:
        yield None
        return
    with tempfile.NamedTemporaryFile(prefix="autoclips-subs-", suffix=".ass",
                                     dir=str(directory), delete=False) as file:
        destination = Path(file.name)
    try:
        write_styled_ass(source_srt, destination, style, layout_config=layout_config)
        yield destination.name
    finally:
        destination.unlink(missing_ok=True)
