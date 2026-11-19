"""Fetch pinned HLS assets and pre-cache Whisper while building the image.

No CDN request or model download is required from a user's browser/recording.
The npm tarball is verified against the published sha512 before extraction.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
from pathlib import Path
import tarfile
import urllib.request

HLS_VERSION = "1.7.4"
HLS_INTEGRITY = "3GlxGdPvT2YHvQYOt+yOzT/DEior/qJVNIylHA77kgeoBTdQQqi70GwefNEUmHQeNGyjC8U62+bruqW/Tpm+tg=="


def prepare_hls() -> None:
    url = f"https://registry.npmjs.org/hls.js/-/hls.js-{HLS_VERSION}.tgz"
    with urllib.request.urlopen(url, timeout=90) as response:
        archive = response.read(15 * 1024 * 1024 + 1)
    if len(archive) > 15 * 1024 * 1024:
        raise RuntimeError("Unexpected HLS distribution size")
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != HLS_INTEGRITY:
        raise RuntimeError("HLS distribution integrity check failed")
    destination = Path(__file__).resolve().parents[1] / "static"
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        for source, name in (("package/dist/hls.min.js", "hls.min.js"), ("package/LICENSE", "hls.LICENSE.txt")):
            member = bundle.extractfile(source)
            if member is None:
                raise RuntimeError(f"HLS distribution is missing {source}")
            (destination / name).write_bytes(member.read())
    print(f"HLS.js {HLS_VERSION}: integrity verified and self-hosted assets prepared")


def prepare_whisper() -> None:
    from faster_whisper.utils import download_model
    model = os.environ.get("AUTOCLIPS_WHISPER_MODEL", "tiny")
    cache = os.environ.get("AUTOCLIPS_WHISPER_CACHE", "/opt/autoclips-models")
    download_model(model, cache_dir=cache)
    print(f"Whisper {model}: model cached for CPU transcription")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hls-only", action="store_true")
    args = parser.parse_args()
    prepare_hls()
    if not args.hls_only:
        prepare_whisper()
