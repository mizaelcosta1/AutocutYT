"""Local face location from sampled video frames using OpenCV YuNet on CPU."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
import json
import math
import os
from pathlib import Path
import shutil
from statistics import median
import subprocess
from threading import Lock

from autoclips import run


MODEL_NAME = "yunet-2023mar"
DEFAULT_MODEL = Path("/opt/autoclips-models/face_detection_yunet_2023mar.onnx")
MAX_FRAME_SIDE = 960
MAX_SAMPLES = 8
MAX_CANDIDATES = 8
MIN_CONTEXT_SIDE = 0.02
CONFIDENCE_THRESHOLD = 0.72
_opencv_lock = Lock()
_opencv_configured = False

Box = tuple[float, float, float, float]


class FaceDetectionError(RuntimeError):
    """The source could not be decoded or analyzed."""


class FaceDetectionUnavailable(FaceDetectionError):
    """A local dependency for detection is absent or unusable."""


@dataclass(frozen=True)
class Observation:
    box: Box
    confidence: float
    timestamp: float


def _model_path() -> Path:
    return Path(os.environ.get("AUTOCLIPS_FACE_MODEL") or DEFAULT_MODEL).expanduser()


def face_detection_available() -> bool:
    """Check prerequisites without loading a model or running inference."""
    return bool(_model_path().is_file() and shutil.which("ffmpeg") and shutil.which("ffprobe")
                and find_spec("cv2") and find_spec("numpy"))


def _video_geometry(source: Path) -> tuple[int, int, float]:
    try:
        result = run([
            "ffprobe", "-v", "error", "-protocol_whitelist", "file", "-select_streams", "V:0",
            "-show_entries", "stream=width,height,sample_aspect_ratio,duration:stream_side_data=rotation:format=duration",
            "-of", "json", str(source),
        ], timeout=10)
        metadata = json.loads(result.stdout)
        stream = metadata["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(metadata.get("format", {}).get("duration") or stream.get("duration"))
        if width < 1 or height < 1 or not math.isfinite(duration) or duration <= 0:
            raise ValueError("invalid video dimensions or duration")

        ratio = stream.get("sample_aspect_ratio", "N/A")
        if ":" in ratio:
            numerator, denominator = map(int, ratio.split(":"))
            if numerator > 0 and denominator > 0:
                width = max(1, round(width * numerator / denominator))
        rotation = next((item["rotation"] for item in stream.get("side_data_list", []) if "rotation" in item), 0)
        if round(float(rotation)) % 180 == 90:
            width, height = height, width
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError, IndexError) as exc:
        raise FaceDetectionError("Não foi possível ler a duração ou as dimensões do vídeo.") from exc
    return width, height, duration


def _sample_size(width: int, height: int, max_side: int) -> tuple[int, int]:
    scale = min(1.0, max_side / max(width, height))
    return max(2, round(width * scale)), max(2, round(height * scale))


def display_frame_geometry(source: Path, max_side: int = MAX_FRAME_SIDE) -> tuple[int, int, int, int]:
    """Return displayed dimensions and downscaled frame dimensions in square pixels."""
    width, height, _ = _video_geometry(Path(source).resolve())
    sample_width, sample_height = _sample_size(width, height, max_side)
    return width, height, sample_width, sample_height


def _read_frame(source: Path, timestamp: float, size: tuple[int, int]) -> bytes:
    width, height = size
    try:
        result = subprocess.run([
            "ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file", "-threads", "1",
            "-ss", f"{timestamp:.6f}", "-i", str(source), "-map", "0:V:0",
            "-filter_threads", "1", "-vf", f"scale={width}:{height},setsar=1",
            "-frames:v", "1", "-c:v", "png", "-compression_level", "1", "-threads", "1",
            "-f", "image2pipe", "pipe:1",
        ], capture_output=True, timeout=12, check=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise FaceDetectionError("Não foi possível extrair um quadro para localizar rostos.") from exc
    return result.stdout


def _open_detector():
    model = _model_path()
    if not model.is_file():
        raise FaceDetectionUnavailable("Modelo YuNet ausente. Prepare o modelo de detecção local.")
    try:
        import cv2
        import numpy as np
    except (ImportError, OSError) as exc:
        raise FaceDetectionUnavailable("OpenCV não está disponível para localizar rostos.") from exc

    try:
        global _opencv_configured
        with _opencv_lock:
            if not _opencv_configured:
                cv2.setNumThreads(1)
                _opencv_configured = True
        detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), CONFIDENCE_THRESHOLD, 0.3, 1000)
    except (cv2.error, AttributeError) as exc:
        raise FaceDetectionUnavailable("Não foi possível carregar o modelo YuNet no OpenCV.") from exc

    def detect(png: bytes) -> list[tuple[float, float, float, float, float]]:
        try:
            image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise FaceDetectionError("O quadro extraído não pôde ser aberto.")
            height, width = image.shape[:2]
            detector.setInputSize((width, height))
            _, detections = detector.detect(image)
        except cv2.error as exc:
            raise FaceDetectionError("O YuNet não conseguiu analisar um quadro do vídeo.") from exc
        if detections is None:
            return []

        valid = []
        for row in detections:
            x, y, w, h = map(float, row[:4])
            confidence = float(row[-1])
            if not all(math.isfinite(value) for value in (x, y, w, h, confidence)):
                continue
            left, top = max(0.0, x), max(0.0, y)
            right, bottom = min(float(width), x + w), min(float(height), y + h)
            if right > left and bottom > top:
                valid.append((left / width, top / height, (right - left) / width,
                              (bottom - top) / height, confidence))
        return valid

    return detect


def _overlap(first: Box, second: Box) -> float:
    x, y, w, h = first
    other_x, other_y, other_w, other_h = second
    width = max(0.0, min(x + w, other_x + other_w) - max(x, other_x))
    height = max(0.0, min(y + h, other_y + other_h) - max(y, other_y))
    intersection = width * height
    return intersection / (w * h + other_w * other_h - intersection)


def _median_box(cluster: list[Observation]) -> Box:
    xs, ys, widths, heights = zip(*(item.box for item in cluster))
    return median(xs), median(ys), median(widths), median(heights)


def _add_detections(clusters: list[list[Observation]], detections, timestamp: float) -> None:
    # Match a location at most once per frame. Different times contribute to
    # spatial clusters without associating the detections with an identity.
    ordered = sorted(detections, key=lambda item: -item[-1])[:MAX_CANDIDATES]
    for x, y, w, h, confidence in ordered:
        observation = Observation((x, y, w, h), confidence, timestamp)
        best_cluster = None
        best_overlap = 0.30
        for cluster in clusters:
            if cluster[-1].timestamp == timestamp:
                continue
            overlap = _overlap(observation.box, _median_box(cluster))
            if overlap >= best_overlap:
                best_cluster = cluster
                best_overlap = overlap
        if best_cluster is None:
            clusters.append([observation])
        else:
            best_cluster.append(observation)


def _context_box(box: Box, aspect_ratio: float) -> Box:
    x, y, w, h = box
    width = min(1.0, max(MIN_CONTEXT_SIDE, w * 3, h * 2.8 / aspect_ratio))
    height = min(1.0, max(MIN_CONTEXT_SIDE, h * 2.4, w * 1.45 * aspect_ratio))
    left = max(0.0, min(1 - width, x + w / 2 - width / 2))
    top = max(0.0, min(1 - height, y - height * 0.24))
    return left, top, width, height


def _box_dict(box: Box) -> dict[str, float]:
    return {key: round(value, 6) for key, value in zip(("x", "y", "w", "h"), box)}


def _candidate(cluster: list[Observation], samples: int, aspect_ratio: float) -> dict:
    typical_box = _median_box(cluster)
    typical = max(cluster, key=lambda item: (_overlap(item.box, typical_box), item.confidence))
    x, y, w, h = typical.box
    x_center, y_center = x + w / 2, y + h / 2
    near_corner = (x_center <= 0.32 or x_center >= 0.68) and (y_center <= 0.33 or y_center >= 0.67)
    stationary = all(_overlap(item.box, typical_box) >= 0.45 for item in cluster)
    likely_facecam = (near_corner and stationary and w <= 0.32 and h <= 0.45
                      and len(cluster) >= max(2, math.ceil(samples * 0.6)))
    return {
        "box": _box_dict(typical.box), "region": _box_dict(_context_box(typical.box, aspect_ratio)),
        "confidence": round(sum(item.confidence for item in cluster) / len(cluster), 4),
        "samples": len(cluster), "timestamp": round(typical.timestamp, 6), "likely_facecam": likely_facecam,
    }


def analyze_faces(source: Path, *, start: float = 0, end: float | None = None, progress=None) -> dict:
    """Return candidate face and crop boxes normalized to displayed video coordinates.

    The boxes come from sampled positions. A persistent corner region is a
    facecam suggestion and should be confirmed visually before composing a cut.
    """
    source = Path(source).resolve()
    if not source.is_file():
        raise FaceDetectionError("Arquivo de vídeo não encontrado.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise FaceDetectionUnavailable("FFmpeg e ffprobe são necessários para localizar rostos.")

    width, height, duration = _video_geometry(source)
    end = duration if end is None else end
    if not (math.isfinite(start) and math.isfinite(end)
            and 0 <= start < min(end, duration) and end <= duration + 0.001):
        raise ValueError("Intervalo de detecção fora dos limites do vídeo.")
    end = min(end, duration)
    detect = _open_detector()
    frame_size = _sample_size(width, height, MAX_FRAME_SIDE)
    length = end - start
    frame_count = max(1, min(MAX_SAMPLES, math.ceil(length / 0.75)))
    clusters: list[list[Observation]] = []
    decoded = 0
    for index in range(frame_count):
        if progress:
            progress({"phase": "detecting_faces", "progress": 100 * index / frame_count,
                      "message": f"Localizando rostos no quadro {index + 1} de {frame_count}."})
        timestamp = start + length * index / frame_count
        png = _read_frame(source, timestamp, frame_size)
        if png:
            decoded += 1
            _add_detections(clusters, detect(png), timestamp)

    if not decoded:
        raise FaceDetectionError("Nenhum quadro pôde ser extraído no intervalo escolhido.")
    candidates = [_candidate(cluster, decoded, width / height) for cluster in clusters]
    candidates.sort(key=lambda item: (item["likely_facecam"], item["samples"], item["confidence"]), reverse=True)
    candidates = candidates[:MAX_CANDIDATES]
    for index, candidate in enumerate(candidates, start=1):
        candidate["id"] = f"face_{index}"
    status = "none" if not candidates else "single" if len(candidates) == 1 else "multiple"
    if progress:
        progress({"phase": "detecting_faces", "progress": 100, "message": "Análise de rostos concluída."})
    return {"status": status, "model": MODEL_NAME, "width": width, "height": height,
            "start": start, "end": end, "samples": decoded, "candidates": candidates}
