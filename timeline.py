"""Original AutoClips non-destructive, single-source sequential timeline core.

Independent implementation; no source code copied from OpenChatCut.
All public operations return a new state and never mutate their arguments.
"""
from __future__ import annotations

from copy import deepcopy
import math

MAX_SEGMENTS = 80
MAX_EDIT_SECONDS = 600.0
MAX_HISTORY = 40
MIN_CLIP_SECONDS = 0.10


class TimelineError(ValueError):
    """User-facing timeline validation error."""


def _number(value, name: str) -> float:
    if isinstance(value, bool):
        raise TimelineError(f"{name}: numero invalido")
    try:
        n = float(value)
    except (TypeError, ValueError) as exc:
        raise TimelineError(f"{name}: numero invalido") from exc
    if not math.isfinite(n):
        raise TimelineError(f"{name}: numero invalido")
    return round(n, 3)


def _segment(identity: str, start: float, end: float) -> dict:
    return {"id": identity, "source_in": round(start, 3), "source_out": round(end, 3)}


def new_timeline(video_id: str, duration: float, digest: str) -> dict:
    duration = _number(duration, "duracao")
    if duration < MIN_CLIP_SECONDS:
        raise TimelineError("Video muito curto")
    return {
        "schema": "autoclips.timeline.v1", "video_id": video_id,
        "source_duration": duration, "source_sha256": digest,
        "version": 1, "next_id": 2,
        "segments": [_segment("seg_001", 0, min(60.0, duration))],
        "undo": [], "redo": [],
    }


def total_duration(doc: dict) -> float:
    return round(sum(s["source_out"] - s["source_in"] for s in doc["segments"]), 3)


def _validate(doc: dict) -> None:
    segments = doc["segments"]
    if not 1 <= len(segments) <= MAX_SEGMENTS:
        raise TimelineError(f"Permitidos de 1 a {MAX_SEGMENTS} trechos")
    duration = _number(doc["source_duration"], "duracao original")
    ids = set()
    for segment in segments:
        start = _number(segment["source_in"], "inicio")
        end = _number(segment["source_out"], "fim")
        if not (0 <= start < end <= duration + 0.001) or end - start < MIN_CLIP_SECONDS - 0.001:
            raise TimelineError("Trecho invalido ou fora do video original")
        if segment["id"] in ids:
            raise TimelineError("IDs duplicados")
        ids.add(segment["id"])
    if total_duration(doc) > MAX_EDIT_SECONDS + 0.001:
        raise TimelineError(f"A montagem nao pode exceder {int(MAX_EDIT_SECONDS)} segundos")


def _find(segments: list[dict], segment_id: str) -> int:
    for i, s in enumerate(segments):
        if s["id"] == segment_id:
            return i
    raise TimelineError("Trecho nao encontrado")


def _allocate(doc: dict) -> str:
    n = int(doc["next_id"])
    doc["next_id"] = n + 1
    return f"seg_{n:03d}"


def _remove_pauses(doc: dict, words: list[dict], threshold: float) -> None:
    if not words:
        raise TimelineError("E necessaria uma transcricao com palavras para remover pausas")
    if not 0.5 <= threshold <= 30:
        raise TimelineError("Pausa deve estar entre 0.5 e 30 segundos")
    new_segments = []
    for seg in doc["segments"]:
        relevant = sorted((w for w in words if
                           float(w["start"]) >= seg["source_in"] - 0.001 and
                           float(w["end"]) <= seg["source_out"] + 0.001 and
                           w.get("text", "").strip()), key=lambda w: float(w["start"]))
        if not relevant:
            continue
        groups = []
        cur = [relevant[0]]
        for word in relevant[1:]:
            if float(word["start"]) - float(cur[-1]["end"]) >= threshold:
                groups.append(cur)
                cur = [word]
            else:
                cur.append(word)
        groups.append(cur)
        for group in groups:
            start = max(seg["source_in"], float(group[0]["start"]) - 0.14)
            end = min(seg["source_out"], float(group[-1]["end"]) + 0.14)
            if end - start >= MIN_CLIP_SECONDS:
                new_segments.append(_segment(_allocate(doc), start, end))
    if not new_segments:
        raise TimelineError("A transcricao nao possui falas neste recorte")
    doc["segments"] = new_segments


def apply_command(document: dict, command: dict, *, words: list[dict] | None = None) -> dict:
    """Apply one validated command; immutable result with bounded undo/redo."""
    doc = deepcopy(document)
    action = command.get("action")
    if action in {"undo", "redo"}:
        own, other = ("undo", "redo") if action == "undo" else ("redo", "undo")
        if not doc[own]:
            raise TimelineError("Nao ha alteracoes para " + ("desfazer" if own == "undo" else "refazer"))
        doc[other].append(deepcopy(doc["segments"]))
        doc["segments"] = doc[own].pop()
        _validate(doc)
        doc["version"] += 1
        return doc

    before = deepcopy(doc["segments"])
    segments = doc["segments"]
    if action == "split":
        index = _find(segments, command.get("id"))
        at = _number(command.get("at"), "posicao")
        old = segments[index]
        if not old["source_in"] + MIN_CLIP_SECONDS <= at <= old["source_out"] - MIN_CLIP_SECONDS:
            raise TimelineError("Divisao deve ocorrer dentro do trecho, longe das bordas")
        right = _segment(_allocate(doc), at, old["source_out"])
        old["source_out"] = at
        segments.insert(index + 1, right)
    elif action == "trim":
        index = _find(segments, command.get("id"))
        segments[index]["source_in"] = _number(command.get("source_in"), "inicio")
        segments[index]["source_out"] = _number(command.get("source_out"), "fim")
    elif action == "add":
        start = _number(command.get("source_in"), "inicio")
        end = _number(command.get("source_out"), "fim")
        new = _segment(_allocate(doc), start, end)
        after_id = command.get("after_id")
        pos = _find(segments, after_id) + 1 if after_id else len(segments)
        segments.insert(pos, new)
    elif action == "remove":
        if len(segments) == 1:
            raise TimelineError("Nao e permitido remover o ultimo trecho")
        segments.pop(_find(segments, command.get("id")))
    elif action == "move":
        index = _find(segments, command.get("id"))
        direction = command.get("direction")
        if direction not in (-1, 1):
            raise TimelineError("Movimento deve ser -1 ou 1")
        target = index + direction
        if not 0 <= target < len(segments):
            raise TimelineError("Trecho ja esta na extremidade")
        segments[index], segments[target] = segments[target], segments[index]
    elif action == "restore":
        segments[:] = [_segment(_allocate(doc), 0, min(60.0, doc["source_duration"]))]
    elif action == "remove_pauses":
        threshold = _number(command.get("threshold", 2), "pausa")
        _remove_pauses(doc, words or [], threshold)
    else:
        raise TimelineError("Acao de timeline desconhecida")

    _validate(doc)
    if doc["segments"] == before:
        raise TimelineError("Nenhuma alteracao produzida")
    doc["undo"] = (doc["undo"] + [before])[-MAX_HISTORY:]
    doc["redo"] = []
    doc["version"] += 1
    return doc


def rebase_words(document: dict, words: list[dict]) -> list[dict]:
    """Re-map original source words to edited sequence; duplicated edits duplicate words."""
    mapped = []
    offset = 0.0
    for segment in document["segments"]:
        start, end = segment["source_in"], segment["source_out"]
        selected = [w for w in words if start - 0.001 <= float(w["start"]) and
                    float(w["end"]) <= end + 0.001 and str(w.get("text", "")).strip()]
        for w in sorted(selected, key=lambda x: x["start"]):
            mapped.append({"start": round(max(0.0, offset + float(w["start"]) - start), 3),
                           "end": round(offset + float(w["end"]) - start, 3),
                           "text": str(w["text"]).strip(), "source_start": round(float(w["start"]), 3)})
        offset += end - start
    return mapped
