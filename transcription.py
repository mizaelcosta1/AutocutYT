"""Local, bounded-memory transcription for completed recordings.

One CPU int8 Whisper instance is reused and inference is serialized. Audio is
decoded into short temporary mono WAV chunks, not a full live recording in RAM.
Model downloads use a separate cache, never the persistent media volume.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
from typing import Callable
import wave

ProgressCallback = Callable[[dict], None]
_INFERENCE_LOCK = threading.Lock()
_MODEL = None
_MODEL_KEY = None
CHUNK_SECONDS = 180
CHUNK_OVERLAP = 1.0


def default_model_name() -> str:
    return os.environ.get("AUTOCLIPS_WHISPER_MODEL", "tiny").strip() or "tiny"


def model_cache_dir() -> Path:
    return Path(os.environ.get("AUTOCLIPS_WHISPER_CACHE", "")
                or str(Path(tempfile.gettempdir()) / "autoclips-whisper")).expanduser()


def _notify(callback: ProgressCallback | None, phase: str, progress: float,
            message: str, **details) -> None:
    if callback:
        callback({"phase": phase, "progress": round(max(0., min(100., progress)), 1),
                  "message": message, **details})


def _audio_duration(source: Path) -> float:
    completed = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
        "stream=duration:format=duration", "-of", "json", str(source),
    ], capture_output=True, text=True, check=False, timeout=30)
    if completed.returncode:
        raise RuntimeError("Nao foi possivel ler o audio da gravacao.")
    data = json.loads(completed.stdout)
    streams = data.get("streams", [])
    if not streams:
        raise ValueError("O video nao contem uma faixa de audio para transcrever.")
    raw = data.get("format", {}).get("duration") or streams[0].get("duration")
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("Finalize a gravacao antes de transcrever.") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Finalize a gravacao antes de transcrever.")
    return duration


def _extract_chunk(source: Path, destination: Path, offset: float, duration: float) -> bool:
    completed = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-ss", f"{offset:.3f}", "-i", str(source), "-t", f"{duration:.3f}",
        "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
        # Retain leading silence when an audio track starts after the video.
        # Otherwise Whisper word timestamps would be early by that offset.
        "-af", "aresample=async=1:first_pts=0,apad",
        "-c:a", "pcm_s16le", str(destination),
    ], capture_output=True, text=True, check=False, timeout=max(60, int(duration * 2)))
    if completed.returncode:
        raise RuntimeError("Nao foi possivel preparar um trecho de audio para transcricao.")
    with wave.open(str(destination)) as audio:
        return audio.getnframes() > 0


def _merge_chunk_words(words: list[dict], incoming: list[dict]) -> None:
    for word in incoming:
        if words:
            previous = words[-1]
            same_text = previous["text"].casefold().strip(".,!?;:") == word["text"].casefold().strip(".,!?;:")
            overlap = min(previous["end"], word["end"]) - max(previous["start"], word["start"])
            shorter = min(previous["end"] - previous["start"], word["end"] - word["start"])
            if same_text and overlap > shorter * .5:
                # Independent chunks can place a boundary word a few ms apart.
                # Collapse overlapping duplicates, preserving real repetitions.
                previous["start"] = min(previous["start"], word["start"])
                previous["end"] = max(previous["end"], word["end"])
                continue
        words.append(word)


def _read_audio_chunk(path: Path):
    """Read only our bounded PCM chunk, without decoding through PyAV again."""
    import numpy as np
    with wave.open(str(path)) as audio:
        if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth()) != (1, 16000, 2):
            raise ValueError("Formato do trecho de audio invalido")
        if audio.getnframes() > (CHUNK_SECONDS + CHUNK_OVERLAP * 2 + 1) * 16000:
            raise ValueError("Trecho de audio excede o limite de transcricao")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype("float32")
    samples *= 1. / 32768.
    return samples


def _get_model(model_name: str):
    global _MODEL, _MODEL_KEY
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError("Transcricao local indisponivel: instale faster-whisper no servidor.") from exc
    try:
        threads = min(8, max(1, int(os.environ.get("AUTOCLIPS_WHISPER_THREADS", "2"))))
    except ValueError:
        threads = 2
    cache = model_cache_dir()
    local_only = os.environ.get("AUTOCLIPS_WHISPER_LOCAL_ONLY") == "1"
    key = (model_name, str(cache), threads, local_only)
    if _MODEL_KEY != key or _MODEL is None:
        # Drop the previous instance before loading another model. This function
        # is only called while the inference lock is held.
        _MODEL = None
        _MODEL_KEY = None
        cache.mkdir(parents=True, exist_ok=True)
        try:
            _MODEL = WhisperModel(model_name, device="cpu", compute_type="int8",
                                  cpu_threads=threads, num_workers=1, download_root=str(cache),
                                  local_files_only=local_only)
        except Exception as exc:
            raise RuntimeError("Nao foi possivel carregar o modelo local de transcricao. "
                               "Verifique o cache e o acesso do servidor ao modelo.") from exc
        _MODEL_KEY = key
    return _MODEL


def transcribe_local(source: Path, model_name: str | None = None, *,
                     language: str | None = None,
                     progress: ProgressCallback | None = None) -> dict:
    source = Path(source).expanduser().resolve(strict=True)
    model_name = model_name or default_model_name()
    if language == "auto":
        language = None
    if language is not None and not re.fullmatch(r"[a-z]{2,3}", language):
        raise ValueError("Idioma de transcricao invalido; use auto, pt, en ou outro codigo de idioma.")
    duration = _audio_duration(source)
    _notify(progress, "waiting", 0, "Aguardando o transcritor local.")
    words = []
    detected_language = language
    chunk_count = max(1, math.ceil(duration / CHUNK_SECONDS))
    with _INFERENCE_LOCK:
        _notify(progress, "loading_model", 1, "Preparando o modelo de transcricao local.", model=model_name)
        model = _get_model(model_name)
        with tempfile.TemporaryDirectory(prefix="autoclips-audio-") as temporary:
            audio_path = Path(temporary) / "chunk.wav"
            for index in range(chunk_count):
                start = index * CHUNK_SECONDS
                end = min(duration, start + CHUNK_SECONDS)
                decode_start = max(0., start - CHUNK_OVERLAP)
                decode_end = min(duration, end + CHUNK_OVERLAP)
                _notify(progress, "transcribing", 2 + 96 * start / duration,
                        f"Transcrevendo trecho {index + 1} de {chunk_count}.",
                        chunk=index + 1, chunks=chunk_count)
                if _extract_chunk(source, audio_path, decode_start, decode_end - decode_start) is False:
                    # A video can continue after its audio stream ends.
                    audio_path.unlink(missing_ok=True)
                    continue
                segments, info = model.transcribe(
                    _read_audio_chunk(audio_path), language=detected_language, word_timestamps=True,
                    vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
                    beam_size=5, condition_on_previous_text=False,
                )
                chunk_words = []
                for segment in segments:
                    for word in segment.words or []:
                        left, right = decode_start + word.start, decode_start + word.end
                        if not math.isfinite(left) or not math.isfinite(right) or right <= left:
                            continue
                        midpoint = (left + right) / 2
                        # Overlap supplies context at chunk boundaries. Ownership
                        # by midpoint prevents the same word being emitted twice.
                        if not start <= midpoint < end:
                            continue
                        text = str(word.word).strip()
                        left, right = round(max(0., left), 3), round(min(duration, right), 3)
                        if text and right > left:
                            chunk_words.append({"start": left, "end": right, "text": text})
                    done = min(end, max(start, decode_start + float(segment.end)))
                    _notify(progress, "transcribing", 2 + 96 * done / duration,
                            f"Transcrevendo trecho {index + 1} de {chunk_count}.",
                            chunk=index + 1, chunks=chunk_count)
                _merge_chunk_words(words, chunk_words)
                if chunk_words and detected_language is None:
                    detected_language = getattr(info, "language", None)
                audio_path.unlink(missing_ok=True)
    words.sort(key=lambda item: (item["start"], item["end"]))
    _notify(progress, "done", 100, "Transcricao concluida." if words else "Nenhuma fala detectada.",
            words=len(words))
    result = {"words": words, "engine": f"faster-whisper/{model_name}",
              "language": detected_language or language or "und", "duration": round(duration, 3),
              "chunk_seconds": CHUNK_SECONDS, "timestamp_confidence": "lower-than-human-reviewed"}
    if not words:
        result["notice"] = "Nenhuma fala detectada. Revise se o audio contem voz audivel."
    return result
