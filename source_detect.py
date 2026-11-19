"""Safe, read-only live versus archived-video detection for approved sources.

Detection queries yt-dlp metadata without downloading a media stream. A /live/
URL does not mean the broadcast is currently live: archived YouTube lives use
exactly the same URL shape.
"""
from __future__ import annotations

import json
import subprocess


def classify_info(info: dict, provider: str, url: str) -> str:
    if not isinstance(info, dict):
        raise ValueError("Metadados da transmissao invalidos")
    status = str(info.get("live_status") or "").lower()
    if status == "is_upcoming":
        raise ValueError("Esta live ainda nao comecou")
    if status == "post_live":
        raise ValueError("A live terminou, mas o replay ainda esta em processamento")
    if status == "is_live" or info.get("is_live") is True:
        return "live"
    if status in {"was_live", "not_live"}:
        return "vod"
    # A full video with known duration may not expose live_status.
    if info.get("duration") and not info.get("is_live"):
        return "vod"
    raise ValueError("O provedor nao informou se a transmissao esta ao vivo ou arquivada")


def detect_source_kind(url: str) -> str:
    # Call only after URL host, scheme, port and credentials were validated.
    try:
        result = subprocess.run([
            "yt-dlp", "--dump-single-json", "--skip-download", "--no-playlist",
            "--no-warnings", "--socket-timeout", "10", "--", url,
        ], text=True, capture_output=True, check=False, timeout=40)
    except FileNotFoundError as exc:
        raise RuntimeError("yt-dlp nao instalado no servidor") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tempo excedido ao verificar o estado da transmissao") from exc
    if result.returncode:
        raise RuntimeError("Nao foi possivel consultar o link no provedor (indisponivel, privado ou bloqueado)")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("O provedor retornou metadados invalidos") from exc
    return classify_info(info, "external", url)
