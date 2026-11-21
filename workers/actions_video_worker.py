"""GitHub Actions worker: analyze a private video stored in Backblaze B2.

The runner is temporary. Source videos and generated manifests live in B2;
no video is committed to Git or retained as an Actions artifact.
"""
from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
import re
import tempfile
from pathlib import Path

import boto3
from botocore.config import Config

from autoclips import analyze, probe

OWNER_RE = re.compile(r"^[0-9a-f]{64}$")
VIDEO_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,179}\.(mp4|mkv|mov|webm|ts|m4v)$", re.I)
MAX_SOURCE_BYTES = 8 * 1024 * 1024 * 1024


def truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def validate_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Payload do worker inválido.")
    owner = str(payload.get("owner_hash", ""))
    video_id = str(payload.get("video_id", ""))
    source_key = str(payload.get("source_key", ""))
    if not OWNER_RE.fullmatch(owner):
        raise ValueError("owner_hash deve ser SHA-256 hexadecimal.")
    if not VIDEO_RE.fullmatch(video_id):
        raise ValueError("video_id inválido.")
    parts = PurePosixPath(source_key).parts
    expected_prefix = ("users", owner, "originals", video_id)
    if len(parts) != 5 or parts[:4] != expected_prefix or not FILENAME_RE.fullmatch(parts[4]):
        raise ValueError("source_key fora do namespace privado esperado.")
    if any(part in {"", ".", ".."} for part in parts) or source_key.startswith("/"):
        raise ValueError("source_key inválida.")
    try:
        clips = int(payload.get("clips", 5))
        seconds = int(payload.get("seconds", 45))
    except (TypeError, ValueError):
        raise ValueError("Parâmetros de análise inválidos.") from None
    if not 1 <= clips <= 20 or not 5 <= seconds <= 600:
        raise ValueError("clips deve ser 1-20 e seconds deve ser 5-600.")
    return {
        "owner_hash": owner,
        "video_id": video_id,
        "source_key": source_key,
        "clips": clips,
        "seconds": seconds,
        "transcribe": truthy(payload.get("transcribe", False)),
    }


def event_payload(event_path: str, event_name: str) -> dict:
    with open(event_path, encoding="utf-8") as stream:
        event = json.load(stream)
    if event_name == "workflow_dispatch":
        return event.get("inputs") or {}
    if event_name == "repository_dispatch" and event.get("action") == "autoclips.analyze":
        return event.get("client_payload") or {}
    raise ValueError("Evento de worker não reconhecido.")


def b2_client():
    required = ("B2_ENDPOINT", "B2_REGION", "B2_KEY_ID", "B2_APPLICATION_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing or not os.getenv("B2_BUCKET"):
        raise RuntimeError("Configure os segredos B2_ENDPOINT, B2_REGION, B2_KEY_ID, B2_APPLICATION_KEY e B2_BUCKET.")
    return boto3.client(
        "s3",
        endpoint_url=os.environ["B2_ENDPOINT"],
        region_name=os.environ["B2_REGION"],
        aws_access_key_id=os.environ["B2_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def process(payload: dict, client=None) -> dict:
    job = validate_payload(payload)
    client = client or b2_client()
    bucket = os.environ["B2_BUCKET"]
    source_key = job["source_key"]
    metadata = client.head_object(Bucket=bucket, Key=source_key)
    size = int(metadata.get("ContentLength", 0))
    if size <= 0 or size > MAX_SOURCE_BYTES:
        raise ValueError("Vídeo vazio ou maior que o limite seguro de 8 GiB por execução.")
    with tempfile.TemporaryDirectory(prefix="autoclips-actions-") as folder:
        source = Path(folder) / Path(source_key).name
        client.download_file(bucket, source_key, str(source))
        media_info = probe(source)
        work = analyze(
            source,
            transcript_json=None,
            model_name="tiny" if job["transcribe"] else None,
            clips=job["clips"],
            target=job["seconds"],
        )
        manifest_path = work / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["source"] = source_key
        manifest["source_storage"] = "backblaze-b2"
        manifest["worker"] = "github-actions"
        manifest["probe"] = media_info
        manifest_key = f"users/{job['owner_hash']}/analysis/{job['video_id']}/manifest.json"
        transcript_path = work / "transcript.json"
        transcript_key = f"users/{job['owner_hash']}/analysis/{job['video_id']}/transcript.json"
        client.put_object(
            Bucket=bucket,
            Key=manifest_key,
            Body=json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
            Metadata={"owner-hash": job["owner_hash"], "video-id": job["video_id"]},
        )
        if transcript_path.is_file():
            client.upload_file(
                str(transcript_path), bucket, transcript_key,
                ExtraArgs={"ContentType": "application/json", "Metadata": {
                    "owner-hash": job["owner_hash"], "video-id": job["video_id"]
                }},
            )
        return {
            "status": "succeeded",
            "video_id": job["video_id"],
            "source_key": source_key,
            "manifest_key": manifest_key,
            "transcript_key": transcript_key if transcript_path.is_file() else None,
            "clips": len(manifest.get("clips", [])),
            "duration_seconds": media_info["duration"],
        }


if __name__ == "__main__":
    event = event_payload(os.environ["GITHUB_EVENT_PATH"], os.environ["GITHUB_EVENT_NAME"])
    result = process(event)
    output = json.dumps(result, ensure_ascii=False)
    print(output)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
        stream.write("result_json=" + json.dumps(output) + "\n")
