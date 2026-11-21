"""Regression checks for public trending signals (no external API calls)."""
import json
from trend_suggestions import SuggestionsService, normalize_twitch, normalize_youtube, summarize_week


def test_twitch_ranking_and_topic_aggregation():
    creators, topics = normalize_twitch([
        {"user_login": "boa_live", "user_name": "Boa Live", "game_name": "Minecraft",
         "title": "Construindo", "viewer_count": 57},
        {"user_login": "outra", "user_name": "Outra", "game_name": "Minecraft",
         "viewer_count": 28},
        {"user_login": "malicious/redirect", "viewer_count": 5000, "game_name": "Hack"},
        {"user_login": "terceiro", "user_name": "Terceiro", "game_name": "FPS",
         "viewer_count": -20},
    ])
    assert [x["name"] for x in creators][:2] == ["Boa Live", "Outra"]
    assert creators[0]["url"] == "https://www.twitch.tv/boa_live"
    assert topics[0]["name"] == "Minecraft"
    assert topics[0]["metric"] == 85
    assert topics[0]["stream_count"] == 2
    assert all("redirect" not in x["url"] for x in creators)


def test_youtube_chart_vs_total_views():
    creators, videos = normalize_youtube([
        {"id": "AbC123_xyz0", "snippet": {"title": "Novo vídeo", "channelId": "UC-1",
          "channelTitle": "Streamer A"}, "statistics": {"viewCount": "54321"}},
        {"id": "not-valid", "snippet": {"title": "Pular"}, "statistics": {}},
        {"id": "zzZX9_76543", "snippet": {"title": "Outro vídeo", "channelId": "UC-1",
          "channelTitle": "Streamer A"}, "statistics": {"viewCount": "1010"}},
    ])
    assert len(videos) == 2 and len(creators) == 1
    assert videos[0]["thumbnail_url"] == "https://i.ytimg.com/vi/AbC123_xyz0/hqdefault.jpg"
    assert videos[0]["metric_unit"] == "views_total"
    assert videos[0]["metric"] == 54321
    assert videos[1]["chart_position"] == 3


def test_weekly_metric_is_average_of_days_observed():
    row = {"id": "twitch:boa_live", "platform": "twitch", "name": "Boa Live",
           "metric": 50, "metric_unit": "viewers_live"}
    snapshots = [
        {"day": "2026-11-19", "creators": [row], "topics": [], "videos": []},
        {"day": "2026-11-20", "creators": [{**row, "metric": 70}],
         "topics": [], "videos": []},
    ]
    weekly = summarize_week(snapshots)
    assert weekly["creators"][0]["metric"] == 60
    assert weekly["creators"][0]["sample_days"] == 2
    assert weekly["creators"][0]["peak_observed"] == 70


def test_missing_credentials_never_fabricate_rankings(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOCLIPS_TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("AUTOCLIPS_TWITCH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AUTOCLIPS_YOUTUBE_API_KEY", raising=False)
    service = SuggestionsService(tmp_path, vault=None)
    monkeypatch.setattr(service, "_fetch_youtube", lambda region:
                        ([], [], {"id": "youtube", "status": "not_configured"}))
    result = service.get()
    assert result["status"] == "no_data"
    assert result["creators"] == result["topics"] == result["videos"] == []
    assert [x["status"] for x in result["sources"]] == ["not_configured", "not_configured"]
    saved = json.loads((tmp_path / "trend_observations.json").read_text())
    assert "access_token" not in json.dumps(saved)


def test_weekly_partial_and_refresh_floor(tmp_path, monkeypatch):
    service = SuggestionsService(tmp_path, vault=None)
    calls = [0]
    def fake_twitch():
        calls[0] += 1
        return ([{"id": "twitch:live", "platform": "twitch", "name": "Live",
                  "metric": 42, "metric_unit": "viewers_live"}], [],
                {"id": "twitch", "status": "ready"})
    monkeypatch.setattr(service, "_fetch_twitch", fake_twitch)
    monkeypatch.setattr(service, "_fetch_youtube", lambda region:
                        ([], [], {"id": "youtube", "status": "not_configured"}))
    daily = service.get("daily")
    weekly = service.get("weekly")
    manual = service.get("daily", refresh=True)
    assert calls[0] == 1
    assert daily["status"] == "ready"
    assert weekly["coverage_days"] == 1 and weekly["partial"] is True
    assert weekly["creators"][0]["sample_days"] == 1
    assert manual["updated_at"] == daily["updated_at"]
    persisted = SuggestionsService(tmp_path, vault=None)
    monkeypatch.setattr(persisted, "_fetch_twitch", fake_twitch)
    monkeypatch.setattr(persisted, "_fetch_youtube", lambda region:
                        ([], [], {"id": "youtube", "status": "not_configured"}))
    assert persisted.get("weekly")["coverage_days"] == 1
    assert calls[0] == 1
