"""FFmpeg renderer for AutoClips sequential edits, reusing our existing video engine.

Exports independently rendered fragments with uniform codecs, then concat-demuxes
without changing the source footage. Does not contain OpenChatCut source code.
"""
from __future__ import annotations

from pathlib import Path
import tempfile

from autoclips import generate_srt, probe, render_clip, run, sha256_file
from subtitles import normalize_subtitle_style
from timeline import total_duration


def render_project(source: Path, destination: Path, document: dict, *, words: list[dict],
                   quality: str, layout: str, subtitles: bool, subtitle_style=None) -> dict:
    if quality not in {"preview", "final"} or layout not in {"blur", "fit", "crop"}:
        raise ValueError("Configuracao de render invalida")
    source = Path(source)
    destination = Path(destination)
    if not source.exists() or sha256_file(source) != document["source_sha256"]:
        raise ValueError("O video original foi alterado; recrie o projeto")
    info = probe(source)
    edited_duration = total_duration(document)
    if not 0.1 <= edited_duration <= 600:
        raise ValueError("Duracao de timeline invalida")
    if not 1 <= len(document["segments"]) <= 80:
        raise ValueError("Numero de segmentos invalido")
    has_audio = bool(info["audio_codec"])
    subtitle_style = normalize_subtitle_style(subtitle_style)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="autoclips-parts-", dir=str(destination.parent)) as temp_name:
        temp = Path(temp_name)
        parts = []
        for i, segment in enumerate(document["segments"]):
            part = temp / f"part_{i:03d}.mp4"
            srt = temp / f"part_{i:03d}.srt"
            has_subs = subtitles and generate_srt(words, segment["source_in"], segment["source_out"], srt)
            render_clip(source, part,
                        {"start": segment["source_in"], "end": segment["source_out"]},
                        quality=quality, layout=layout, burn=bool(has_subs),
                        srt=srt if has_subs else None, has_audio=has_audio,
                        subtitle_style=subtitle_style)
            parts.append(part.name)
        concat_file = temp / "concat.txt"
        concat_file.write_text("".join(f"file '{name}'\n" for name in parts), encoding="utf-8")
        pending = temp / "joined.mp4"
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-f", "concat", "-safe", "0", "-i", str(concat_file),
             "-c", "copy", "-movflags", "+faststart", str(pending)], timeout=180)
        rendered = probe(pending)
        expected_width, expected_height = (720, 1280) if quality == "preview" else (1080, 1920)
        if rendered["width"] != expected_width or rendered["height"] != expected_height:
            raise ValueError("Resolucao gerada nao corresponde ao perfil")
        if has_audio and not rendered["audio_codec"]:
            raise ValueError("Audio original desapareceu na montagem")
        if abs(rendered["duration"] - edited_duration) > 1.1:
            raise ValueError("Duracao divergente da timeline")
        run(["ffmpeg", "-v", "error", "-xerror", "-i", str(pending), "-f", "null", "-"], timeout=180)
        pending.replace(destination)
    return {"version": document["version"], "quality": quality, "layout": layout,
            "subtitles": subtitles, "subtitle_style": subtitle_style,
            "source_sha256": document["source_sha256"],
            "path": str(destination), "probe": rendered}
