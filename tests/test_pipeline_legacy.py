from pathlib import Path
import json
import shutil
import subprocess

import pytest
from autoclips import (fmt_srt, load_words, norm, candidate_score, propose,
                       non_overlapping, analyze, probe)


def test_normalization_and_cues():
    assert norm("NÃO Acredito!") == "nao acredito!"
    words = [
        {"start": 0.1, "end": 0.3, "text": "Não"},
        {"start": 0.4, "end": 0.8, "text": "acredito!"},
    ]
    score, reason, excerpt = candidate_score(words, [-45] * 10, 0, 1)
    assert score >= 20 and "nao acredito" in reason
    assert "Não acredito!" in excerpt


def test_srt_and_bounds():
    assert fmt_srt(61.234) == "00:01:01,234"
    assert load_words({"words":[{"start": .1, "end": .3, "text":"oi"}]}, 1)
    with pytest.raises(ValueError):
        load_words({"words":[{"start": 1, "end": 2, "text":"oi"}]}, 1)


def test_rank_and_overlap():
    energy = [-40] * 100
    words = [{"start": float(t), "end": t + .35, "text": "caramba!" if t == 35 else "teste"}
             for t in range(0, 90, 2)]
    proposals = propose(words, energy, 100.0, max_clips=4, target=30)
    assert proposals
    assert all(0 <= x["start"] < x["end"] <= 100 for x in proposals)
    selected = non_overlapping(proposals, 3)
    assert len(selected) <= 3


@pytest.mark.skip(reason="CLI render_manifest removed in v0.5; video rendering covered by TestClient API and v0.6 timeline tests")
def test_demo_render(tmp_path: Path):
    src = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                    "-t", "5", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", str(src)], check=True)
    transcripts = tmp_path / "transcript.json"
    transcripts.write_text(json.dumps({"words": [
        {"start": .2, "end": .6, "text": "olha"},
        {"start": .8, "end": 1.3, "text": "isso"},
        {"start": 1.7, "end": 2.1, "text": "caramba!"},
        {"start": 2.3, "end": 3.0, "text": "legal!"},
    ]}), encoding="utf-8")
    job = analyze(src, transcripts, None, clips=1, target=8)
    manifest = json.loads((job / "manifest.json").read_text())
    assert len(manifest["clips"]) == 1
    assert manifest["transcription_engine"] == "user-supplied"
    render_manifest(job, quality="preview", layout="fit", burn=False, approved=True)
    preview = job / "preview/clip_01.mp4"
    assert preview.exists()
    assert probe(preview)["height"] == 1280
    render_manifest(job, quality="final", layout="fit", burn=False, approved=True)
    final = job / "final/clip_01.mp4"
    assert final.exists() and probe(final)["height"] == 1920
