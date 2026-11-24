"""Local video uploads for /clips — a full video from the user's computer.

Files of any size arrive in sequential chunks (``PUT /api/uploads/<id>?offset=N``)
so a multi-GB upload streams straight to disk, shows progress, and resumes after
a dropped connection. A finished upload is probed with ffprobe and, when the
clips job is created, *moved* (same filesystem: instant, no second copy) into
``projects/<id>/assets/video/``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from backlot.state import PROJECTS_DIR, REPO_ROOT

UPLOAD_DIR = REPO_ROOT / ".backlot" / "uploads"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".ts", ".wmv", ".flv", ".mpg", ".mpeg"}
DISK_MARGIN = 2 * 1024**3          # keep 2 GB free on top of the file itself
STALE_SECONDS = 48 * 3600          # unclaimed uploads older than this are removed

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(uid: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(uid, threading.Lock())


def _dir(uid: str) -> Path:
    if not isinstance(uid, str) or not _ID_RE.match(uid):
        raise KeyError(uid)
    d = UPLOAD_DIR / uid
    if not (d / "upload.json").is_file():
        raise KeyError(uid)
    return d


def _read(uid: str) -> dict[str, Any]:
    return json.loads((_dir(uid) / "upload.json").read_text(encoding="utf-8"))


def _write(d: Path, meta: dict[str, Any]) -> None:
    tmp = d / "upload.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp.replace(d / "upload.json")


def _data_path(d: Path, meta: dict[str, Any]) -> Path:
    return d / f"video{meta['ext']}"


def _public(meta: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "name", "size", "received", "status", "info")
    return {k: meta.get(k) for k in keep}


def cleanup_stale() -> None:
    if not UPLOAD_DIR.is_dir():
        return
    now = time.time()
    for d in UPLOAD_DIR.iterdir():
        try:
            if d.is_dir() and now - d.stat().st_mtime > STALE_SECONDS:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Upload lifecycle
# ---------------------------------------------------------------------------

def start(name: str, size: Any) -> dict[str, Any]:
    name = Path(str(name or "")).name.strip()[:200]
    ext = Path(name).suffix.lower()
    if not name or ext not in VIDEO_EXT:
        raise ValueError(f"Pick a video file ({', '.join(sorted(e.lstrip('.') for e in VIDEO_EXT))}).")
    try:
        size = int(size)
    except (TypeError, ValueError):
        raise ValueError("Unknown file size.")
    if size <= 0:
        raise ValueError("The file is empty.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale()
    free = shutil.disk_usage(UPLOAD_DIR).free
    if free < size + DISK_MARGIN:
        raise ValueError(f"Not enough disk space: the video needs {size / 1024**3:.1f} GB "
                         f"(+2 GB margin) and only {free / 1024**3:.1f} GB are free.")
    uid = uuid.uuid4().hex
    d = UPLOAD_DIR / uid
    d.mkdir()
    meta = {"id": uid, "name": name, "ext": ext, "size": size, "received": 0,
            "status": "uploading", "created_at": time.time()}
    _data_path(d, meta).touch()
    _write(d, meta)
    return _public(meta)


def status(uid: str) -> dict[str, Any]:
    return _public(_read(uid))


def append(uid: str, offset: int, chunks: Iterable[bytes]) -> dict[str, Any]:
    """Write one chunk. ``offset`` must equal the bytes already received, so a
    retried chunk can never duplicate or skip data."""
    with _lock(uid):
        d = _dir(uid)
        meta = _read(uid)
        if meta["status"] != "uploading":
            raise RuntimeError("This upload is already finished.")
        if offset != meta["received"]:
            raise RuntimeError(f"offset mismatch: expected {meta['received']}")
        path = _data_path(d, meta)
        written = 0
        with open(path, "r+b") as f:
            f.seek(offset)
            f.truncate()
            for chunk in chunks:
                if offset + written + len(chunk) > meta["size"]:
                    f.truncate(offset)
                    raise ValueError("More data than the declared file size.")
                f.write(chunk)
                written += len(chunk)
        meta["received"] = offset + written
        _write(d, meta)
        return _public(meta)


def finish(uid: str) -> dict[str, Any]:
    with _lock(uid):
        d = _dir(uid)
        meta = _read(uid)
        if meta["status"] == "ready":
            return _public(meta)
        if meta["received"] != meta["size"]:
            raise RuntimeError(f"Upload incomplete ({meta['received']} of {meta['size']} bytes).")
        info = probe_file(_data_path(d, meta))
        meta["info"] = {**info, "title": Path(meta["name"]).stem}
        meta["status"] = "ready"
        _write(d, meta)
        return _public(meta)


def cancel(uid: str) -> None:
    with _lock(uid):
        d = _dir(uid)
        if _read(uid)["status"] == "claimed":
            raise RuntimeError("This upload already belongs to a clips job.")
        shutil.rmtree(d, ignore_errors=True)


def probe_file(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe was not found (install FFmpeg).")
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
        data = json.loads(out or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError("This file couldn't be read as a video. Is it corrupted?") from exc
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not video:
        raise ValueError("This file has no video track.")
    try:
        duration = float((data.get("format") or {}).get("duration") or video.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError("Couldn't read the video's duration.")
    w, h = video.get("width"), video.get("height")
    rot = 0
    for sd in video.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(sd.get("rotation") or 0))
    if rot in (90, 270):
        w, h = h, w
    return {
        "duration": round(duration, 2),
        "width": w, "height": h,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "has_audio": bool(audio),
        "container": (data.get("format") or {}).get("format_name", ""),
    }


def claim(uid: str, project_id: str) -> dict[str, Any]:
    """Move a ready upload into the project. Returns the source paths
    (relative to the project) the agent works from."""
    with _lock(uid):
        d = _dir(uid)
        meta = _read(uid)
        if meta["status"] != "ready":
            raise ValueError("The video upload hasn't finished yet.")
        video_dir = PROJECTS_DIR / project_id / "assets" / "video"
        video_dir.mkdir(parents=True, exist_ok=True)
        info = meta.get("info") or {}
        browser_ready = (meta["ext"] in (".mp4", ".m4v", ".mov") and info.get("video_codec") == "h264"
                         and info.get("audio_codec") in (None, "aac", "mp3"))
        rel = "assets/video/source.mp4" if browser_ready else f"assets/video/original{meta['ext']}"
        shutil.move(str(_data_path(d, meta)), str(PROJECTS_DIR / project_id / rel))
        meta["status"] = "claimed"
        meta["project_id"] = project_id
        _write(d, meta)
        return {"original_path": rel, "needs_conversion": not browser_ready}


def claim_to(uid: str, dest_dir: Path, stem: str) -> str:
    """Move a ready upload to ``dest_dir/<stem><ext>`` (local quick clips).
    Returns the file name."""
    with _lock(uid):
        d = _dir(uid)
        meta = _read(uid)
        if meta["status"] != "ready":
            raise ValueError("The video upload hasn't finished yet.")
        dest_dir.mkdir(parents=True, exist_ok=True)
        name = f"{stem}{meta['ext']}"
        shutil.move(str(_data_path(d, meta)), str(dest_dir / name))
        meta["status"] = "claimed"
        meta["claimed_by"] = str(dest_dir)
        _write(d, meta)
        return name


def ready_info(uid: str) -> dict[str, Any]:
    meta = _read(uid)
    if meta["status"] != "ready":
        raise ValueError("The video upload hasn't finished yet." if meta["status"] == "uploading"
                         else "This upload was already used; upload the video again.")
    return {"upload_id": uid, "name": meta["name"], "size": meta["size"], **(meta.get("info") or {})}

