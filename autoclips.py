#!/usr/bin/env python3
"""AutoClips AI Engine v0.5.0: authorized ingest, highlight proposals and approved video rendering.

Runs on Python 3.10+ with FFmpeg / ffprobe. Optional yt-dlp, Streamlink and faster-whisper.
The original video is never modified.
"""
from __future__ import annotations

import argparse
import array
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import unicodedata
from urllib.request import Request, urlopen
from urllib.error import URLError

from composition import build_composition_graph, normalize_layout_config, output_dimensions
from subtitles import fmt_srt, generate_srt, generate_vtt, prepared_ass

MEDIA_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".ts", ".m4v"}
CUE_WEIGHTS = {
    "olha isso": 4, "nao acredito": 5, "meu deus": 5, "que isso": 3,
    "inacreditavel": 4, "caramba": 3, "sensacional": 3, "surreal": 3,
    "aconteceu": 2, "revelacao": 3, "surpresa": 3, "segredo": 3,
    "consegui": 3, "vitoria": 4, "ganhamos": 3, "zeramos": 4,
    "perdi": 2, "morri": 2, "finalmente": 2, "importante": 2,
    "urgente": 3, "polemica": 3, "absurdo": 3, "cuidado": 2,
    "hahaha": 3, "risos": 2, "engracado": 2, "insano": 4, "impossivel": 4,
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def run(args: list[str], *, cwd: Path | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    p = subprocess.run(args, cwd=str(cwd) if cwd else None, text=True, capture_output=True,
                       timeout=timeout, check=False)
    if p.returncode:
        raise RuntimeError(f"Comando falhou ({p.returncode}): {' '.join(args[:4])}\n{p.stderr[-3000:]}")
    return p


def require_executables(*names: str) -> None:
    missing = [x for x in names if shutil.which(x) is None]
    if missing:
        raise RuntimeError("Dependencias ausentes: " + ", ".join(missing))


def probe(path: Path) -> dict:
    require_executables("ffprobe")
    p = run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(path)])
    data = json.loads(p.stdout)
    video = next((x for x in data.get("streams", []) if x.get("codec_type") == "video"), None)
    audio = next((x for x in data.get("streams", []) if x.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError(f"Arquivo sem trilha de video: {path}")
    raw_duration = data.get("format", {}).get("duration") or video.get("duration")
    if not raw_duration or float(raw_duration) <= 0:
        raise ValueError("Duracao nao disponivel; finalize a gravacao antes de analisar.")
    return {
        "duration": float(raw_duration), "width": int(video.get("width", 0)),
        "height": int(video.get("height", 0)),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "fps": video.get("avg_frame_rate"),
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def workspace(src: Path, digest: str) -> Path:
    return src.parent / "edit" / f"{src.stem[:48]}-{digest[:8]}"


def write_json(path: Path, obj: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_words(obj: dict, duration: float) -> list[dict]:
    if not isinstance(obj.get("words"), list):
        raise ValueError('Transcricao deve ter objeto {"words": [{"start":0,"end":1,"text":"..."}]}')
    out = []
    for w in obj["words"]:
        start, end = float(w["start"]), float(w["end"])
        if not (0 <= start < end <= duration + 0.25) or start >= duration:
            raise ValueError(f"Timestamp invalido para palavra: {w}")
        if not str(w.get("text", "")).strip():
            continue
        out.append({"start": round(start, 3), "end": round(min(duration, end), 3),
                    "text": str(w["text"]).strip()})
    out.sort(key=lambda w: (w["start"], w["end"]))
    for left, right in zip(out, out[1:]):
        if left["start"] > right["start"]:
            raise ValueError("Transcricao desordenada")
    return out


def transcript_local(src: Path, model_name: str | None = None, *,
                     language: str | None = None, progress=None) -> dict:
    """Compatibility entry point for the bounded-memory local transcriber."""
    from transcription import transcribe_local
    return transcribe_local(src, model_name, language=language, progress=progress)


def audio_rms_by_second(src: Path, max_seconds: int | None = None) -> list[float]:
    require_executables("ffmpeg")
    cmd = ["ffmpeg", "-v", "error", "-i", str(src), "-vn", "-map", "0:a:0", "-ac", "1",
           "-ar", "2000"]
    if max_seconds is not None:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-f", "s16le", "-acodec", "pcm_s16le", "pipe:1"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    energy: list[float] = []
    assert p.stdout is not None
    try:
        while True:
            samples_bytes = p.stdout.read(4000)
            if not samples_bytes:
                break
            samples_bytes = samples_bytes[:len(samples_bytes) - (len(samples_bytes) % 2)]
            samples = array.array("h")
            samples.frombytes(samples_bytes)
            if sys.byteorder != "little":
                samples.byteswap()
            if samples:
                rms = math.sqrt(sum(int(x) * int(x) for x in samples) / len(samples))
                energy.append(20 * math.log10(max(rms, 0.1) / 32768.0))
    finally:
        p.stdout.close()
    if p.stderr:
        err = p.stderr.read().decode("utf-8", errors="replace")
        p.stderr.close()
    else:
        err = ""
    p.wait()
    if p.returncode:
        raise RuntimeError("Nao consegui analisar audio: " + err[-600:])
    return energy


def volume_bonus(energy: list[float], start: float, end: float) -> tuple[float, str]:
    if not energy:
        return 0.0, "sem audio"
    selected = energy[max(0, int(start)):max(int(start) + 1, int(math.ceil(end)))]
    if not selected:
        return 0.0, "sem janela de audio"
    baseline = statistics.median(energy)
    p90 = sorted(selected)[min(len(selected) - 1, int(len(selected) * .9))]
    delta = p90 - baseline
    return max(0.0, min(12.0, (delta - 2.0) * 1.8)), f"pico audio {delta:+.1f} dB"


def candidate_score(words: list[dict], energy: list[float], start: float, end: float) -> tuple[float, str, str]:
    chosen = [w for w in words if w["start"] >= start - 0.02 and w["end"] <= end + 0.02]
    transcript = " ".join(w["text"] for w in chosen).strip()
    text = norm(transcript)
    matched = [(cue, weight) for cue, weight in CUE_WEIGHTS.items()
               if re.search(r"(?<!\w)" + re.escape(cue) + r"(?!\w)", text)]
    cue_score = min(35.0, sum(w for _, w in matched) * 4.0)
    punctuation = min(9, transcript.count("!") * 3 + transcript.count("?") * 2)
    vocal = min(16.0, len(chosen) / max(1.0, end - start) * 6.0)
    loudness, loudness_note = volume_bonus(energy, start, end)
    score = round(cue_score + punctuation + vocal + loudness, 2)
    cues = ", ".join(c for c, _ in matched) if matched else "fala continua"
    reason = f"{cues}; {loudness_note}"
    return score, reason, transcript[:400]


def non_overlapping(proposals: list[dict], max_clips: int) -> list[dict]:
    chosen = []
    for p in sorted(proposals, key=lambda c: (c["score"], -c["start"]), reverse=True):
        if len(chosen) >= max_clips:
            break
        if any(max(0, min(p["end"], c["end"]) - max(p["start"], c["start"])) /
               max(.01, min(p["end"] - p["start"], c["end"] - c["start"])) > .40
               for c in chosen):
            continue
        chosen.append(dict(p))
    return sorted(chosen, key=lambda c: c["start"])


def propose(words: list[dict], energy: list[float], duration: float,
            max_clips: int, target: int) -> list[dict]:
    if duration <= 0:
        return []
    proposals: list[dict] = []
    if words:
        anchor = -1e6
        starts = []
        for idx, w in enumerate(words):
            is_sentence_start = idx == 0 or words[idx - 1]["text"].endswith((".", "!", "?"))
            min_step = max(4, target // 3) if is_sentence_start else max(6, target // 2)
            if w["start"] >= anchor + min_step:
                starts.append(w["start"])
                anchor = w["start"]
        for word_start in starts:
            end_goal = min(duration, word_start + target)
            end_words = [w for w in words if word_start <= w["start"] <= end_goal]
            if not end_words:
                continue
            first, last = end_words[0], end_words[-1]
            start, end = max(0., first["start"] - .15), min(duration, last["end"] + .15)
            if end - start < min(8., duration * .50):
                continue
            score, reason, excerpt = candidate_score(words, energy, start, end)
            proposals.append({"start": round(start, 3), "end": round(end, 3),
                              "score": score, "reason": reason,
                              "excerpt": excerpt, "method": "transcript+audio-heuristic"})
    if not proposals:
        centers = list(range(0, max(1, int(duration)), max(8, target // 2)))
        for center in centers:
            start = max(0., min(duration - min(target, duration), center - target / 2))
            end = min(duration, start + target)
            volume, reason = volume_bonus(energy, start, end)
            proposals.append({"start": round(start, 3), "end": round(end, 3),
                              "score": round(volume, 2), "reason": reason,
                              "excerpt": "", "method": "audio-only-low-confidence"})
    chosen = non_overlapping(proposals, max_clips)
    for i, clip in enumerate(chosen, start=1):
        clip["id"] = f"clip_{i:02d}"
    return chosen


def _extract_json_object(raw: str) -> dict:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        cleaned = match.group(0)
    return json.loads(cleaned)


def rerank_ollama(proposals: list[dict], model: str) -> list[dict]:
    for p in proposals:
        if not p["excerpt"]:
            continue
        prompt = (
            "Voce avalia momentos de videos de modo fiel, sem distorcer contexto. "
            "Avalie 0-10 quao interessante seria o trecho como video curto. "
            "Priorize surpresa real, comeco compreensivel e sentido completo. "
            'Responda somente JSON: {"score": numero, "reason": texto}.\n\n'
            "Trecho: " + p["excerpt"]
        )
        body = json.dumps({"model": model, "stream": False, "format": "json",
                           "prompt": prompt, "options": {"temperature": 0}}).encode()
        request = Request("http://127.0.0.1:11434/api/generate", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=180) as response:
                result = json.loads(response.read())
            parsed = _extract_json_object(result["response"])
            semantic = max(0., min(10., float(parsed["score"])))
            p["score"] = round(p["score"] + semantic * 5, 2)
            p["reason"] += "; Ollama: " + str(parsed.get("reason", ""))[:200]
            p["method"] = "local-llm+transcript+audio"
        except (URLError, KeyError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ollama local indisponivel ou resposta invalida.") from exc
    return proposals


def analyze(src: Path, transcript_json: Path | None, model_name: str | None,
            clips: int, target: int, ollama: str | None = None,
            remote_reranker=None) -> Path:
    src = src.expanduser().resolve(strict=True)
    if src.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError("Formato de video nao reconhecido: " + src.suffix)
    data = probe(src)
    digest = sha256_file(src)
    job = workspace(src, digest)
    job.mkdir(parents=True, exist_ok=True)
    transcript_path = job / "transcript.json"
    if transcript_json:
        transcript_data = json.loads(transcript_json.read_text(encoding="utf-8"))
        transcript_data.setdefault("engine", "user-supplied")
    elif transcript_path.exists():
        transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
        # An audio-only analysis stores a placeholder. Enabling transcription
        # later must replace it rather than silently keep captions disabled.
        if model_name and transcript_data.get("engine") == "none":
            transcript_data = transcript_local(src, model_name)
    elif model_name:
        transcript_data = transcript_local(src, model_name)
    else:
        transcript_data = {"words": [], "engine": "none", "notice": "Somente deteccao de picos no audio."}
    words = load_words(transcript_data, data["duration"])
    transcript_data["words"] = words
    write_json(transcript_path, transcript_data)
    energy = audio_rms_by_second(src) if data["audio_codec"] else []
    proposed = propose(words, energy, data["duration"], max(clips * 4, 12), target)
    if ollama:
        proposed = rerank_ollama(proposed, ollama)
    if remote_reranker:
        proposed = remote_reranker(proposed)
    proposed = non_overlapping(proposed, clips)
    for i, clip in enumerate(proposed, start=1):
        clip["id"] = f"clip_{i:02d}"
        clip["status"] = "proposed_only"
    manifest = {
        "version": "0.5.0", "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(src), "sha256": digest, "probe": data,
        "transcription_engine": transcript_data["engine"],
        "ranking": ("remote LLM + heuristic" if remote_reranker else
                    "local LLM + heuristic" if ollama else "heuristic (not virality prediction)"),
        "review_required": True, "clips": proposed,
        "notices": ["Video original preservado.", "Confirme selecao/contexto antes de previas e finais."]
    }
    write_json(job / "manifest.json", manifest)
    return job


def render_clip(src: Path, dst: Path, clip: dict, *, quality: str,
                layout: str, burn: bool, srt: Path | None, has_audio: bool,
                subtitle_style=None, layout_config=None) -> dict:
    config = normalize_layout_config(layout_config)
    width, height = output_dimensions(quality, config["aspect"])
    graph = build_composition_graph(layout, config, width, height)
    require_executables("ffmpeg", "ffprobe")
    src, dst = Path(src).resolve(), Path(dst)
    if dst.is_symlink():
        raise ValueError("O destino da exportação não pode ser um link simbólico.")
    dst = dst.resolve()
    if src == dst:
        raise ValueError("A exportação não pode substituir o vídeo original.")
    # Guard ordinary source replacement/edits while FFmpeg reads it without
    # another full-media copy on the small recording volume. Timeline exports
    # additionally validate their stored SHA-256 before publishing the join.
    def source_revision():
        stat = src.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    revision = source_revision()
    dst.parent.mkdir(parents=True, exist_ok=True)
    ass_width, ass_height = output_dimensions("final", config["aspect"])
    # Every file owned by this attempt lives in its own sibling directory.
    # An existing approved dst is untouched until all validations pass. SRT is
    # caller-owned input and must never be deleted by this renderer.
    with tempfile.TemporaryDirectory(prefix="autoclips-render-", dir=str(dst.parent)) as temp_name:
        staging = Path(temp_name)
        pending = staging / "render.mp4"
        with prepared_ass(srt if burn else None, staging, subtitle_style, layout_config=config) as ass_name:
            if ass_name:
                graph += f";[base]ass=filename='{ass_name}':original_size={ass_width}x{ass_height}[v]"
            else:
                graph += ";[base]null[v]"
            duration = max(.1, clip["end"] - clip["start"])
            cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                   "-ss", str(clip["start"]), "-i", str(src), "-t", str(duration),
                   "-filter_complex_threads", "2", "-filter_complex", graph, "-map", "[v]"]
            if has_audio:
                cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-af", "aresample=async=1:first_pts=0"]
            else:
                cmd += ["-an"]
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(pending)]
            run(cmd, cwd=staging)
        meta = probe(pending)
        if meta["width"] != width or meta["height"] != height:
            raise RuntimeError("Resolucao divergente em " + str(dst))
        if abs(meta["duration"] - duration) > 1.5:
            raise RuntimeError("Duracao divergente em " + str(dst))
        run(["ffmpeg", "-v", "error", "-xerror", "-i", str(pending), "-f", "null", "-"])
        try:
            unchanged = source_revision() == revision
        except FileNotFoundError:
            unchanged = False
        if not unchanged:
            raise ValueError("O vídeo original mudou durante a exportação.")
        pending.replace(dst)
    return {**meta, "layout_config": config, "output_width": width, "output_height": height}


def download_vod(url: str, folder: Path) -> Path:
    require_executables("yt-dlp", "ffprobe")
    if not url.startswith(("https://", "http://")):
        raise ValueError("Use uma URL HTTP(S) autorizada.")
    folder.mkdir(parents=True, exist_ok=True)
    unique = "vod_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    template = str(folder / (unique + ".%(ext)s"))
    run([
        "yt-dlp",
        "--no-playlist",
        "--format", "bv*[vcodec^=avc1][ext=mp4]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", template,
        url,
    ])
    candidates = list(folder.glob(unique + ".*"))
    valid = [x for x in candidates if x.suffix.lower() in MEDIA_EXTENSIONS]
    if len(valid) != 1:
        raise RuntimeError("Nao foi possivel determinar o video baixado: " + str(candidates))
    downloaded = valid[0]
    if downloaded.suffix.lower() != ".mp4" and shutil.which("ffmpeg"):
        mp4_target = downloaded.with_suffix(".mp4")
        run(["ffmpeg", "-v", "error", "-y", "-i", str(downloaded),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(mp4_target)])
        downloaded.unlink(missing_ok=True)
        downloaded = mp4_target
    probe(downloaded)
    return downloaded
