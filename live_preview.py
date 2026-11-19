"""Record one Streamlink input and expose the same media as a bounded HLS preview.

The HLS output is a stream copy, not a second provider connection or a video
encoder. Its two-second segment target follows source keyframes, so this is a
delayed live preview, not frame-accurate monitoring or the LL-HLS protocol.
Browser playback still depends on the source codecs (normally H.264/AAC).

Integration: ``start_capture(url, target, loghandle)`` replaces the old
Streamlink-only Popen. Serve only ``resolve_preview_asset`` results behind the
same authentication as recordings. Never serve the provider URL or log file as
a playback URL. Chrome/Firefox need a locally hosted HLS.js player; Safari can
use native HLS. Completed preview directories may be removed once no longer in
use; their sliding window is bounded while a recording is active.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path
from typing import IO


SEGMENT_SECONDS = 2
PLAYLIST_SEGMENTS = 6
STOP_GRACE_SECONDS = 5
MUX_DRAIN_SECONDS = 10
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*\Z")
_ASSET_NAME = re.compile(r"(?:index\.m3u8|segment_[0-9]{6,12}\.ts)\Z")
_CLEANUP_NAME = re.compile(r"(?:(?:index|master)\.m3u8|segment_[0-9]{6,12}\.ts)(?:\.tmp)?\Z")


def preview_directory(target: Path) -> Path:
    """Return the private, per-recording directory next to the recording."""
    return Path(target).with_name(f"{Path(target).stem}_preview")


def build_ffmpeg_command(target: Path, directory: Path | None = None) -> list[str]:
    """Build a tee command run with ``cwd=target.parent``.

    Only application-generated basenames enter tee's mini-language. Keeping
    absolute paths out of it supports data roots containing spaces or colons
    without shell escaping or accidental tee option injection.
    """
    target = Path(target)
    directory = Path(directory) if directory is not None else preview_directory(target)
    if directory.parent.resolve() != target.parent.resolve():
        raise ValueError("O preview deve ficar ao lado da gravação")
    if not _SAFE_NAME.fullmatch(target.name) or not _SAFE_NAME.fullmatch(directory.name):
        raise ValueError("Nome interno de gravação inválido")
    if target.suffix != ".ts":
        raise ValueError("A captura deve ser gravada em MPEG-TS")
    preview = (
        "[f=hls:onfail=ignore:"
        f"hls_time={SEGMENT_SECONDS}:hls_list_size={PLAYLIST_SEGMENTS}:"
        "hls_delete_threshold=1:hls_allow_cache=0:"
        "hls_flags=delete_segments+temp_file+independent_segments:"
        "master_pl_name=master.m3u8:"
        f"hls_segment_filename={directory.name}/segment_%06d.ts]"
        f"{directory.name}/index.m3u8"
    )
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
        "-fflags", "+genpts", "-i", "pipe:0",
        "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
        "-f", "tee", f"[f=mpegts:onfail=abort]{target.name}|{preview}",
    ]


def resolve_preview_asset(directory: Path, filename: str) -> Path | None:
    """Allow only a playlist or a published TS segment inside this directory.

    Temporary segments, logs, recordings, paths and symlinks are never exposed.
    The HTTP layer should return 404 when this returns None. A segment can age
    out immediately after this check; HLS clients handle this by reloading the
    playlist, and the HTTP layer must also tolerate that race.
    """
    if not isinstance(filename, str) or not _ASSET_NAME.fullmatch(filename):
        return None
    directory = Path(directory)
    candidate = directory / filename
    try:
        if directory.is_symlink() or candidate.is_symlink():
            return None
        root = directory.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        if resolved.parent != root or not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def preview_status(directory: Path) -> dict:
    """Report media that actually exists, without revealing local/provider paths."""
    status = {"ready": False, "segment_count": 0, "ended": False,
              "has_video": None, "message": None}
    playlist = resolve_preview_asset(directory, "index.m3u8")
    if playlist is None:
        return status
    try:
        if playlist.stat().st_size > 65536:
            return status
        lines = playlist.read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "#EXTM3U":
            return status
        segments = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
        if not segments or len(segments) > PLAYLIST_SEGMENTS:
            return status
        if any(resolve_preview_asset(directory, name) is None for name in segments):
            return status
        status.update(ready=True, segment_count=len(segments), ended="#EXT-X-ENDLIST" in lines)
        # FFmpeg writes RESOLUTION in the private master only for a video
        # stream. This avoids launching ffprobe on every UI polling request.
        master = Path(directory) / "master.m3u8"
        if not master.is_symlink() and master.is_file() and master.stat().st_size < 65536:
            metadata = master.read_text(encoding="utf-8")
            variant = next((line for line in metadata.splitlines()
                            if line.startswith("#EXT-X-STREAM-INF:")), None)
            if variant:
                status["has_video"] = bool(re.search(r"(?:[:,])RESOLUTION=[0-9]+x[0-9]+(?:,|$)", variant))
                if not status["has_video"]:
                    status["ready"] = False
                    status["message"] = "A fonte contém apenas áudio. A captura continua, mas o editor de clipes exige uma faixa de vídeo."
        return status
    except (OSError, UnicodeError):
        return status


def cleanup_preview(directory: Path) -> int:
    """Remove known HLS files after wait(), preserving any unrelated files.

    Call this only when the capture has stopped. The recording and log live
    outside this directory and are never removed by this helper.
    """
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        return 0
    removed = 0
    for item in directory.iterdir():
        if _CLEANUP_NAME.fullmatch(item.name) and not item.is_dir():
            try:
                item.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    try:
        directory.rmdir()
    except OSError:
        pass
    return removed


class CaptureProcess:
    """Minimal Popen-compatible lifecycle for a Streamlink -> FFmpeg pipeline.

    Stopping closes the upstream stream first, allowing FFmpeg to finish the
    recording and playlist. A failed muxer stops the upstream process too, and
    an unresponsive process is killed after a bounded grace period. The caller
    owns the log handle and closes it after wait(), as for the previous Popen.
    """

    def __init__(self, source: subprocess.Popen, muxer: subprocess.Popen, target: Path):
        self.source = source
        self.muxer = muxer
        self.target = Path(target)
        self.preview_dir = preview_directory(self.target)
        self._stop_requested = threading.Event()
        self._stop_at: float | None = None
        self._drain_at: float | None = None
        self._mux_terminate_at: float | None = None

    @property
    def pid(self) -> int:
        return self.source.pid

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        source_code = self.source.poll()
        muxer_code = self.muxer.poll()
        if source_code is None or muxer_code is None:
            return None
        if muxer_code:
            return muxer_code
        if self._stop_requested.is_set():
            return 0
        return source_code

    def terminate(self) -> None:
        if not self._stop_requested.is_set():
            self._stop_at = time.monotonic()
            self._stop_requested.set()
        if self.source.poll() is None:
            try:
                self.source.terminate()
            except ProcessLookupError:
                pass

    def kill(self) -> None:
        self._stop_requested.set()
        for process in (self.source, self.muxer):
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    def wait(self, timeout: float | None = None) -> int:
        started = time.monotonic()
        while True:
            now = time.monotonic()
            source_code = self.source.poll()
            muxer_code = self.muxer.poll()
            if source_code is not None and muxer_code is not None:
                # poll() also reaps both children; no pipe/log-reader threads
                # remain and the caller can safely close the shared log file.
                return self.poll()
            if timeout is not None and now - started >= timeout:
                raise subprocess.TimeoutExpired(["live-capture"], timeout)
            if muxer_code is not None and source_code is None:
                # Do not normalize this as an intentional user stop. A muxer
                # failure must remain visible even if Streamlink is retrying.
                if self._stop_at is None:
                    self._stop_at = now
                    try:
                        self.source.terminate()
                    except ProcessLookupError:
                        pass
            if source_code is None and self._stop_at is not None:
                if now - self._stop_at >= STOP_GRACE_SECONDS:
                    try:
                        self.source.kill()
                    except ProcessLookupError:
                        pass
            if source_code is not None and muxer_code is None:
                if self._drain_at is None:
                    self._drain_at = now
                if now - self._drain_at >= MUX_DRAIN_SECONDS:
                    if self._mux_terminate_at is None:
                        self._mux_terminate_at = now
                        try:
                            self.muxer.terminate()
                        except ProcessLookupError:
                            pass
                    elif now - self._mux_terminate_at >= STOP_GRACE_SECONDS:
                        try:
                            self.muxer.kill()
                        except ProcessLookupError:
                            pass
            time.sleep(0.05)


def start_capture(url: str, target: Path, loghandle: IO) -> CaptureProcess:
    """Start one provider capture and multiplex it to recording + private HLS.

    Validate/authorize the provider URL in the API layer before calling this.
    No shell is used, and no HLS playlist contains the provider URL or token.
    """
    target = Path(target).resolve()
    directory = preview_directory(target)
    command = build_ffmpeg_command(target, directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Diretório de preview inválido")
    source = subprocess.Popen(
        ["streamlink", "--retry-streams", "30", "--stdout", url, "best"],
        stdout=subprocess.PIPE, stderr=loghandle, stdin=subprocess.DEVNULL,
    )
    try:
        muxer = subprocess.Popen(
            command, cwd=target.parent, stdin=source.stdout,
            stdout=subprocess.DEVNULL, stderr=loghandle,
        )
    except BaseException:
        if source.stdout is not None:
            source.stdout.close()
        if source.poll() is None:
            source.terminate()
        try:
            source.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            source.kill()
            source.wait()
        raise
    # Only FFmpeg keeps the read end. This also lets Streamlink see a broken
    # pipe if the muxer dies, instead of blocking on an unread parent handle.
    if source.stdout is not None:
        source.stdout.close()
    return CaptureProcess(source, muxer, target)
