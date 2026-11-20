"""AutoClips Studio API v0.7.1: local-first, cloud-deployable and mobile-friendly.

Start: uvicorn server:app --host 127.0.0.1 --port 8765
For cloud / LAN deployment, set AUTOCLIPS_PASSWORD and bind to 0.0.0.0.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import threading
import time
from typing import Literal
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, RedirectResponse
from pydantic import BaseModel, Field, ConfigDict, field_validator

from autoclips import (MEDIA_EXTENSIONS, CommandError, analyze, download_vod, generate_srt, transcript_local, probe,
                       render_clip, run, sha256_file, workspace, write_json)
from timeline import (TimelineError, apply_command, new_timeline, rebase_words, total_duration)
from timeline_export import render_project
import studio_auth
import logging
from source_detect import detect_source_kind
from integrations import (Vault, build_router, generate_clip_metadata, get_selected, migrate_free_defaults,
                          refresh_saved_free_catalog, rerank_remote, upload_youtube_video)
from ai_runtime import ModelRequestError
from subtitles import SubtitleStyle, normalize_subtitle_style, generate_vtt
from transcription import default_model_name
from live_preview import start_capture, preview_directory, preview_status, resolve_preview_asset, cleanup_preview
from composition import LayoutConfig, normalize_layout_config, require_face
from layout_presets import builtin_presets

APP_VERSION = "0.7.6"
ROOT = Path(os.environ.get("AUTOCLIPS_DATA_ROOT", str(Path.home() / "AutoClipsStudioData"))).expanduser().resolve()
UPLOADS = ROOT / "upload_sessions"
MEDIA = ROOT / "videos"
RECORDINGS = ROOT / "recordings"
DOWNLOADS = ROOT / "downloads"
for _folder in (ROOT, UPLOADS, MEDIA, RECORDINGS, DOWNLOADS):
    _folder.mkdir(parents=True, exist_ok=True)

INDEX = ROOT / "catalog.json"
TASKS_FILE = ROOT / "tasks.json"
CHUNK_SIZE = 8 * 1024 * 1024
LOCK = threading.RLock()
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="autoclips")
LIVE: dict[str, dict] = {}
PENDING_PUBLISH: dict[tuple[str, str], str] = {}
PROCESSING_VIDEOS: dict[str, str] = {}
RENDERING: set[tuple[str, str]] = set()
TASK_CONTEXT = threading.local()
PASSWORD = os.environ.get("AUTOCLIPS_PASSWORD", "")

app = FastAPI(title="AutoClips AI Studio", version=APP_VERSION, docs_url=None, redoc_url=None)
CONNECTION_VAULT = Vault(ROOT)
FREE_MODELS_UPGRADE = migrate_free_defaults(CONNECTION_VAULT)
FREE_CATALOG_REFRESH = refresh_saved_free_catalog(CONNECTION_VAULT)
if FREE_MODELS_UPGRADE.get("migrated"):
    logging.getLogger("uvicorn.error").info(
        "AI free-mode upgrade completed; configured tasks: %s",
        ", ".join(FREE_MODELS_UPGRADE.get("tasks_configured", [])) or "none")
if FREE_CATALOG_REFRESH.get("checked"):
    logging.getLogger("uvicorn.error").info("AI public-catalog upgrade: %s", FREE_CATALOG_REFRESH)
app.include_router(studio_auth.router)
app.include_router(build_router(CONNECTION_VAULT))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def _load_initial_tasks() -> dict[str, dict]:
    saved = read_json(TASKS_FILE, {})
    if not isinstance(saved, dict):
        return {}
    for t in saved.values():
        if t.get("status") in {"queued", "running"}:
            t["status"] = "interrupted"
            t["error"] = "Servidor reiniciado durante a execucao"
            t["ended_at"] = utcnow()
    return saved


TASKS: dict[str, dict] = _load_initial_tasks()


def _save_tasks() -> None:
    items = sorted(TASKS.values(), key=lambda x: x.get("created_at", ""), reverse=True)[:100]
    write_json(TASKS_FILE, {x["id"]: x for x in items})


def cleanup_stale_uploads(max_age_seconds: int = 86400) -> int:
    removed = 0
    now = time.time()
    for folder in list(UPLOADS.iterdir()):
        if not folder.is_dir():
            continue
        try:
            age = now - folder.stat().st_mtime
            if age > max_age_seconds:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed


cleanup_stale_uploads()


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    address = request.client.host if request.client else ""
    is_loopback = address in {"127.0.0.1", "::1", "testclient", "localhost"}
    if request.url.path == "/health" and request.method == "GET":
        return await call_next(request)
    if request.url.path in {"/login", "/login/google", "/login/google/callback"}:
        return await call_next(request)
    if studio_auth.configured():
        if not studio_auth.read_session(request):
            if request.method == "GET" and ("text/html" in request.headers.get("accept", "") or request.url.path == "/"):
                return RedirectResponse('/login', status_code=303)
            return JSONResponse({"detail": "Faça login com Google"}, status_code=401)
        return await call_next(request)
    # Permit unconfigured development only on the local machine.
    if is_loopback:
        return await call_next(request)
    if request.url.path == "/logout":
        return RedirectResponse('/login', status_code=303)
    if request.method == "GET" and ("text/html" in request.headers.get("accept", "") or request.url.path == "/"):
        return RedirectResponse('/login', status_code=303)
    return JSONResponse({"detail": "Configuração do Google OAuth pendente"}, status_code=503)


def clean_filename(name: str) -> str:
    name = (name or "video.mp4").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:100].strip("._") or "video.mp4"
    return name


def catalog() -> dict:
    return read_json(INDEX, {})


def video_info(video_id: str) -> dict:
    with LOCK:
        item = catalog().get(video_id)
    if not item:
        raise HTTPException(status_code=404, detail="Video nao encontrado")
    if not Path(item["path"]).exists():
        raise HTTPException(status_code=410, detail="Arquivo nao esta mais no disco")
    return item


def save_video(item: dict):
    with LOCK:
        data = catalog()
        data[item["id"]] = item
        write_json(INDEX, data)


def task_info(task_id: str):
    with LOCK:
        return TASKS.get(task_id)


def task_progress(progress: dict) -> None:
    """Expose useful stages to the UI without a write for every decoded word."""
    task_id = getattr(TASK_CONTEXT, "task_id", None)
    with LOCK:
        task = TASKS.get(task_id)
        if not task:
            return
        task.update({k: v for k, v in progress.items() if k in {"phase", "progress", "message"}})
        now = time.monotonic()
        if now - getattr(TASK_CONTEXT, "last_save", 0) >= 1 or progress.get("progress") == 100:
            _save_tasks()
            TASK_CONTEXT.last_save = now


def _bind_video_task(task_id: str, video_id: str) -> None:
    TASKS[task_id]["video_id"] = video_id
    PROCESSING_VIDEOS[video_id] = task_id


def schedule(kind: str, fn, *, video_id: str | None = None,
             publish_key: tuple[str, str] | None = None) -> str:
    task_id = uuid.uuid4().hex
    task = {"id": task_id, "kind": kind, "status": "queued", "created_at": utcnow()}

    def release():
        bound_video = task.get("video_id")
        if bound_video and PROCESSING_VIDEOS.get(bound_video) == task_id:
            PROCESSING_VIDEOS.pop(bound_video)
        if publish_key is not None and PENDING_PUBLISH.get(publish_key) == task_id:
            PENDING_PUBLISH.pop(publish_key)

    def do_work():
        TASK_CONTEXT.task_id = task_id
        TASK_CONTEXT.last_save = 0
        outcome = {"status": "interrupted", "error": "Processamento interrompido."}
        try:
            with LOCK:
                task["status"] = "running"
                _save_tasks()
            outcome = {"status": "done", "result": fn()}
        except Exception as exc:
            error = exc.detail if isinstance(exc, HTTPException) else exc
            if (kind in {"render-preview", "render-final", "timeline-preview", "timeline-final"}
                    and isinstance(exc, CommandError) and exc.program == "ffmpeg" and exc.returncode == -9):
                # On Unix, a negative return code denotes a signal. SIGKILL
                # confirms interruption, but does not identify its cause.
                logging.getLogger("uvicorn.error").warning("FFmpeg interrupted during %s (return code %d)",
                                                          kind, exc.returncode)
                error = ("A renderização foi interrompida no servidor antes de terminar. "
                         "Isso pode ocorrer ao atingir um limite de recursos. Tente novamente ou use um trecho menor.")
            outcome = {"status": "error", "error": str(error)[:500]}
        finally:
            TASK_CONTEXT.task_id = None
            with LOCK:
                task.update(outcome, ended_at=utcnow())
                release()
                _save_tasks()

    with LOCK:
        TASKS[task_id] = task
        if video_id is not None:
            _bind_video_task(task_id, video_id)
        if publish_key is not None:
            PENDING_PUBLISH[publish_key] = task_id
        try:
            _save_tasks()
            POOL.submit(do_work)
        except Exception:
            release()
            TASKS.pop(task_id)
            raise
    return task_id


def get_session(upload_id: str) -> tuple[Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(status_code=404, detail="Sessao inexistente")
    folder = UPLOADS / upload_id
    info = read_json(folder / "session.json", None)
    if info is None:
        raise HTTPException(status_code=404, detail="Sessao inexistente")
    return folder, info


class UploadStart(BaseModel):
    filename: str
    size: int = Field(ge=1)
    authorized: bool


class AnalyzeRequest(BaseModel):
    clips: int = Field(default=5, ge=1, le=30)
    seconds: int = Field(default=40, ge=8, le=180)
    transcribe: bool = False
    ollama_model: str | None = Field(default=None, max_length=70)
    use_cloud_model: bool = False
    cloud_approved: bool = False


class ClipItem(BaseModel):
    id: str
    title: str = Field(default="", max_length=140)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    selected: bool = True


class UpdateClips(BaseModel):
    clips: list[ClipItem] = Field(min_length=1, max_length=30)


class ManualClip(BaseModel):
    title: str = Field(default="Momento especial", max_length=140)
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class RenderRequest(BaseModel):
    clip_id: str
    layout: str = "blur"
    subtitles: bool = False
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    layout_config: LayoutConfig = Field(default_factory=LayoutConfig)
    approved_selection: bool = False
    approved_preview: bool = False
    approved_preview_id: str | None = Field(default=None, max_length=64)


class LiveStart(BaseModel):
    url: str
    authorized: bool
    analyze_after: bool = True
    transcribe_after: bool = True
    auto_preview: bool = False


class VodDownloadRequest(BaseModel):
    url: str
    authorized: bool
    analyze_after: bool = True
    transcribe_after: bool = False
    auto_preview: bool = False


class AutoSourceRequest(BaseModel):
    url: str
    authorized: bool
    analyze_after: bool = True
    auto_preview: bool = True
    transcribe_after: bool = True


class PublishYouTubeRequest(BaseModel):
    title: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=4900)
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    tags: list[str] = Field(default_factory=list)
    approved_publish: bool = False


@app.get("/health")
def health():
    return {"status": "ok", "component": "autoclips-media", "version": APP_VERSION}


@app.get("/")
def home():
    return FileResponse(Path(__file__).parent / "static" / "index.html", media_type="text/html",
                        headers={"Cache-Control": "no-cache"})


@app.get("/static/{name}")
def static(name: str):
    if name not in {"app.css", "app.js", "connections.js", "timeline.css", "timeline.js", "hls.min.js", "hls.LICENSE.txt", "live-preview.js", "layout-editor.js", "layout-editor.css", "video-activity.js"}:
        raise HTTPException(status_code=404)
    return FileResponse(Path(__file__).parent / "static" / name, headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
def config():
    try:
        import importlib.util
        has_whisper = importlib.util.find_spec("faster_whisper") is not None
    except Exception:
        has_whisper = False
    return {
        "version": APP_VERSION,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "streamlink": bool(shutil.which("streamlink")),
        "ytdlp": bool(shutil.which("yt-dlp")),
        "transcribe_available": has_whisper,
        "transcription_model": default_model_name(),
        "live_preview_available": bool(shutil.which("ffmpeg")) and bool(shutil.which("streamlink")),
        "face_detection_available": _face_detection_ready(),
        "layout_aspects": ["9:16", "16:9"],
        "layout_modes": ["standard", "split", "split_caption", "pip"],
        "ai_titles_ready": get_selected(CONNECTION_VAULT, "titles") is not None,
        "chunksize": CHUNK_SIZE,
        "upload_limit": None,
        "storage_free_bytes": shutil.disk_usage(ROOT).free,
        "directory": str(ROOT),
    }


@app.get("/api/creative/workflows")
def comfy_cloud_workflows():
    """Read-only Comfy Cloud launcher; never spends credits or shares app secrets.

    The ChatGPT MCP OAuth token is NOT available to this FastAPI service. The
    user opens their saved workflow inside Comfy Cloud, authenticated there.
    """
    raw = os.getenv("AUTOCLIPS_COMFY_WORKFLOWS_JSON", "[]")
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        entries = []
    if not isinstance(entries, list):
        entries = []
    workflows = []
    for entry in entries[:20]:
        if not isinstance(entry, dict):
            continue
        title = entry.get("title", "")
        workflow_id = entry.get("workflow_id", "")
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 80:
            continue
        if not isinstance(workflow_id, str) or not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", workflow_id, re.I
        ):
            continue
        workflows.append({"title": title.strip(), "url": "https://cloud.comfy.org/#" + workflow_id.lower()})
    return {"configured": bool(workflows), "execution": "external", "workflows": workflows}


# --- Timeline editing (independent OpenChatCut-inspired extension) ---

class TimelineEditRequest(BaseModel):
    action: Literal["split", "trim", "add", "remove", "move", "undo", "redo", "restore", "remove_pauses"]
    expected_version: int = Field(ge=1)
    id: str | None = None
    at: float | None = None
    source_in: float | None = None
    source_out: float | None = None
    after_id: str | None = None
    direction: int | None = None
    threshold: float | None = None


def _project_folder(video_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", video_id):
        raise HTTPException(status_code=404, detail="Projeto nao encontrado")
    video_info(video_id)
    return ROOT / "projects" / video_id


def _timeline_document(video_id: str) -> tuple[dict, Path]:
    item = video_info(video_id)
    folder = _project_folder(video_id)
    path = folder / "timeline.json"
    doc = read_json(path, None)
    if doc is None:
        folder.mkdir(parents=True, exist_ok=True)
        doc = new_timeline(video_id, float(item["duration"]), sha256_file(Path(item["path"])))
        write_json(path, doc)
    elif doc.get("schema") != "autoclips.timeline.v1" or doc.get("video_id") != video_id:
        raise HTTPException(status_code=409, detail="Formato do projeto invalido")
    return doc, path


def _public_timeline(doc: dict) -> dict:
    return {"timeline": {
        "schema": doc["schema"], "version": doc["version"],
        "source_duration": doc["source_duration"], "segments": doc["segments"]},
        "edited_duration": total_duration(doc),
        "can_undo": bool(doc["undo"]), "can_redo": bool(doc["redo"])}


def _transcript_document(video_id: str) -> dict:
    item = video_info(video_id)
    possible = [_project_folder(video_id) / "transcript.json"]
    if item.get("job_path"):
        possible.append(Path(item["job_path"]) / "transcript.json")
    for candidate in possible:
        if candidate.exists():
            data = read_json(candidate, {})
            if isinstance(data, dict) and isinstance(data.get("words"), list):
                return data
    return {"words": [], "engine": "none"}


def _transcript_signature(video_id: str) -> str:
    document = _transcript_document(video_id)
    return hashlib.sha256(json.dumps(document.get("words", []), sort_keys=True,
                                    ensure_ascii=False).encode()).hexdigest()[:16]


def _subtitle_style(video_id: str) -> dict:
    return normalize_subtitle_style(read_json(_project_folder(video_id) / "subtitle_style.json", {}))


@app.get("/api/videos/{video_id}/subtitle-style")
def get_subtitle_style(video_id: str):
    return {"subtitle_style": _subtitle_style(video_id)}


@app.put("/api/videos/{video_id}/subtitle-style")
def save_subtitle_style(video_id: str, style: SubtitleStyle):
    with LOCK:
        folder = _project_folder(video_id)
        folder.mkdir(parents=True, exist_ok=True)
        write_json(folder / "subtitle_style.json", style.model_dump())
    return {"subtitle_style": style.model_dump()}


class LayoutSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    layout: Literal["blur", "fit", "crop"] = "blur"
    layout_config: LayoutConfig = Field(default_factory=LayoutConfig)
    subtitles: bool | None = None


class LayoutPreset(LayoutSettings):
    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def readable_name(cls, value):
        value = " ".join(value.split())
        if not value:
            raise ValueError("Dê um nome ao modelo de layout.")
        return value


def _layout_settings(video_id: str) -> dict:
    saved = read_json(_project_folder(video_id) / "layout.json", {})
    return LayoutSettings.model_validate(saved).model_dump()


@app.get("/api/videos/{video_id}/layout")
def get_layout_settings(video_id: str):
    return _layout_settings(video_id)


@app.put("/api/videos/{video_id}/layout")
def save_layout_settings(video_id: str, settings: LayoutSettings):
    # An incomplete composition can be saved while editing; rendering requires
    # a selected face region. Saving never changes an already rendered file.
    with LOCK:
        file = _project_folder(video_id) / "layout.json"
        payload = settings.model_dump()
        if "subtitles" not in settings.model_fields_set:
            saved = read_json(file, {})
            previous = saved.get("subtitles") if isinstance(saved, dict) else None
            payload["subtitles"] = previous if isinstance(previous, bool) else None
        write_json(file, payload)
    return payload


@app.get("/api/layout-presets")
def layout_presets():
    with LOCK:
        return {"presets": builtin_presets(), "custom_presets": read_json(ROOT / "layout_presets.json", [])}


@app.post("/api/layout-presets")
def create_layout_preset(preset: LayoutPreset):
    with LOCK:
        path = ROOT / "layout_presets.json"
        saved = read_json(path, [])
        if len(saved) >= 30:
            raise HTTPException(409, "Você já tem 30 modelos. Exclua um para salvar outro.")
        item = {"id": "custom-" + uuid.uuid4().hex, **preset.model_dump(exclude_none=True),
                "description": "Modelo personalizado. Confira o rosto e a área do jogo em cada vídeo."}
        saved.append(item)
        write_json(path, saved)
    return item


@app.delete("/api/layout-presets/{preset_id}")
def delete_layout_preset(preset_id: str):
    if not re.fullmatch(r"custom-[0-9a-f]{32}", preset_id):
        raise HTTPException(404, "Modelo personalizado não encontrado.")
    with LOCK:
        path = ROOT / "layout_presets.json"
        saved = read_json(path, [])
        remaining = [item for item in saved if item.get("id") != preset_id]
        if len(remaining) == len(saved):
            raise HTTPException(404, "Modelo personalizado não encontrado.")
        write_json(path, remaining)
    return {"deleted": True}


def _face_detection_ready() -> bool:
    try:
        from face_detection import face_detection_available
        return face_detection_available()
    except (ImportError, ValueError):
        return False


class FaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)


@app.post("/api/videos/{video_id}/faces")
def locate_faces(video_id: str, request: FaceRequest):
    item = video_info(video_id)
    end = request.end if request.end is not None else float(item["duration"])
    if not request.start < min(end, float(item["duration"])) or end > float(item["duration"]) + .01:
        raise HTTPException(400, "Escolha um intervalo dentro do vídeo para localizar o rosto.")
    end = min(end, float(item["duration"]))
    if not _face_detection_ready():
        raise HTTPException(503, "A localização automática de rostos está indisponível. Você pode marcar a área manualmente.")
    def work():
        from face_detection import analyze_faces
        return analyze_faces(Path(item["path"]), start=request.start, end=end, progress=task_progress)
    return {"task_id": _schedule_video("detect-faces", video_id, work)}


FRAME_SLOTS = threading.BoundedSemaphore(2)


@app.get("/api/videos/{video_id}/frame")
def source_frame(video_id: str, at: float = 0):
    item = video_info(video_id)
    duration = float(item["duration"])
    if not math.isfinite(at) or at < 0 or at >= duration:
        raise HTTPException(400, "Escolha um momento dentro do vídeo.")
    if not FRAME_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Aguarde o quadro anterior carregar.")
    try:
        from face_detection import display_frame_geometry, FaceDetectionError
        try:
            _, _, sample_width, sample_height = display_frame_geometry(Path(item["path"]), max_side=960)
        except FaceDetectionError:
            raise HTTPException(422, "Não foi possível ler este vídeo para mostrar o quadro.")
        # Exactly the same autorotation, SAR and scale as the detector.
        result = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file",
            "-threads", "2", "-ss", str(at), "-i", str(item["path"]), "-map", "0:V:0",
            "-frames:v", "1", "-an", "-sn", "-dn", "-filter_threads", "1",
            "-vf", f"scale={sample_width}:{sample_height},setsar=1",
            "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=25, check=False)
        if result.returncode or not result.stdout:
            raise HTTPException(422, "Não foi possível abrir este quadro. Escolha outro momento.")
        return Response(result.stdout, media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})
    except (subprocess.TimeoutExpired, FileNotFoundError):
        raise HTTPException(503, "O quadro demorou para carregar. Tente outro momento.")
    finally:
        FRAME_SLOTS.release()


def _update_video(video_id: str, **changes) -> dict:
    with LOCK:
        item = video_info(video_id)
        item.update(changes)
        save_video(item)
        return item


@app.get("/api/videos/{video_id}/activity")
def video_activity(video_id: str):
    with LOCK:
        video_info(video_id)
        task_id = PROCESSING_VIDEOS.get(video_id)
        if not task_id:
            task_id = next((pending for key, pending in PENDING_PUBLISH.items() if key[0] == video_id), None)
        task = TASKS.get(task_id) if task_id else None
        activity = None
        if task and task["status"] in {"queued", "running"}:
            activity = {"task_id": task_id, **{key: task[key] for key in
                        ("kind", "status", "phase", "progress", "message") if key in task}}
    return JSONResponse({"activity": activity}, headers={"Cache-Control": "private, no-store"})


def _schedule_video(kind: str, video_id: str, fn, *, preflight=None) -> str:
    with LOCK:
        video_info(video_id)
        if (video_id in PROCESSING_VIDEOS or any(key[0] == video_id for key in RENDERING)
                or any(key[0] == video_id for key in PENDING_PUBLISH)):
            raise HTTPException(409, "Este vídeo está em processamento. Aguarde a tarefa terminar.")
        if preflight is not None:
            preflight()
        return schedule(kind, fn, video_id=video_id)


def _require_render_space(quality: str, duration: float, *, timeline: bool = False) -> None:
    # CRF output depends on the footage. Estimate 3 Mb/s for 720p, 8 Mb/s for
    # 1080p and 160 kb/s for AAC, plus headroom for metadata and sidecars.
    # Timeline assembly briefly holds both the fragments and the joined file.
    video_bitrate = 3_000_000 if quality == "preview" else 8_000_000
    copies = 2 if timeline else 1
    estimate = 8 * 1024 * 1024 + math.ceil(max(.1, duration) * (video_bitrate + 160_000) * copies / 8)
    free = shutil.disk_usage(ROOT).free
    if free < estimate:
        needed_mb = f"{estimate / 1_000_000:.1f}".replace(".", ",")
        free_mb = f"{free / 1_000_000:.1f}".replace(".", ",")
        raise HTTPException(507, f"Espaço insuficiente para renderizar. Estimativa de {needed_mb} MB, "
                            f"com {free_mb} MB livres. Use um trecho menor ou libere espaço na biblioteca.")


def _transcribe_for_video(video_id: str, *, force: bool = False) -> dict:
    """One transcript is shared by the simple editor and the timeline."""
    existing = _transcript_document(video_id)
    if not force and existing.get("engine", "none") != "none":
        return existing
    source = Path(video_info(video_id)["path"])
    folder = _project_folder(video_id)
    folder.mkdir(parents=True, exist_ok=True)
    with LOCK:
        task_progress({"phase": "waiting", "progress": 0, "message": "Preparando transcrição."})
        _update_video(video_id, transcription={"status": "running", "message": "Preparando transcrição."})
    try:
        result = transcript_local(source, progress=task_progress)
        with LOCK:
            write_json(folder / "transcript.json", result)
            # Refresh the shared analysis document too, so metadata and existing
            # clip suggestions always use the newly generated words.
            item = video_info(video_id)
            if item.get("job_path"):
                work = Path(item["job_path"])
                write_json(work / "transcript.json", result)
                manifest = read_json(work / "manifest.json", None)
                if manifest:
                    manifest["transcription_engine"] = result["engine"]
                    for clip in manifest.get("clips", []):
                        clip["excerpt"] = " ".join(w["text"] for w in result.get("words", [])
                                                  if clip["start"] <= w["start"] < clip["end"])
                    write_json(work / "manifest.json", manifest)
            _update_video(video_id, transcription={"status": "done", "words": len(result.get("words", [])),
                          "engine": result["engine"], "message": result.get("notice", "Legendas disponíveis."),
                          "updated_at": utcnow()})
        return result
    except Exception as exc:
        _update_video(video_id, transcription={"status": "error", "message": str(exc)[:300]})
        raise


def _prepare_recorded_video(video_id: str, *, analyze_after: bool, transcribe_after: bool,
                            auto_preview: bool) -> dict:
    outcome = {"video_id": video_id, "previews": 0, "proposals": 0}
    if transcribe_after:
        try:
            transcript = _transcribe_for_video(video_id)
            outcome["transcription"] = "done"
            outcome["words"] = len(transcript.get("words", []))
        except Exception as exc:
            # Preserve the recording and let the user retry transcription. A
            # missing audio track must not erase a valid video capture.
            outcome["transcription"] = "error"
            outcome["transcription_message"] = str(exc)[:300]
    if analyze_after:
        task_progress({"phase": "analyzing", "progress": 0, "message": "Encontrando momentos na gravação."})
        item = video_info(video_id)
        transcript_path = _project_folder(video_id) / "transcript.json"
        result_path = analyze(Path(item["path"]), transcript_path if transcript_path.exists() else None,
                              None, 5, 40)
        _update_video(video_id, job_path=str(result_path))
        clips = read_json(result_path / "manifest.json", {"clips": []})["clips"]
        outcome["proposals"] = len(clips)
        if auto_preview:
            words = _transcript_document(video_id).get("words", [])
            for i, clip in enumerate(clips[:3]):
                task_progress({"phase": "rendering", "progress": i * 100 / min(3, len(clips)),
                               "message": f"Preparando prévia {i + 1} de {min(3, len(clips))}."})
                has_speech = any(word["start"] < clip["end"] and word["end"] > clip["start"] for word in words)
                render_one(video_id, clip["id"], "preview", "blur", has_speech, _subtitle_style(video_id))
                outcome["previews"] += 1
    return outcome


@app.get("/studio/{video_id}")
def studio_timeline_page(video_id: str):
    _project_folder(video_id)
    return FileResponse(Path(__file__).parent / "static" / "timeline.html", media_type="text/html")


@app.get("/api/videos/{video_id}/timeline")
def get_video_timeline(video_id: str):
    with LOCK:
        doc, _ = _timeline_document(video_id)
        return _public_timeline(doc)


@app.post("/api/videos/{video_id}/timeline")
def edit_video_timeline(video_id: str, req: TimelineEditRequest):
    with LOCK:
        doc, path = _timeline_document(video_id)
        if doc["version"] != req.expected_version:
            raise HTTPException(status_code=409, detail="Timeline foi alterada; atualize a pagina")
        transcript = _transcript_document(video_id) if req.action == "remove_pauses" else {"words": []}
        command = req.model_dump(exclude_none=True, exclude={"expected_version"})
        try:
            edited = apply_command(doc, command, words=transcript.get("words", []))
        except TimelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        write_json(path, edited)
    return _public_timeline(edited)


@app.get("/api/videos/{video_id}/transcript")
def get_video_transcript(video_id: str):
    with LOCK:
        data = _transcript_document(video_id)
        state = video_info(video_id).get("transcription", {})
        if isinstance(state, dict) and state.get("status") == "running":
            task = TASKS.get(PROCESSING_VIDEOS.get(video_id), {})
            if (task.get("status") != "running"
                    or task.get("phase") not in {"waiting", "loading_model", "transcribing", "done"}):
                state = {**state, "status": "error",
                         "message": "A transcrição anterior foi interrompida. Tente gerar a transcrição novamente."}
    return {"available": bool(data.get("words")), "engine": data.get("engine", "none"),
            "words": data.get("words", []), "notice": data.get("notice"),
            "state": state,
            "subtitle_style": _subtitle_style(video_id)}


@app.post("/api/videos/{video_id}/transcript")
def transcribe_video_locally(video_id: str):
    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Instale faster-whisper para transcricao local") from exc
    def work():
        result = _transcribe_for_video(video_id, force=True)
        return {"video_id": video_id, "words": len(result.get("words", [])),
                "engine": result.get("engine"), "notice": result.get("notice")}
    video_info(video_id)
    return {"task_id": _schedule_video("transcribe-local", video_id, work)}


@app.get("/api/videos/{video_id}/subtitles.{extension}")
def download_video_subtitles(video_id: str, extension: str):
    if extension not in {"srt", "vtt"}:
        raise HTTPException(404)
    item = video_info(video_id)
    words = _transcript_document(video_id).get("words", [])
    folder = _project_folder(video_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"transcript.{extension}"
    with LOCK:
        generate = generate_srt if extension == "srt" else generate_vtt
        if not generate(words, 0, item["duration"], target):
            raise HTTPException(404, "Nenhuma fala disponível. Transcreva o vídeo primeiro.")
    return FileResponse(target, media_type="application/x-subrip" if extension == "srt" else "text/vtt",
                        filename=f"autoclips-legendas.{extension}", headers={"Cache-Control": "no-store"})


@app.get("/api/videos/{video_id}/timeline.srt")
def download_timeline_srt(video_id: str):
    return download_timeline_captions(video_id, "srt")


@app.get("/api/videos/{video_id}/timeline.vtt")
def download_timeline_vtt(video_id: str):
    return download_timeline_captions(video_id, "vtt")


def download_timeline_captions(video_id: str, extension: str):
    with LOCK:
        doc, path = _timeline_document(video_id)
        words = _transcript_document(video_id).get("words", [])
        mapped = rebase_words(doc, words)
        if not mapped:
            raise HTTPException(status_code=404, detail="Transcricao sem palavras nesse corte")
        file = path.parent / f"timeline-v{doc['version']}.{extension}"
        generate = generate_srt if extension == "srt" else generate_vtt
        if not generate(mapped, 0, total_duration(doc), file):
            raise HTTPException(status_code=404, detail="Nao foi possivel gerar as legendas")
    return FileResponse(file, media_type="application/x-subrip" if extension == "srt" else "text/vtt",
                        filename=f"autoclips-legendas-v{doc['version']}.{extension}",
                        headers={"Cache-Control": "no-store"})


class TimelineRenderRequest(BaseModel):
    layout: Literal["blur", "fit", "crop"] = "blur"
    subtitles: bool = False
    subtitle_style: SubtitleStyle = Field(default_factory=SubtitleStyle)
    layout_config: LayoutConfig = Field(default_factory=LayoutConfig)
    approved_selection: bool = False
    approved_preview: bool = False
    approved_preview_id: str | None = Field(default=None, max_length=64)


def _current_export(video_id: str, quality: str, *, version: int | None = None) -> dict | None:
    info = read_json(_project_folder(video_id) / "timeline_exports.json", {})
    rendered = info.get(quality)
    if not isinstance(rendered, dict):
        return None
    if version is not None and rendered.get("version") != version:
        return None
    if not Path(rendered.get("path", "")).is_file():
        return None
    if rendered.get("subtitles") and rendered.get("transcript_signature") != _transcript_signature(video_id):
        return None
    return rendered


@app.get("/api/videos/{video_id}/timeline/exports")
def video_timeline_exports(video_id: str):
    with LOCK:
        doc, _ = _timeline_document(video_id)
        out = {}
        for quality in ("preview", "final"):
            meta = _current_export(video_id, quality, version=doc["version"])
            if meta:
                out[quality] = {"url": f"/api/videos/{video_id}/timeline/rendered/{quality}",
                                "version": meta["version"], "layout": meta["layout"],
                                "subtitles": meta["subtitles"], "probe": meta["probe"],
                                "layout_config": normalize_layout_config(meta.get("layout_config")),
                                "subtitle_style": meta.get("subtitle_style", normalize_subtitle_style()),
                                "render_id": meta.get("render_id", "")}
        return {"exports": out}


@app.post("/api/videos/{video_id}/timeline/render/{quality}")
def render_video_timeline(video_id: str, quality: str, req: TimelineRenderRequest):
    if quality not in {"preview", "final"}:
        raise HTTPException(status_code=404, detail="Perfil desconhecido")
    if quality == "preview" and not req.approved_selection:
        raise HTTPException(status_code=400, detail="Aprove a selecao da timeline primeiro")
    if quality == "final" and not req.approved_preview:
        raise HTTPException(status_code=400, detail="Revise e aprove a previa antes da exportacao final")
    with LOCK:
        doc, folder_path = _timeline_document(video_id)
        source = Path(video_info(video_id)["path"])
        doc_snapshot = json.loads(json.dumps(doc))
        style = req.subtitle_style.model_dump()
        composition = req.layout_config.model_dump()
        try:
            require_face(composition)
        except ValueError as error:
            raise HTTPException(400, str(error))
        signature = _transcript_signature(video_id)
        preview_id = None
        if quality == "final":
            preview = _current_export(video_id, "preview", version=doc["version"])
            if (not preview or preview["layout"] != req.layout or preview["subtitles"] != req.subtitles
                    or normalize_layout_config(preview.get("layout_config")) != composition
                    or (req.approved_preview_id is not None and preview.get("render_id") != req.approved_preview_id)
                    or (req.subtitles and preview.get("subtitle_style") != style)):
                raise HTTPException(status_code=409, detail="Gere e assista a previa desta versao com estas opcoes")
            preview_id = preview.get("render_id")
        words = _transcript_document(video_id).get("words", [])
        if req.subtitles and not rebase_words(doc, words):
            raise HTTPException(409, "Transcreva o vídeo e selecione trechos com fala antes de ativar legendas.")
        render_id = uuid.uuid4().hex[:12]
        dest = folder_path.parent / "renders" / f"timeline-v{doc['version']}-{quality}-{render_id}.mp4"
        duration = total_duration(doc_snapshot)

    def work():
        _require_render_space(quality, duration, timeline=True)
        meta = render_project(source, dest, doc_snapshot, words=words,
                              quality=quality, layout=req.layout, subtitles=req.subtitles,
                              subtitle_style=style, layout_config=composition)
        with LOCK:
            current, _ = _timeline_document(video_id)
            if (current["version"] != doc_snapshot["version"]
                    or (req.subtitles and signature != _transcript_signature(video_id))):
                dest.unlink(missing_ok=True)
                raise ValueError("A montagem ou transcrição mudou durante a renderização. Gere uma nova prévia.")
            exports_file = folder_path.parent / "timeline_exports.json"
            records = read_json(exports_file, {})
            if quality == "final" and records.get("preview", {}).get("render_id") != preview_id:
                dest.unlink(missing_ok=True)
                raise ValueError("A prévia aprovada mudou. Revise a versão atual antes de exportar.")
            stale = [records.get(quality)]
            if quality == "preview":
                stale.append(records.pop("final", None))
            meta.update(transcript_signature=signature, render_id=render_id,
                        layout_config=composition, approved_preview_id=preview_id)
            records[quality] = meta
            write_json(exports_file, records)
            for previous in stale:
                if previous and Path(previous.get("path", "")).is_file():
                    Path(previous["path"]).unlink(missing_ok=True)
        return {"quality": quality, "url": f"/api/videos/{video_id}/timeline/rendered/{quality}"}

    return {"task_id": _schedule_video("timeline-" + quality, video_id, work,
                                       preflight=lambda: _require_render_space(quality, duration, timeline=True))}


@app.get("/api/videos/{video_id}/timeline/rendered/{quality}")
def view_timeline_export(video_id: str, quality: str, render_id: str | None = None):
    if quality not in {"preview", "final"}:
        raise HTTPException(status_code=404)
    with LOCK:
        doc, _ = _timeline_document(video_id)
        meta = _current_export(video_id, quality, version=doc["version"])
        if not meta:
            raise HTTPException(status_code=404, detail="Arquivo nao exportado para a versao atual")
        if render_id and meta.get("render_id") != render_id:
            raise HTTPException(409, "Este arquivo foi substituído. Atualize o editor para ver a versão atual.")
    return FileResponse(meta["path"], media_type="video/mp4", filename=f"autoclips-timeline-{quality}.mp4",
                        content_disposition_type="inline")


@app.post("/api/maintenance/cleanup")
def maintenance_cleanup():
    removed = cleanup_stale_uploads(max_age_seconds=3600)
    return {"ok": True, "removed_stale_uploads": removed, "storage_free_bytes": shutil.disk_usage(ROOT).free}


@app.post("/api/upload/start")
def upload_start(req: UploadStart):
    if not req.authorized:
        raise HTTPException(status_code=400, detail="Confirme direito de uso do video")
    safe = clean_filename(req.filename)
    if Path(safe).suffix.lower() not in MEDIA_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Formato nao suportado")
    free = shutil.disk_usage(ROOT).free
    # Keep space for the project JSON, thumbnails and at least one render.
    reserve = max(8 * 1024 * 1024, min(64 * 1024 * 1024, free // 5))
    if req.size > max(0, free - reserve):
        raise HTTPException(status_code=413, detail="Espaco insuficiente para o video e sua edicao. Libere espaco ou use um volume maior.")
    upload_id = uuid.uuid4().hex
    folder = UPLOADS / upload_id
    folder.mkdir()
    (folder / "video.part").touch()
    state = {"id": upload_id, "filename": safe, "size": req.size,
             "chunks": math.ceil(req.size / CHUNK_SIZE), "received": {},
             "created_at": utcnow()}
    write_json(folder / "session.json", state)
    return {"upload_id": upload_id, "chunksize": CHUNK_SIZE, "chunks": state["chunks"]}


@app.get("/api/upload/{upload_id}")
def upload_status(upload_id: str):
    _, info = get_session(upload_id)
    return {"upload_id": upload_id, "chunksize": CHUNK_SIZE,
            "chunks": info["chunks"], "received": sorted(map(int, info["received"]))}


@app.put("/api/upload/{upload_id}/chunk/{index}")
async def upload_chunk(upload_id: str, index: int, request: Request):
    folder, session = get_session(upload_id)
    if not 0 <= index < session["chunks"]:
        raise HTTPException(status_code=400, detail="Indice de bloco invalido")
    expected = min(CHUNK_SIZE, session["size"] - index * CHUNK_SIZE)
    chunk_tmp = folder / f"chunk_{index}.tmp"
    got = 0
    digest = hashlib.sha256()
    try:
        with chunk_tmp.open("wb") as fh:
            async for data in request.stream():
                got += len(data)
                if got > expected:
                    raise HTTPException(status_code=413, detail="Bloco excedeu tamanho esperado")
                digest.update(data)
                fh.write(data)
        if got != expected:
            raise HTTPException(status_code=400, detail=f"Bloco incompleto: {got}/{expected}")
        with LOCK:
            folder, session = get_session(upload_id)
            with (folder / "video.part").open("r+b") as out, chunk_tmp.open("rb") as source:
                out.seek(index * CHUNK_SIZE)
                shutil.copyfileobj(source, out, 1024 * 1024)
                out.flush()
                os.fsync(out.fileno())
            session["received"][str(index)] = digest.hexdigest()
            write_json(folder / "session.json", session)
    finally:
        chunk_tmp.unlink(missing_ok=True)
    return {"uploaded": index, "received": len(session["received"]), "total": session["chunks"]}


def create_thumbnail(path: Path, dest: Path):
    run(["ffmpeg", "-v", "error", "-y", "-ss", "2", "-i", str(path), "-frames:v", "1",
         "-vf", "scale=480:-2", str(dest)], timeout=90)


@app.post("/api/upload/{upload_id}/complete")
def upload_complete(upload_id: str):
    with LOCK:
        folder, session = get_session(upload_id)
        if len(session["received"]) != session["chunks"]:
            raise HTTPException(status_code=409, detail="Ainda faltam blocos no upload")
        source = folder / "video.part"
        if not source.exists() or source.stat().st_size != session["size"]:
            raise HTTPException(status_code=409, detail="Tamanho final inconsistente")
        vid = uuid.uuid4().hex
        dest = MEDIA / f"{vid}_{session['filename']}"
        source.replace(dest)
        folder.joinpath("session.json").unlink(missing_ok=True)
        folder.rmdir()
    try:
        info = probe(dest)
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Arquivo de video nao abriu: {exc}")
    thumb = MEDIA / f"{vid}.jpg"
    try:
        create_thumbnail(dest, thumb)
    except Exception:
        thumb = None
    item = {"id": vid, "name": session["filename"], "path": str(dest),
            "platform": "upload", "created_at": utcnow(), "size": dest.stat().st_size,
            "duration": info["duration"], "width": info["width"],
            "height": info["height"], "thumbnail": str(thumb) if thumb else None}
    save_video(item)
    return {"video": present_video(item)}


@app.post("/api/vod/download")
def import_vod(req: VodDownloadRequest):
    if not req.authorized:
        raise HTTPException(status_code=400, detail="Confirme que tem direito de editar este video")
    if not shutil.which("yt-dlp"):
        raise HTTPException(status_code=503, detail="yt-dlp nao instalado no servidor")
    # Exactly the providers advertised by the VOD import form.
    # Reject arbitrary URLs before scheduling yt-dlp (network access).
    try:
        platform(req.url.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if req.transcribe_after:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="Instale faster-whisper para transcrever") from exc

    def job():
        downloaded = download_vod(req.url.strip(), DOWNLOADS)
        meta = probe(downloaded)
        vid = uuid.uuid4().hex
        thumb = DOWNLOADS / f"{vid}.jpg"
        try:
            create_thumbnail(downloaded, thumb)
        except Exception:
            thumb = None
        item = {
            "id": vid,
            "name": downloaded.name,
            "path": str(downloaded),
            "platform": "vod",
            "source_url": req.url.strip(),
            "created_at": utcnow(),
            "size": downloaded.stat().st_size,
            "duration": meta["duration"],
            "width": meta["width"],
            "height": meta["height"],
            "thumbnail": str(thumb) if thumb else None,
        }
        with LOCK:
            save_video(item)
            _bind_video_task(TASK_CONTEXT.task_id, vid)
        result = _prepare_recorded_video(vid, analyze_after=req.analyze_after,
                                         transcribe_after=req.transcribe_after, auto_preview=req.auto_preview)
        return {**result, "name": downloaded.name}

    return {"task_id": schedule("download-vod", job)}


def present_video(video: dict):
    return {k: v for k, v in video.items() if k != "path" and k != "thumbnail"} | {
        "thumbnail_url": f"/api/videos/{video['id']}/thumbnail" if video.get("thumbnail") else None,
        "stream_url": f"/api/videos/{video['id']}/stream"
    }


@app.get("/api/videos")
def list_videos():
    with LOCK:
        items = list(catalog().values())
    return {"videos": [present_video(x) for x in sorted(items, key=lambda x: x["created_at"], reverse=True)]}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str):
    with LOCK:
        if (video_id in PROCESSING_VIDEOS or any(key[0] == video_id for key in RENDERING)
                or any(key[0] == video_id for key in PENDING_PUBLISH)):
            raise HTTPException(409, "Aguarde o processamento ou envio terminar antes de excluir o projeto.")
        data = catalog()
        item = data.pop(video_id, None)
        if not item:
            raise HTTPException(status_code=404, detail="Video nao encontrado")
        write_json(INDEX, data)
    for key in ("path", "thumbnail"):
        p = item.get(key)
        if p:
            Path(p).unlink(missing_ok=True)
    if item.get("job_path"):
        shutil.rmtree(item["job_path"], ignore_errors=True)
    if re.fullmatch(r"[0-9a-f]{32}", video_id):
        shutil.rmtree(ROOT / "projects" / video_id, ignore_errors=True)
    return {"ok": True}


@app.get("/api/videos/{video_id}/stream")
def stream_video(video_id: str):
    item = video_info(video_id)
    return FileResponse(item["path"], media_type="video/mp4", filename=item["name"], content_disposition_type="inline")


@app.get("/api/videos/{video_id}/thumbnail")
def thumbnail(video_id: str):
    item = video_info(video_id)
    if not item.get("thumbnail") or not Path(item["thumbnail"]).exists():
        raise HTTPException(status_code=404)
    return FileResponse(item["thumbnail"], media_type="image/jpeg")


@app.post("/api/videos/{video_id}/analyze")
def analyze_video(video_id: str, req: AnalyzeRequest):
    item = video_info(video_id)
    if req.transcribe:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="Instale faster-whisper (opcional) para transcrever") from exc
    selected = None
    if req.use_cloud_model:
        if not req.cloud_approved:
            raise HTTPException(400, "Autorize o envio de trechos de transcricao ao provedor de IA")
        if not req.transcribe:
            raise HTTPException(400, "Habilite a transcricao antes de usar IA na nuvem")
        selected = get_selected(CONNECTION_VAULT, "moments")
        if not selected:
            raise HTTPException(400, "Selecione um modelo para momentos na aba Conexoes")
    source = Path(item["path"])

    def job():
        if req.transcribe:
            _transcribe_for_video(video_id)
        task_progress({"phase": "analyzing", "progress": 0, "message": "Encontrando os melhores momentos."})
        transcript_path = _project_folder(video_id) / "transcript.json"
        reranker = (lambda clips: rerank_remote(clips, selected)) if selected else None
        result_path = analyze(source, transcript_path if transcript_path.exists() else None, None,
                              req.clips, req.seconds, req.ollama_model, reranker)
        _update_video(video_id, job_path=str(result_path))
        return {"video_id": video_id, "clips": len(read_json(result_path / "manifest.json", {})["clips"])}

    return {"task_id": _schedule_video("analyze", video_id, job)}


def get_manifest(video_id: str) -> tuple[dict, Path, dict]:
    item = video_info(video_id)
    if not item.get("job_path"):
        raise HTTPException(status_code=404, detail="Analise nao realizada")
    work = Path(item["job_path"])
    manifest = read_json(work / "manifest.json", None)
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifesto nao encontrado")
    return manifest, work, item


@app.get("/api/videos/{video_id}/manifest")
def show_manifest(video_id: str):
    manifest, _, _ = get_manifest(video_id)
    signature = _transcript_signature(video_id)
    def ready(clip, quality):
        meta = clip.get(quality)
        return bool(meta) and (not meta.get("burn") or meta.get("transcript_signature") == signature)
    return {"transcription_engine": manifest["transcription_engine"],
            "ranking": manifest["ranking"], "duration": manifest["probe"]["duration"],
            "review_required": manifest.get("review_required", True),
            "clips": [{k: v for k, v in c.items() if k not in {"preview", "final"}} | {
                "preview_url": f"/api/videos/{video_id}/rendered/{c['id']}/preview?v={c.get('preview', {}).get('render_id', '')}" if ready(c, "preview") else None,
                "final_url": f"/api/videos/{video_id}/rendered/{c['id']}/final?v={c.get('final', {}).get('render_id', '')}" if ready(c, "final") else None,
                "preview_options": {k: c.get("preview", {}).get(k) for k in ("layout", "burn", "subtitle_style", "layout_config", "render_id")},
                "final_options": {k: c.get("final", {}).get(k) for k in ("layout", "burn", "subtitle_style", "layout_config", "render_id")},
            } for c in manifest["clips"]]}


@app.post("/api/videos/{video_id}/manual")
def manual_clip(video_id: str, req: ManualClip):
    item = video_info(video_id)
    duration = float(item["duration"])
    if not 0 <= req.start < req.end <= duration + .01:
        raise HTTPException(status_code=400, detail="Intervalo fora do video")
    try:
        manifest, work, _ = get_manifest(video_id)
    except HTTPException as err:
        if err.status_code != 404:
            raise
        src = Path(item["path"])
        digest = sha256_file(src)
        work = workspace(src, digest)
        work.mkdir(exist_ok=True, parents=True)
        write_json(work / "transcript.json", {"words": [], "engine": "none"})
        manifest = {"version": APP_VERSION, "created_at": utcnow(), "source": str(src),
                    "sha256": digest, "probe": probe(src), "transcription_engine": "none",
                    "ranking": "manual", "clips": [], "review_required": True}
        item["job_path"] = str(work)
        save_video(item)
    if len(manifest["clips"]) >= 30:
        raise HTTPException(status_code=400, detail="Maximo de 30 clipes por video")
    used = {x["id"] for x in manifest["clips"]}
    i = 1
    while f"clip_{i:02d}" in used:
        i += 1
    clip = {"id": f"clip_{i:02d}", "title": req.title,
            "start": round(req.start, 3), "end": round(req.end, 3),
            "score": None, "reason": "Selecao manual", "excerpt": "",
            "method": "manual", "selected": True, "status": "proposed_only"}
    manifest["clips"].append(clip)
    write_json(work / "manifest.json", manifest)
    return {"clip": clip}


@app.put("/api/videos/{video_id}/clips")
def update_clips(video_id: str, req: UpdateClips):
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        known = {c["id"]: c for c in manifest["clips"]}
        if set(x.id for x in req.clips) != set(known):
            raise HTTPException(status_code=400, detail="Envie todos os clipes existentes")
        if any((video_id, c.id) in PENDING_PUBLISH for c in req.clips):
            raise HTTPException(status_code=409, detail="Aguarde o envio ao YouTube terminar antes de editar")
        duration = manifest["probe"]["duration"]
        stale_renders = []
        for c in req.clips:
            if not (0 <= c.start < c.end <= duration + .01):
                raise HTTPException(status_code=400, detail="Um clipe esta fora dos limites")
            old = known[c.id]
            changed = round(c.start, 3) != old["start"] or round(c.end, 3) != old["end"]
            old.update(title=c.title, start=round(c.start, 3), end=round(c.end, 3), selected=c.selected)
            if changed:
                for stage in ("preview", "final"):
                    previous_render = old.pop(stage, None)
                    if isinstance(previous_render, dict):
                        for field in ("path", "subtitles"):
                            candidate = previous_render.get(field)
                            if isinstance(candidate, str):
                                render_path = Path(candidate).resolve()
                                if render_path.is_relative_to(work.resolve()) and render_path.suffix.lower() in {".mp4", ".srt"}:
                                    stale_renders.append(render_path)
                # Keep evidence of past uploads, but never show it as the
                # publication of the newly edited clip.
                previous_upload = old.pop("youtube", None)
                if previous_upload:
                    old.setdefault("youtube_history", []).append(previous_upload)
                old["status"] = "proposed_only"
        write_json(work / "manifest.json", manifest)
        for stale_path in stale_renders:
            stale_path.unlink(missing_ok=True)
    return {"ok": True}


@app.delete("/api/videos/{video_id}/clips/{clip_id}")
def delete_clip(video_id: str, clip_id: str):
    with LOCK:
        if video_id in PROCESSING_VIDEOS:
            raise HTTPException(409, "Aguarde o processamento terminar antes de remover este corte.")
        if (video_id, clip_id) in PENDING_PUBLISH:
            raise HTTPException(status_code=409, detail="Aguarde o envio ao YouTube terminar antes de remover")
        manifest, work, _ = get_manifest(video_id)
        idx = next((i for i, c in enumerate(manifest["clips"]) if c["id"] == clip_id), None)
        if idx is None:
            raise HTTPException(status_code=404, detail="Clipe nao encontrado")
        removed = manifest["clips"].pop(idx)
        write_json(work / "manifest.json", manifest)
    for stage in ("preview", "final"):
        info = removed.get(stage)
        if isinstance(info, dict):
            if info.get("path"):
                Path(info["path"]).unlink(missing_ok=True)
            if info.get("subtitles"):
                Path(info["subtitles"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/videos/{video_id}/clips/{clip_id}/generate-meta")
def ai_clip_metadata(video_id: str, clip_id: str):
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        clip = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if not clip:
            raise HTTPException(status_code=404, detail="Clipe nao encontrado")
        excerpt = clip.get("excerpt", "")
        if not excerpt:
            words = read_json(work / "transcript.json", {"words": []}).get("words", [])
            excerpt = " ".join(w["text"] for w in words if clip["start"] <= w["start"] <= clip["end"])
    try:
        meta = generate_clip_metadata(CONNECTION_VAULT, excerpt, clip.get("title", ""))
    except ModelRequestError as exc:
        raise HTTPException(exc.status_code, str(exc),
                            headers={"Retry-After": str(exc.retry_after)} if exc.retry_after else None) from None
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Falha ao gerar metadados: {exc}") from exc
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        current = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if current:
            current["title"] = meta["title"]
            current["description"] = meta["description"]
            current["tags"] = meta["tags"]
            write_json(work / "manifest.json", manifest)
    return meta


@app.post("/api/videos/{video_id}/clips/{clip_id}/publish/youtube")
def publish_clip_youtube(video_id: str, clip_id: str, req: PublishYouTubeRequest):
    if not req.approved_publish:
        raise HTTPException(status_code=400, detail="Marque a confirmacao explicita antes de publicar no YouTube")
    with LOCK:
        if video_id in PROCESSING_VIDEOS:
            raise HTTPException(409, "Aguarde o processamento deste vídeo antes de enviar ao YouTube.")
        manifest, _, _ = get_manifest(video_id)
        clip = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if not clip:
            raise HTTPException(status_code=404, detail="Clipe nao encontrado")
        if clip.get("youtube", {}).get("youtube_video_id"):
            raise HTTPException(status_code=409, detail="Clipe ja enviado ao YouTube; edite e renderize uma nova versao para publicar outro video")
        publish_key = (video_id, clip_id)
        if publish_key in PENDING_PUBLISH:
            raise HTTPException(status_code=409, detail="Ja existe um envio em andamento para este clipe")
        final_info = clip.get("final")
        if not isinstance(final_info, dict) or not final_info.get("path"):
            raise HTTPException(status_code=409, detail="Exporte o video final em 1080p antes de enviar ao YouTube")
        final_path = Path(final_info["path"])
        title = (req.title or clip.get("title") or "Corte AutoClips #Shorts").strip()
        desc = (req.description or clip.get("description") or clip.get("excerpt") or "Editado no AutoClips AI Studio").strip()
        tags = req.tags or clip.get("tags") or ["Shorts", "AutoClips"]

        def job():
            res = upload_youtube_video(
                CONNECTION_VAULT,
                final_path,
                title=title,
                description=desc,
                privacy_status=req.privacy_status,
                tags=tags,
            )
            with LOCK:
                m, w, _ = get_manifest(video_id)
                cur = next((c for c in m["clips"] if c["id"] == clip_id), None)
                if cur:
                    cur["youtube"] = res
                    write_json(w / "manifest.json", m)
            return res

        return {"task_id": schedule("publish-youtube", job, publish_key=publish_key)}


def _preview_matches(clip: dict, layout: str, burn: bool, style: dict, signature: str,
                     layout_config=None, approved_preview_id=None) -> bool:
    preview = clip.get("preview", {})
    return bool(preview) and preview.get("layout") == layout and preview.get("burn") == burn and (
        normalize_layout_config(preview.get("layout_config")) == normalize_layout_config(layout_config)
        and (approved_preview_id is None or preview.get("render_id") == approved_preview_id)
        and (not burn or (preview.get("subtitle_style") == style and preview.get("transcript_signature") == signature)))


def render_one(video_id: str, clip_id: str, quality: str, layout: str, burn: bool,
               subtitle_style=None, layout_config=None, approved_preview_id=None) -> dict:
    style = normalize_subtitle_style(subtitle_style)
    composition = normalize_layout_config(layout_config)
    require_face(composition)
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        selected = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if selected is None:
            raise ValueError("Clipe nao existe")
        signature = _transcript_signature(video_id)
        if quality == "final" and not _preview_matches(selected, layout, burn, style, signature,
                                                       composition, approved_preview_id):
            raise ValueError("Gere e aprove uma prévia com estas opções de enquadramento e legenda.")
        preview_id = selected.get("preview", {}).get("render_id") if quality == "final" else None
        clip = selected.copy()
        source = Path(manifest["source"])
        source_signature = manifest["sha256"]
        if not source.exists() or sha256_file(source) != manifest["sha256"]:
            raise ValueError("O original foi alterado; analise novamente")
        has_audio = bool(manifest["probe"]["audio_codec"])
        words = _transcript_document(video_id).get("words", [])
    _require_render_space(quality, clip["end"] - clip["start"])
    folder = work / quality
    folder.mkdir(parents=True, exist_ok=True)
    render_id = uuid.uuid4().hex[:12]
    dest = folder / f"{clip_id}-{render_id}.mp4"
    srt_path = folder / f"{clip_id}-{render_id}.srt"
    try:
        subs = srt_path if generate_srt(words, clip["start"], clip["end"], srt_path) else None
        if burn and subs is None:
            raise ValueError("Este corte não tem falas transcritas. Transcreva o vídeo ou desative as legendas.")
        metadata = render_clip(source, dest, clip, quality=quality, layout=layout,
                               burn=burn, srt=subs, has_audio=has_audio, subtitle_style=style,
                               layout_config=composition)
    except Exception:
        srt_path.unlink(missing_ok=True)
        raise
    if not source.exists() or sha256_file(source) != source_signature:
        dest.unlink(missing_ok=True)
        if subs:
            subs.unlink(missing_ok=True)
        raise ValueError("O original mudou durante a renderização. Analise o vídeo novamente.")
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        current = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if (current is None or manifest["sha256"] != source_signature or Path(manifest["source"]) != source
                or current["start"] != clip["start"] or current["end"] != clip["end"]
                or (burn and signature != _transcript_signature(video_id))
                or (quality == "final" and current.get("preview", {}).get("render_id") != preview_id)):
            dest.unlink(missing_ok=True)
            if subs:
                subs.unlink(missing_ok=True)
            raise ValueError("O corte, a legenda ou a prévia mudou durante a renderização. Gere uma nova prévia.")
        stale = [current.get(quality)]
        if quality == "preview":
            stale.append(current.pop("final", None))
            if current.get("youtube"):
                current.setdefault("youtube_history", []).append(current.pop("youtube"))
        current[quality] = {"path": str(dest), "subtitles": str(subs) if subs else None,
                            "layout": layout, "burn": burn, "subtitle_style": style,
                            "layout_config": composition, "approved_preview_id": preview_id,
                            "transcript_signature": signature, "render_id": render_id,
                            "qa": {"full_decode_ok": True, **metadata}}
        current["status"] = "preview_ready" if quality == "preview" else "final_ready"
        write_json(work / "manifest.json", manifest)
        for record in stale:
            if isinstance(record, dict):
                for key in ("path", "subtitles"):
                    candidate = Path(record[key]) if record.get(key) else None
                    if candidate and candidate.is_relative_to(work):
                        candidate.unlink(missing_ok=True)
    return {"video_id": video_id, "clip_id": clip_id, "quality": quality,
            "url": f"/api/videos/{video_id}/rendered/{clip_id}/{quality}"}


@app.post("/api/videos/{video_id}/render/{quality}")
def request_render(video_id: str, quality: str, req: RenderRequest):
    manifest, _, _ = get_manifest(video_id)
    if quality not in {"preview", "final"} or req.layout not in {"blur", "crop", "fit"}:
        raise HTTPException(status_code=400, detail="Formato invalido")
    if not re.fullmatch(r"clip_[0-9a-z_]+", req.clip_id) or req.clip_id not in {c["id"] for c in manifest["clips"]}:
        raise HTTPException(status_code=400, detail="Clipe invalido")
    if quality == "preview" and not req.approved_selection:
        raise HTTPException(status_code=400, detail="Aprove a selecao primeiro")
    if quality == "final" and not req.approved_preview:
        raise HTTPException(status_code=400, detail="Aprove a previa primeiro")
    clip = next(c for c in manifest["clips"] if c["id"] == req.clip_id)
    style = req.subtitle_style.model_dump()
    composition = req.layout_config.model_dump()
    try:
        require_face(composition)
    except ValueError as error:
        raise HTTPException(400, str(error))
    if quality == "final" and not _preview_matches(clip, req.layout, req.subtitles, style,
                                                  _transcript_signature(video_id), composition, req.approved_preview_id):
        raise HTTPException(409, "Gere e aprove uma prévia com estas opções de enquadramento e legenda.")
    return {"task_id": _schedule_video("render-" + quality, video_id,
                lambda: render_one(video_id, req.clip_id, quality, req.layout, req.subtitles, style,
                                   composition, req.approved_preview_id),
                preflight=lambda: _require_render_space(quality, clip["end"] - clip["start"]))}


@app.get("/api/videos/{video_id}/rendered/{clip_id}/{quality}")
def view_rendered(video_id: str, clip_id: str, quality: str, v: str | None = None):
    manifest, _, _ = get_manifest(video_id)
    if not re.fullmatch(r"clip_[0-9a-z_]+", clip_id) or quality not in {"preview", "final"}:
        raise HTTPException(status_code=404)
    clip = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
    if not clip or quality not in clip:
        raise HTTPException(status_code=404)
    if v and clip[quality].get("render_id") != v:
        raise HTTPException(409, "Este arquivo foi substituído. Atualize o editor para ver a versão atual.")
    if clip[quality].get("burn") and clip[quality].get("transcript_signature") != _transcript_signature(video_id):
        raise HTTPException(409, "A transcrição mudou. Gere uma nova prévia com as legendas atuais.")
    return FileResponse(clip[quality]["path"], media_type="video/mp4", filename=f"{clip_id}_{quality}.mp4", content_disposition_type="inline")


@app.get("/api/tasks/{task_id}")
def task_status(task_id: str):
    info = task_info(task_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Tarefa nao encontrada (servidor reiniciado?)")
    return info


def platform(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
        raise ValueError("Informe link HTTPS normal da live")
    host = (parsed.hostname or "").lower()
    if host in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}:
        return "twitch"
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}:
        return "youtube"
    if host in {"kick.com", "www.kick.com"}:
        return "kick"
    raise ValueError("Aceitamos links diretos de Twitch, YouTube e Kick")


def watch_live(live_id: str, proc, target: Path, logs):
    code = proc.wait()
    logs.close()
    with LOCK:
        info = LIVE[live_id]
        info["exit_code"] = code
        info["status"] = "saving"
        info["bytes"] = target.stat().st_size if target.exists() else 0
    cleanup_preview(preview_directory(target))
    if not target.exists() or target.stat().st_size < 1024:
        with LOCK:
            info["message"] = "Nenhum video foi gravado"
            info["status"] = "error"
        return
    result = target
    remux = target.with_suffix(".mp4")
    try:
        try:
            run(["ffmpeg", "-v", "error", "-y", "-i", str(target), "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(remux)])
        except Exception:
            run(["ffmpeg", "-v", "error", "-y", "-i", str(target), "-c", "copy", "-movflags", "+faststart", str(remux)])
        result = remux
        target.unlink(missing_ok=True)
    except Exception as exc:
        remux.unlink(missing_ok=True)
        with LOCK:
            info["message"] = f"Remux MP4 indisponivel: {str(exc)[:120]}"
    try:
        metadata = probe(result)
    except Exception as exc:
        with LOCK:
            info["status"] = "error"
            info["message"] = f"O arquivo foi preservado, mas não abriu no editor de vídeo: {str(exc)[:160]}"
        return
    vid = uuid.uuid4().hex
    item = {"id": vid, "name": result.name, "path": str(result),
            "platform": info["platform"], "created_at": utcnow(),
            "size": result.stat().st_size, "duration": metadata["duration"],
            "width": metadata["width"], "height": metadata["height"], "thumbnail": None}
    thumb = RECORDINGS / f"{vid}.jpg"
    try:
        create_thumbnail(result, thumb)
        item["thumbnail"] = str(thumb)
    except Exception:
        pass
    save_video(item)
    with LOCK:
        info["video_id"] = vid
        info["status"] = "completed"
        if code != 0 and not info.get("message"):
            info["message"] = "A captura foi interrompida; o trecho recebido foi salvo."
    if info.get("analyze_after") or info.get("transcribe_after"):
        task_id = _schedule_video("prepare-after-live", vid,
            lambda: _prepare_recorded_video(vid, analyze_after=info.get("analyze_after", False),
                                           transcribe_after=info.get("transcribe_after", False),
                                           auto_preview=info.get("auto_preview", False)))
        with LOCK:
            info["analysis_task_id"] = task_id


def monitor_live_storage(live_id: str, proc) -> None:
    """Stop before a recording consumes the space needed to save its metadata."""
    wake = threading.Event()
    while proc.poll() is None:
        with LOCK:
            # MP4 packaging briefly needs a second copy of every active TS.
            # Reserve that space before the recorder consumes it.
            pending_bytes = 0
            for record in LIVE.values():
                if record.get("status") in {"starting", "recording", "stopping", "saving"}:
                    try:
                        pending_bytes += record["target"].stat().st_size
                    except FileNotFoundError:
                        pass
        if shutil.disk_usage(RECORDINGS).free < pending_bytes + 64 * 1024 * 1024:
            with LOCK:
                info = LIVE.get(live_id)
                if info:
                    info["message"] = "Gravação encerrada para reservar espaço ao salvamento. Libere espaço antes de gravar novamente."
                    info["status"] = "stopping"
            proc.terminate()
            return
        wake.wait(5)


@app.post("/api/live/start")
def start_live(req: LiveStart):
    if not req.authorized:
        raise HTTPException(status_code=400, detail="Confirme que tem permissao para gravar")
    try:
        plat = platform(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not shutil.which("streamlink"):
        raise HTTPException(status_code=503, detail="Streamlink nao instalado no computador servidor")
    if not shutil.which("ffmpeg"):
        raise HTTPException(503, "O servidor precisa do FFmpeg para gravar e mostrar a prévia.")
    if shutil.disk_usage(RECORDINGS).free < 80 * 1024 * 1024:
        raise HTTPException(507, "Há pouco espaço para gravar. Libere espaço na biblioteca e tente novamente.")
    if req.transcribe_after:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="Instale faster-whisper para transcrever a live") from exc
    live_id = uuid.uuid4().hex
    target = RECORDINGS / f"live_{plat}_{live_id}.ts"
    logpath = RECORDINGS / f"live_{plat}_{live_id}.log"
    with LOCK:
        if any(record.get("url") == req.url and record.get("status") in
               {"starting", "recording", "stopping"} for record in LIVE.values()):
            raise HTTPException(409, "Esta live ja esta sendo gravada; acompanhe em Lives")
        loghandle = logpath.open("w", encoding="utf-8")
        try:
            proc = start_capture(req.url, target, loghandle)
        except OSError as exc:
            loghandle.close()
            raise HTTPException(502, "Falha ao iniciar o gravador Streamlink") from exc
        LIVE[live_id] = {"id": live_id, "platform": plat, "url": req.url,
                         "status": "starting", "created_at": utcnow(), "process": proc,
                         "target": target, "logpath": logpath,
                         "analyze_after": req.analyze_after,
                         "transcribe_after": req.transcribe_after,
                         "auto_preview": req.auto_preview}
    threading.Thread(target=watch_live, args=(live_id, proc, target, loghandle), daemon=True).start()
    threading.Thread(target=monitor_live_storage, args=(live_id, proc), daemon=True).start()
    return {"id": live_id, "platform": plat, "status": "starting"}


@app.post("/api/source/auto")
def import_source_auto(req: AutoSourceRequest):
    """Resolve the real stream status, not whether the URL contains '/live/'.

    For active lives, capture must be stopped before cutting a complete media
    file. A recorded VOD queues analysis plus up to three 720p review renders.
    No content is published automatically.
    """
    if not req.authorized:
        raise HTTPException(400, "Confirme que tem direito de editar ou gravar este conteudo")
    try:
        platform(req.url.strip())
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    if not shutil.which("yt-dlp"):
        raise HTTPException(503, "yt-dlp nao instalado no servidor")
    try:
        kind = detect_source_kind(req.url.strip())
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if kind == "live":
        recording = start_live(LiveStart(url=req.url.strip(), authorized=True,
                                         analyze_after=req.analyze_after,
                                         transcribe_after=req.transcribe_after,
                                         auto_preview=req.auto_preview))
        return {"kind": "live", "live_id": recording["id"],
                "status": "starting", "message": "Encerre a gravacao para iniciar os cortes"}
    task = import_vod(VodDownloadRequest(url=req.url.strip(), authorized=True,
                                         analyze_after=req.analyze_after,
                                         transcribe_after=req.transcribe_after,
                                         auto_preview=req.auto_preview))
    return {"kind": "vod", "task_id": task["task_id"]}


@app.post("/api/live/{live_id}/stop")
def stop_live(live_id: str):
    with LOCK:
        info = LIVE.get(live_id)
        if not info:
            raise HTTPException(status_code=404, detail="Gravacao nao encontrada")
        process = info["process"]
        if process.poll() is None:
            process.terminate()
            info["status"] = "stopping"
    return {"id": live_id, "status": info["status"]}


@app.get("/api/lives")
def list_lives():
    with LOCK:
        result = []
        for live_id, info in LIVE.items():
            recorded = info["target"].stat().st_size if info["target"].exists() else 0
            state = info["status"]
            if state == "starting" and recorded > 0:
                state = "recording"
            preview = preview_status(preview_directory(info["target"]))
            analysis = TASKS.get(info.get("analysis_task_id"), {})
            if state == "completed" and analysis.get("status") in {"queued", "running"}:
                state = "processing"
            result.append({"id": live_id, "platform": info["platform"], "url": info["url"],
                           "status": state, "bytes": recorded or info.get("bytes", 0), "video_id": info.get("video_id"),
                           "message": info.get("message"),
                           "analysis_task_id": info.get("analysis_task_id"),
                           "analysis_status": analysis.get("status"),
                           "analysis_progress": analysis.get("progress", 0),
                           "analysis_message": analysis.get("error") or
                               analysis.get("result", {}).get("transcription_message") or
                               ("Processamento concluído." if analysis.get("status") == "done" else analysis.get("message")),
                           "preview_url": f"/api/live/{live_id}/preview/index.m3u8",
                           "preview_ready": preview["ready"] and state in {"starting", "recording", "stopping"},
                           "preview_has_video": preview.get("has_video"),
                           "preview_message": preview.get("message")})
    return {"lives": list(reversed(result))}


@app.get("/api/live/{live_id}/preview/{asset}")
def live_preview_asset(live_id: str, asset: str):
    with LOCK:
        info = LIVE.get(live_id)
        if not info:
            raise HTTPException(404, "Gravação não encontrada.")
        path = resolve_preview_asset(preview_directory(info["target"]), asset)
    if path is None:
        raise HTTPException(404, "Prévia ainda não disponível.")
    return FileResponse(path, media_type="application/vnd.apple.mpegurl" if asset.endswith(".m3u8") else "video/mp2t",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
