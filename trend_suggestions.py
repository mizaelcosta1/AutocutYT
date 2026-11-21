"""Real, attributed daily/weekly suggestions from official public platform APIs.

No invented trend rows. A weekly view is built from the daily observations the
server has actually stored; it never claims a historical 7-day measurement when
only one day was sampled. OAuth tokens and app credentials are never persisted
in trend snapshots or returned by this API.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time

import httpx
from fastapi import APIRouter, Query

MAX_ITEMS = 12
CACHE_SECONDS = 30 * 60
MANUAL_REFRESH_FLOOR = 5 * 60
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
TWITCH_LOGIN = re.compile(r"^[a-zA-Z0-9_]{2,25}$")
SOURCE_LABELS = {"twitch": "Twitch · lives em português",
                 "youtube": "YouTube · vídeos populares de games (BR)"}


def _integer(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def normalize_twitch(items):
    """Only inspect explicit fields from Twitch Helix; live viewers are a snapshot."""
    creators = []
    categories = defaultdict(lambda: {"metric": 0, "streams": 0})
    for item in (items if isinstance(items, list) else [])[:100]:
        if not isinstance(item, dict):
            continue
        login = str(item.get("user_login") or "")
        if not TWITCH_LOGIN.fullmatch(login):
            continue
        viewers = _integer(item.get("viewer_count"))
        game = str(item.get("game_name") or "Sem categoria").strip()[:90]
        title = str(item.get("title") or "Transmissão ao vivo").strip()[:170]
        name = str(item.get("user_name") or login).strip()[:100]
        link = "https://www.twitch.tv/" + login
        thumb = "https://static-cdn.jtvnw.net/previews-ttv/live_user_" + login.lower() + "-440x248.jpg"
        creators.append({
            "id": "twitch:" + login.lower(), "platform": "twitch", "name": name,
            "headline": title, "topic": game, "url": link, "thumbnail_url": thumb,
            "metric": viewers, "metric_unit": "viewers_live", "sample_days": 1,
        })
        categories[game]["metric"] += viewers
        categories[game]["streams"] += 1
    topics = [
        {"id": "twitch:" + name.lower(), "platform": "twitch", "name": name,
         "metric": obj["metric"], "metric_unit": "sampled_live_viewers",
         "sample_days": 1, "stream_count": obj["streams"]}
        for name, obj in categories.items()
    ]
    return sorted(creators, key=lambda x: x["metric"], reverse=True)[:MAX_ITEMS], \
        sorted(topics, key=lambda x: x["metric"], reverse=True)[:MAX_ITEMS]


def normalize_youtube(items):
    """YouTube chart position is not a daily view count or growth measurement."""
    creators_by_channel = {}
    videos = []
    for index, item in enumerate((items if isinstance(items, list) else [])[:50]):
        if not isinstance(item, dict):
            continue
        vid = str(item.get("id") or "")
        if not YOUTUBE_VIDEO_ID.fullmatch(vid):
            continue
        snippet = item.get("snippet") or {}
        if not isinstance(snippet, dict):
            continue
        statistics = item.get("statistics") or {}
        if not isinstance(statistics, dict):
            statistics = {}
        name = str(snippet.get("channelTitle") or "Canal").strip()[:100]
        channel_id = str(snippet.get("channelId") or name)[:100]
        title = str(snippet.get("title") or "Vídeo popular").strip()[:160]
        views = _integer(statistics.get("viewCount"))
        row = {
            "id": "youtube:" + vid, "platform": "youtube", "name": name,
            "headline": title, "topic": "Games", "url": "https://www.youtube.com/watch?v=" + vid,
            "thumbnail_url": "https://i.ytimg.com/vi/" + vid + "/hqdefault.jpg",
            "metric": views, "metric_unit": "views_total", "chart_position": index + 1,
            "sample_days": 1,
        }
        videos.append(row)
        if channel_id not in creators_by_channel:
            creators_by_channel[channel_id] = {**row, "id": "youtube-channel:" + channel_id}
    return list(creators_by_channel.values())[:MAX_ITEMS], videos[:MAX_ITEMS]


def summarize_week(observations):
    """Mean metric over observed dates; 1 day is reported as partial, never 7."""
    groups = {}
    for snap in observations:
        date = snap.get("day")
        for field in ("creators", "topics", "videos"):
            for row in snap.get(field, []):
                if not isinstance(row, dict):
                    continue
                key = (field, row.get("id"))
                if not key[1]:
                    continue
                record = groups.setdefault(key, {"latest": row, "by_day": {}})
                record["by_day"][date] = _integer(row.get("metric"))
                record["latest"] = row
    merged = {"creators": [], "topics": [], "videos": []}
    for (field, _key), rec in groups.items():
        metrics = list(rec["by_day"].values())
        if not metrics:
            continue
        row = dict(rec["latest"])
        row["metric"] = round(sum(metrics) / len(metrics))
        row["sample_days"] = len(metrics)
        row["peak_observed"] = max(metrics)
        merged[field].append(row)
    for field in merged:
        if field == "videos":
            merged[field].sort(key=lambda x: (x.get("chart_position", 1000), -x["sample_days"]))
        else:
            merged[field].sort(key=lambda x: (x["platform"] != "twitch", -x["metric"], -x["sample_days"]))
        merged[field] = merged[field][:MAX_ITEMS * 2]
    return merged


class SuggestionsService:
    def __init__(self, root: Path, vault):
        self.root = Path(root)
        self.vault = vault
        self.path = self.root / "trend_observations.json"
        self.lock = threading.RLock()
        self.recent = {}
        self.twitch_token = ("", 0)

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("schema") == 1 else {"schema": 1, "regions": {}}
        except (OSError, ValueError, TypeError):
            return {"schema": 1, "regions": {}}

    def _save(self, data):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.path.with_suffix(".tmp")
        target.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(target, self.path)

    def _fetch_twitch(self):
        client_id = os.getenv("AUTOCLIPS_TWITCH_CLIENT_ID", "").strip()
        secret = os.getenv("AUTOCLIPS_TWITCH_CLIENT_SECRET", "").strip()
        if not client_id or not secret:
            return [], [], {"id": "twitch", "label": SOURCE_LABELS["twitch"],
                            "status": "not_configured",
                            "message": "Configure as credenciais Twitch do aplicativo no Railway."}
        try:
            with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
                token, exp = self.twitch_token
                if not token or exp <= time.monotonic():
                    resp = client.post("https://id.twitch.tv/oauth2/token",
                                       data={"client_id": client_id, "client_secret": secret,
                                             "grant_type": "client_credentials"})
                    resp.raise_for_status()
                    payload = resp.json()
                    token = str(payload["access_token"])
                    exp = time.monotonic() + max(60, _integer(payload.get("expires_in")) - 300)
                    self.twitch_token = (token, exp)
                resp = client.get("https://api.twitch.tv/helix/streams",
                                  headers={"Client-Id": client_id, "Authorization": "Bearer " + token},
                                  params={"language": "pt", "first": 100, "type": "live"})
                resp.raise_for_status()
                payload = resp.json()
            creators, topics = normalize_twitch(payload.get("data", []))
            return creators, topics, {"id": "twitch", "label": SOURCE_LABELS["twitch"],
                                      "status": "ready",
                                      "message": "Amostra das lives públicas em português, por espectadores simultâneos."}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.twitch_token = ("", 0)
            return [], [], {"id": "twitch", "label": SOURCE_LABELS["twitch"],
                            "status": "unavailable",
                            "message": "A Twitch não respondeu com dados válidos. Tente atualizar depois."}

    def _fetch_youtube(self, region):
        key = os.getenv("AUTOCLIPS_YOUTUBE_API_KEY", "").strip()
        headers = {"Accept": "application/json"}
        if not key:
            try:
                from integrations import get_youtube_access_token
                token, _account = get_youtube_access_token(self.vault)
                headers["Authorization"] = "Bearer " + token
            except (ValueError, KeyError, RuntimeError, TypeError):
                return [], [], {"id": "youtube", "label": SOURCE_LABELS["youtube"],
                                "status": "not_configured",
                                "message": "Conecte seu canal na aba Conexões, ou configure AUTOCLIPS_YOUTUBE_API_KEY."}
        params = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": region,
                  "videoCategoryId": "20", "maxResults": 50}
        if key:
            params["key"] = key
        try:
            with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
                resp = client.get("https://www.googleapis.com/youtube/v3/videos",
                                  params=params, headers=headers)
                resp.raise_for_status()
                payload = resp.json()
            creators, videos = normalize_youtube(payload.get("items", []))
            return creators, videos, {"id": "youtube", "label": SOURCE_LABELS["youtube"],
                                      "status": "ready",
                                      "message": "Ranking popular atual; visualizações informadas são totais, não ganhos por dia."}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return [], [], {"id": "youtube", "label": SOURCE_LABELS["youtube"],
                            "status": "unavailable",
                            "message": "A API YouTube não respondeu; verifique acesso, quota ou autorização."}

    def _capture(self, region, now):
        creators_tw, topics, twitch_state = self._fetch_twitch()
        creators_yt, videos, youtube_state = self._fetch_youtube(region)
        return {"day": now.date().isoformat(), "captured_at": now.isoformat(),
                "creators": creators_tw + creators_yt, "topics": topics,
                "videos": videos, "sources": [twitch_state, youtube_state]}

    def get(self, period="daily", region="BR", refresh=False):
        if not re.fullmatch(r"[A-Z]{2}", region):
            raise ValueError("Região inválida")
        if period not in ("daily", "weekly"):
            raise ValueError("Período inválido")
        with self.lock:
            now = datetime.now(timezone.utc)
            stored = self._read()
            by_day = stored.setdefault("regions", {}).setdefault(region, {})
            existing = by_day.get(now.date().isoformat())
            last = self.recent.get(region)
            elapsed = time.monotonic() - last[0] if last else float("inf")
            # After process restart, honor the recent persistent snapshot.
            if last:
                fresh = elapsed < (MANUAL_REFRESH_FLOOR if refresh else CACHE_SECONDS)
            elif existing:
                try:
                    age = (now - datetime.fromisoformat(existing["captured_at"])).total_seconds()
                    fresh = age < (MANUAL_REFRESH_FLOOR if refresh else CACHE_SECONDS)
                except (TypeError, KeyError, ValueError):
                    fresh = False
            else:
                fresh = False
            if not fresh:
                snapshot = self._capture(region, now)
                by_day[snapshot["day"]] = snapshot
                # Only public aggregates; never access/refresh tokens or user identities.
                minimum = (now.date() - timedelta(days=14)).isoformat()
                for day in list(by_day):
                    if day < minimum:
                        by_day.pop(day, None)
                self._save(stored)
                self.recent[region] = (time.monotonic(), snapshot)
            else:
                snapshot = last[1] if last else existing
                self.recent[region] = (last[0] if last else time.monotonic() - max(0, age), snapshot)
            cutoff = (now.date() - timedelta(days=6)).isoformat()
            observed = [value for day, value in sorted(by_day.items())
                        if cutoff <= day <= now.date().isoformat() and isinstance(value, dict)]
            covered_days = sum(bool(value.get("creators") or value.get("topics") or value.get("videos"))
                               for value in observed)
            if period == "daily":
                ranking = {name: snapshot.get(name, []) for name in ("creators", "topics", "videos")}
            else:
                ranking = summarize_week(observed)
            sources = snapshot.get("sources", [])
            return {
                "period": period, "region": region, "updated_at": snapshot.get("captured_at"),
                "coverage_days": covered_days if period == "weekly" else (1 if covered_days else 0),
                "partial": period == "weekly" and covered_days < 7,
                "status": "ready" if any(ranking.values()) else "no_data",
                "sources": sources, **ranking,
                "methodology": (
                    "Hoje: audiência simultânea da Twitch e posição no ranking YouTube de games; "
                    "views do YouTube são acumuladas, não crescimento de 24h."
                    if period == "daily" else
                    "Semana: média das medições diárias realmente armazenadas nos últimos 7 dias; "
                    "não é audiência total semanal. A cobertura indica quantos dias foram medidos."
                ),
            }


def build_router(root: Path, vault, *, resolve=None):
    router = APIRouter()
    service = SuggestionsService(root, vault)

    @router.get("/api/suggestions/trending")
    def trending(period: str = Query("daily", pattern="^(daily|weekly)$"),
                 region: str = Query("BR", pattern="^[A-Z]{2}$"),
                 refresh: bool = False):
        ws = resolve() if resolve else None
        backend = ws.suggestions if ws else service
        return backend.get(period=period, region=region, refresh=refresh)

    return router
