"""Cache the integrity-checked YuNet CPU detector and its upstream MIT license."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import urlopen


OPENCV_ZOO_REVISION = "47534e27c9851bb1128ccc0102f1145e27f23f98"
MODEL_NAME = "face_detection_yunet_2023mar.onnx"
DEFAULT_MODEL = Path("/opt/autoclips-models") / MODEL_NAME
ASSET_DIR = f"opencv/opencv_zoo/{OPENCV_ZOO_REVISION}/models/face_detection_yunet"
MODEL_URL = f"https://media.githubusercontent.com/media/{ASSET_DIR}/{MODEL_NAME}"
MODEL_SIZE = 232589
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
LICENSE_URL = f"https://raw.githubusercontent.com/{ASSET_DIR}/LICENSE"
LICENSE_SIZE = 1085
LICENSE_SHA256 = "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958"


def _matches(data: bytes, size: int, checksum: str) -> bool:
    return len(data) == size and hashlib.sha256(data).hexdigest() == checksum


def _prepare_asset(destination: Path, url: str, size: int, checksum: str) -> None:
    if destination.is_file() and destination.stat().st_size == size:
        if _matches(destination.read_bytes(), size, checksum):
            return
    with urlopen(url, timeout=60) as response:
        data = response.read(size + 1)
    if not _matches(data, size, checksum):
        raise RuntimeError(f"YuNet asset integrity check failed for {destination.name}")

    with tempfile.TemporaryDirectory(prefix="yunet-", dir=destination.parent) as folder:
        pending = Path(folder) / destination.name
        pending.write_bytes(data)
        pending.replace(destination)


def prepare_model(destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    license_path = destination.with_suffix(".LICENSE.txt")
    _prepare_asset(license_path, LICENSE_URL, LICENSE_SIZE, LICENSE_SHA256)
    _prepare_asset(destination, MODEL_URL, MODEL_SIZE, MODEL_SHA256)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(os.environ.get("AUTOCLIPS_FACE_MODEL") or DEFAULT_MODEL))
    model_path = parser.parse_args().output.expanduser()
    prepare_model(model_path)
    print(f"YuNet: model and MIT license verified at {model_path.parent}")
