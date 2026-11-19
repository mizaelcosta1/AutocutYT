FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8765 \
    AUTOCLIPS_DATA_ROOT=/data/autoclips \
    AUTOCLIPS_WHISPER_MODEL=tiny \
    AUTOCLIPS_WHISPER_CACHE=/opt/autoclips-models \
    AUTOCLIPS_WHISPER_THREADS=2 \
    AUTOCLIPS_WHISPER_LOCAL_ONLY=1 \
    AUTOCLIPS_FACE_MODEL=/opt/autoclips-models/face_detection_yunet_2023mar.onnx

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    curl \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-transcription.txt requirements-vision.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-transcription.txt -r requirements-vision.txt \
    && pip install --no-cache-dir yt-dlp streamlink

COPY scripts/prepare_runtime_assets.py scripts/prepare_runtime_assets.py
RUN python scripts/prepare_runtime_assets.py

COPY scripts/prepare_face_model.py scripts/prepare_face_model.py
RUN python scripts/prepare_face_model.py

COPY . .

RUN mkdir -p /data/autoclips

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${PORT}/health || exit 1

CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
