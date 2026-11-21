import json
from pathlib import Path

import pytest

from workers.actions_video_worker import event_payload, validate_payload


OWNER = "a" * 64
VIDEO = "video_123"
SOURCE = f"users/{OWNER}/originals/{VIDEO}/capture.mp4"


def test_valid_payload_is_normalized():
    value = validate_payload({
        "owner_hash": OWNER, "video_id": VIDEO, "source_key": SOURCE,
        "clips": "7", "seconds": "60", "transcribe": "true",
    })
    assert value == {
        "owner_hash": OWNER, "video_id": VIDEO, "source_key": SOURCE,
        "clips": 7, "seconds": 60, "transcribe": True,
    }


@pytest.mark.parametrize("key", [
    f"users/{OWNER}/originals/{VIDEO}/../private.mp4",
    f"users/{'b' * 64}/originals/{VIDEO}/capture.mp4",
    f"users/{OWNER}/originals/other/capture.mp4",
    f"/users/{OWNER}/originals/{VIDEO}/capture.mp4",
    f"users/{OWNER}/originals/{VIDEO}/capture.exe",
])
def test_rejects_untrusted_object_keys(key):
    with pytest.raises(ValueError):
        validate_payload({"owner_hash": OWNER, "video_id": VIDEO, "source_key": key})


@pytest.mark.parametrize("clips,seconds", [(0, 45), (21, 45), (5, 4), (5, 601)])
def test_rejects_out_of_range_analysis_settings(clips, seconds):
    with pytest.raises(ValueError):
        validate_payload({
            "owner_hash": OWNER, "video_id": VIDEO, "source_key": SOURCE,
            "clips": clips, "seconds": seconds,
        })


def test_workflow_dispatch_payload(tmp_path: Path):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"inputs": {"video_id": VIDEO}}), encoding="utf-8")
    assert event_payload(str(event), "workflow_dispatch") == {"video_id": VIDEO}


def test_repository_dispatch_payload(tmp_path: Path):
    event = tmp_path / "event.json"
    payload = {"owner_hash": OWNER, "video_id": VIDEO, "source_key": SOURCE}
    event.write_text(json.dumps({"action": "autoclips.analyze", "client_payload": payload}), encoding="utf-8")
    assert event_payload(str(event), "repository_dispatch") == payload
