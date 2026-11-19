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
from pydantic import BaseModel, Field

from autoclips import (MEDIA_EXTENSIONS, analyze, download_vod, generate_srt, transcript_local, probe,
                       render_clip, run, sha256_file, workspace, write_json)
from timeline import (TimelineError, apply_command, new_timeline, rebase_words, total_duration)
from timeline_export import render_project
import studio_auth
from integrations import (Vault, build_router, generate_clip_metadata, get_selected,
                          rerank_remote, upload_youtube_video)

APP_VERSION = "0.7.1"
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
PENDING_PUBLISH: set[tuple[str, str]] = set()
PASSWORD = os.environ.get("AUTOCLIPS_PASSWORD", "")

app = FastAPI(title="AutoClips AI Studio", version=APP_VERSION, docs_url=None, redoc_url=None)
CONNECTION_VAULT = Vault(ROOT)
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


def schedule(kind: str, fn, *args, **kwargs) -> str:
    task_id = uuid.uuid4().hex
    with LOCK:
        TASKS[task_id] = {"id": task_id, "kind": kind, "status": "queued", "created_at": utcnow()}
        _save_tasks()

    def do_work():
        with LOCK:
            TASKS[task_id]["status"] = "running"
            _save_tasks()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            with LOCK:
                TASKS[task_id].update(status="error", error=str(exc)[:500], ended_at=utcnow())
                _save_tasks()
        else:
            with LOCK:
                TASKS[task_id].update(status="done", result=result, ended_at=utcnow())
                _save_tasks()

    POOL.submit(do_work)
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
    approved_selection: bool = False
    approved_preview: bool = False


class LiveStart(BaseModel):
    url: str
    authorized: bool
    analyze_after: bool = True
    transcribe_after: bool = False


class VodDownloadRequest(BaseModel):
    url: str
    authorized: bool
    analyze_after: bool = True
    transcribe_after: bool = False


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
    if name not in {"app.css", "app.js", "connections.js", "timeline.css", "timeline.js"}:
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
    data = _transcript_document(video_id)
    return {"available": bool(data.get("words")), "engine": data.get("engine", "none"),
            "words": data.get("words", [])}


@app.post("/api/videos/{video_id}/transcript")
def transcribe_video_locally(video_id: str):
    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="Instale faster-whisper para transcricao local") from exc
    source = Path(video_info(video_id)["path"])
    destination = _project_folder(video_id) / "transcript.json"
    def work():
        result = transcript_local(source, "small")
        with LOCK:
            write_json(destination, result)
        return {"words": len(result.get("words", [])), "engine": result.get("engine")}
    return {"task_id": schedule("transcribe-local", work)}


@app.get("/api/videos/{video_id}/timeline.srt")
def download_timeline_srt(video_id: str):
    with LOCK:
        doc, path = _timeline_document(video_id)
        words = _transcript_document(video_id).get("words", [])
        mapped = rebase_words(doc, words)
        if not mapped:
            raise HTTPException(status_code=404, detail="Transcricao sem palavras nesse corte")
        file = path.parent / f"timeline-v{doc['version']}.srt"
        if not generate_srt(mapped, 0, total_duration(doc), file):
            raise HTTPException(status_code=404, detail="Nao foi possivel gerar as legendas")
    return FileResponse(file, media_type="application/x-subrip", filename=f"autoclips-legendas-v{doc['version']}.srt")


class TimelineRenderRequest(BaseModel):
    layout: Literal["blur", "fit", "crop"] = "blur"
    subtitles: bool = False
    approved_selection: bool = False
    approved_preview: bool = False


def _current_export(video_id: str, quality: str, *, version: int | None = None) -> dict | None:
    info = read_json(_project_folder(video_id) / "timeline_exports.json", {})
    rendered = info.get(quality)
    if not isinstance(rendered, dict):
        return None
    if version is not None and rendered.get("version") != version:
        return None
    if not Path(rendered.get("path", "")).is_file():
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
                                "subtitles": meta["subtitles"], "probe": meta["probe"]}
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
        if quality == "final":
            preview = _current_export(video_id, "preview", version=doc["version"])
            if not preview or preview["layout"] != req.layout or preview["subtitles"] != req.subtitles:
                raise HTTPException(status_code=409, detail="Gere e assista a previa desta versao com estas opcoes")
        words = _transcript_document(video_id).get("words", [])
        dest = folder_path.parent / "renders" / f"timeline-v{doc['version']}-{quality}-{req.layout}-s{int(req.subtitles)}.mp4"

    def work():
        meta = render_project(source, dest, doc_snapshot, words=words,
                              quality=quality, layout=req.layout, subtitles=req.subtitles)
        with LOCK:
            current, _ = _timeline_document(video_id)
            if current["version"] != doc_snapshot["version"]:
                dest.unlink(missing_ok=True)
                raise ValueError("Timeline mudou durante renderizacao; exportacao descartada")
            exports_file = folder_path.parent / "timeline_exports.json"
            records = read_json(exports_file, {})
            records[quality] = meta
            write_json(exports_file, records)
        return {"quality": quality, "url": f"/api/videos/{video_id}/timeline/rendered/{quality}"}

    return {"task_id": schedule("timeline-" + quality, work)}


@app.get("/api/videos/{video_id}/timeline/rendered/{quality}")
def view_timeline_export(video_id: str, quality: str):
    if quality not in {"preview", "final"}:
        raise HTTPException(status_code=404)
    with LOCK:
        doc, _ = _timeline_document(video_id)
        meta = _current_export(video_id, quality, version=doc["version"])
        if not meta:
            raise HTTPException(status_code=404, detail="Arquivo nao exportado para a versao atual")
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
        save_video(item)
        if req.analyze_after:
            model = "small" if req.transcribe_after else None
            job_path = analyze(downloaded, None, model, 5, 40)
            item["job_path"] = str(job_path)
            save_video(item)
        return {"video_id": vid, "name": downloaded.name}

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
    model = "small" if req.transcribe else None

    def job():
        reranker = (lambda clips: rerank_remote(clips, selected)) if selected else None
        result_path = analyze(source, None, model, req.clips, req.seconds, req.ollama_model, reranker)
        item["job_path"] = str(result_path)
        save_video(item)
        return {"video_id": video_id, "clips": len(read_json(result_path / "manifest.json", {})["clips"])}

    return {"task_id": schedule("analyze", job)}


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
    return {"transcription_engine": manifest["transcription_engine"],
            "ranking": manifest["ranking"], "duration": manifest["probe"]["duration"],
            "clips": [{k: v for k, v in c.items() if k not in {"preview", "final"}} | {
                "preview_url": f"/api/videos/{video_id}/rendered/{c['id']}/preview" if c.get("preview") else None,
                "final_url": f"/api/videos/{video_id}/rendered/{c['id']}/final" if c.get("final") else None,
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
        PENDING_PUBLISH.add(publish_key)

    def job():
        try:
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
        finally:
            with LOCK:
                PENDING_PUBLISH.discard(publish_key)

    try:
        return {"task_id": schedule("publish-youtube", job)}
    except Exception:
        with LOCK:
            PENDING_PUBLISH.discard(publish_key)
        raise


def render_one(video_id: str, clip_id: str, quality: str, layout: str, burn: bool) -> dict:
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        selected = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
        if selected is None:
            raise ValueError("Clipe nao existe")
        if quality == "final" and not selected.get("preview"):
            raise ValueError("Primeiro assista e aprove a previa")
        clip = selected.copy()
        source = Path(manifest["source"])
        if not source.exists() or sha256_file(source) != manifest["sha256"]:
            raise ValueError("O original foi alterado; analise novamente")
        has_audio = bool(manifest["probe"]["audio_codec"])
        words = read_json(work / "transcript.json", {"words": []})["words"]
    folder = work / quality
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{clip_id}.mp4"
    subs = folder / f"{clip_id}.srt"
    if not generate_srt(words, clip["start"], clip["end"], subs):
        subs = None
    metadata = render_clip(source, dest, clip, quality=quality, layout=layout,
                           burn=burn, srt=subs, has_audio=has_audio)
    with LOCK:
        manifest, work, _ = get_manifest(video_id)
        current = next(c for c in manifest["clips"] if c["id"] == clip_id)
        if current["start"] != clip["start"] or current["end"] != clip["end"]:
            dest.unlink(missing_ok=True)
            raise ValueError("Intervalo mudou durante renderizacao; tente novamente")
        current[quality] = {"path": str(dest), "subtitles": str(subs) if subs else None,
                            "qa": {"full_decode_ok": True, **metadata}}
        current["status"] = "preview_ready" if quality == "preview" else "final_ready"
        write_json(work / "manifest.json", manifest)
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
    return {"task_id": schedule("render-" + quality, render_one,
                                video_id, req.clip_id, quality, req.layout, req.subtitles)}


@app.get("/api/videos/{video_id}/rendered/{clip_id}/{quality}")
def view_rendered(video_id: str, clip_id: str, quality: str):
    manifest, _, _ = get_manifest(video_id)
    if not re.fullmatch(r"clip_[0-9a-z_]+", clip_id) or quality not in {"preview", "final"}:
        raise HTTPException(status_code=404)
    clip = next((c for c in manifest["clips"] if c["id"] == clip_id), None)
    if not clip or quality not in clip:
        raise HTTPException(status_code=404)
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


def watch_live(live_id: str, proc: subprocess.Popen, target: Path, logs):
    code = proc.wait()
    logs.close()
    with LOCK:
        info = LIVE[live_id]
        info["exit_code"] = code
        info["status"] = "completed" if code == 0 else "interrupted"
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
    except Exception as exc:
        with LOCK:
            info["message"] = f"Remux MP4 indisponivel: {str(exc)[:120]}"
    try:
        metadata = probe(result)
    except Exception as exc:
        with LOCK:
            info["status"] = "error"
            info["message"] = f"Gravacao nao abriu: {str(exc)[:160]}"
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
    if info.get("analyze_after"):
        def auto_analyze():
            model = "small" if info.get("transcribe_after") else None
            job_path = analyze(result, None, model, 5, 40)
            item["job_path"] = str(job_path)
            save_video(item)
            return {"video_id": vid, "proposals": len(read_json(job_path / "manifest.json", {})["clips"])}
        task_id = schedule("analyze-after-live", auto_analyze)
        with LOCK:
            info["analysis_task_id"] = task_id


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
    if req.transcribe_after:
        try:
            import faster_whisper  # noqa: F401
        except ImportError as exc:
            raise HTTPException(status_code=400, detail="Instale faster-whisper para transcrever a live") from exc
    live_id = uuid.uuid4().hex
    target = RECORDINGS / f"live_{plat}_{live_id}.ts"
    logpath = RECORDINGS / f"live_{plat}_{live_id}.log"
    loghandle = logpath.open("w", encoding="utf-8")
    proc = subprocess.Popen(["streamlink", "--retry-streams", "30", "--output", str(target), req.url, "best"],
                            stdout=loghandle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    with LOCK:
        LIVE[live_id] = {"id": live_id, "platform": plat, "url": req.url,
                         "status": "starting", "created_at": utcnow(), "process": proc,
                         "target": target, "logpath": logpath,
                         "analyze_after": req.analyze_after,
                         "transcribe_after": req.transcribe_after}
    threading.Thread(target=watch_live, args=(live_id, proc, target, loghandle), daemon=True).start()
    return {"id": live_id, "platform": plat, "status": "starting"}


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
            result.append({"id": live_id, "platform": info["platform"], "url": info["url"],
                           "status": state, "bytes": recorded, "video_id": info.get("video_id"),
                           "message": info.get("message"),
                           "analysis_task_id": info.get("analysis_task_id")})
    return {"lives": list(reversed(result))}
