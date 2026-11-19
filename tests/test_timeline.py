"""Regression suite for independent, non-destructive timeline editing."""
import pytest
from timeline import new_timeline, apply_command, total_duration, rebase_words, TimelineError


def fresh(duration=120):
    return new_timeline("video_1", duration, "abcdef")


def test_split_move_trim_undo_redo_keeps_original_and_rebases_time():
    original = fresh()
    first_id = original["segments"][0]["id"]
    split = apply_command(original, {"action": "split", "id": first_id, "at": 20})
    assert [(s["source_in"], s["source_out"]) for s in split["segments"]] == [(0, 20), (20, 60)]
    assert [(s["source_in"], s["source_out"]) for s in original["segments"]] == [(0, 60)]
    moved = apply_command(split, {"action": "move", "id": split["segments"][1]["id"], "direction": -1})
    assert moved["segments"][0]["source_in"] == 20
    trimmed = apply_command(moved, {"action": "trim", "id": moved["segments"][0]["id"], "source_in": 25, "source_out": 40})
    assert total_duration(trimmed) == 35
    undone = apply_command(trimmed, {"action": "undo"})
    assert total_duration(undone) == 60
    assert undone["version"] > trimmed["version"]
    redone = apply_command(undone, {"action": "redo"})
    assert total_duration(redone) == 35
    assert redone["version"] > undone["version"]


def test_add_remove_and_reject_last():
    doc = fresh(600)
    added = apply_command(doc, {"action": "add", "source_in": 500, "source_out": 510})
    assert total_duration(added) == 70
    assert len(added["segments"]) == 2
    removed = apply_command(added, {"action": "remove", "id": added["segments"][1]["id"]})
    assert total_duration(removed) == 60
    with pytest.raises(TimelineError, match="ultimo"):
        apply_command(removed, {"action": "remove", "id": removed["segments"][0]["id"]})


def test_invalid_bounds_and_move_rejected_without_mutation():
    doc = fresh(120)
    fid = doc["segments"][0]["id"]
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "split", "id": fid, "at": 0})
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "split", "id": fid, "at": 60})
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "trim", "id": fid, "source_in": -1, "source_out": 40})
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "add", "source_in": 40, "source_out": 160})
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "move", "id": fid, "direction": 1})
    assert doc["version"] == 1
    assert doc["segments"][0]["source_out"] == 60


def test_speech_silence_cleanup_and_word_rebase():
    doc = fresh(100)
    words = [
        {"start": 0.8, "end": 1.2, "text": "bom"},
        {"start": 1.3, "end": 1.8, "text": "dia"},
        {"start": 9.2, "end": 9.8, "text": "pessoal"},
        {"start": 10, "end": 10.5, "text": "!"},
    ]
    clean = apply_command(doc, {"action": "remove_pauses", "threshold": 2.0}, words=words)
    assert len(clean["segments"]) == 2
    assert 1.4 < total_duration(clean) < 3.5
    mapped = rebase_words(clean, words)
    assert [x["text"] for x in mapped] == ["bom", "dia", "pessoal", "!"]
    assert mapped[2]["start"] < 2.0
    assert mapped[2]["start"] > mapped[1]["start"]
    assert [x["start"] for x in mapped] == sorted(x["start"] for x in mapped)


def test_no_speech_never_empties_project():
    doc = fresh(90)
    with pytest.raises(TimelineError, match="transcri"):
        apply_command(doc, {"action": "remove_pauses", "threshold": 2}, words=[])


def test_duration_and_segment_caps():
    doc = fresh(3600)
    assert total_duration(doc) == 60
    with pytest.raises(TimelineError, match="600"):
        apply_command(doc, {"action": "add", "source_in": 300, "source_out": 900})
    with pytest.raises(TimelineError):
        apply_command(doc, {"action": "add", "source_in": 1, "source_out": 1})
