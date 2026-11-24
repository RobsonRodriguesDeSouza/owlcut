"""Backlot server — FastAPI app: board state API, SSE change feed, media.

The watcher observes ``projects/`` with watchfiles; on any change it bumps a
per-project version and wakes SSE subscribers, who tell the browser to
refetch state. The server never writes to project directories.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backlot.state import PROJECTS_DIR, REPO_ROOT, list_projects, load_board_state, summarize_project
from backlot import clipper
from backlot import ranking
from backlot import uploads
from backlot import quickclips
from backlot import branding
from backlot import exports
from backlot import library
from backlot import social_tiktok
from backlot.jobs import JobManager
from backlot.jobs import options as job_options

log = logging.getLogger("owlcut.server")

UI_DIR = Path(__file__).resolve().parent / "ui"
THUMB_CACHE_DIR = REPO_ROOT / ".backlot" / "thumbs"
THUMB_WIDTHS = (320, 640, 960)

# Paths inside a project whose changes are pure noise for the board.
_IGNORE_PARTS = {"node_modules", ".git", "__pycache__", ".cache"}

SSE_HEARTBEAT_SECONDS = 15


_UI_REF = re.compile(r'(["\'])/ui/([\w./-]+\.(?:css|js))\1')


def _ui_html(name: str, assets: tuple[str, ...] = ()) -> HTMLResponse:
    """Serve a UI page with every /ui/*.css|js reference cache-busted by mtime.

    ``assets`` is accepted for backwards compatibility; all local CSS/JS
    references in the page are versioned automatically.
    """
    html = (UI_DIR / name).read_text(encoding="utf-8")

    def _version(m: re.Match) -> str:
        quote, asset = m.group(1), m.group(2)
        path = UI_DIR / asset
        if not path.is_file():
            return m.group(0)
        return f"{quote}/ui/{asset}?v={int(path.stat().st_mtime)}{quote}"

    return HTMLResponse(_UI_REF.sub(_version, html))


class ChangeHub:
    """Fan-out of project-change notifications to SSE subscribers.

    Subscriptions are filtered: a board subscribed to one project only ever
    receives that project's ids, so unrelated-project bursts can't flood its
    queue and starve out the one notification it actually needs.
    """

    def __init__(self) -> None:
        self._subscribers: dict[asyncio.Queue, Optional[str]] = {}

    def subscribe(self, project_id: Optional[str] = None) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers[q] = project_id
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, project_id: str) -> None:
        for q, only in list(self._subscribers.items()):
            if only is not None and only != project_id:
                continue
            try:
                q.put_nowait(project_id)
            except asyncio.QueueFull:
                # Queue holds only THIS subscriber's relevant ids, so a full
                # queue already guarantees a pending wake-up → safe to drop.
                pass


hub = ChangeHub()

# Library summaries are expensive to derive (full state parse per project);
# cache per project and invalidate from the watcher.
_summary_cache: dict[str, dict] = {}


def _invalidate_summary(project_id: str) -> None:
    _summary_cache.pop(project_id, None)


def _cached_summaries() -> list[dict]:
    if not PROJECTS_DIR.is_dir():
        return []
    summaries = []
    for entry in sorted(PROJECTS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith(("_", ".")):
            continue
        cached = _summary_cache.get(entry.name)
        if cached is None:
            try:
                cached = summarize_project(entry)
            except Exception:
                cached = {
                    "project_id": entry.name, "title": entry.name,
                    "pipeline_type": "unknown", "has_pipeline_state": False,
                    "poster": None, "live": False, "last_activity": 0,
                    "active_stage": None, "awaiting_human": False,
                    "stage_states": [], "completed_count": 0,
                    "render_count": 0, "scene_count": 0, "error": "unreadable",
                }
            _summary_cache[entry.name] = cached
        summaries.append(cached)
    summaries.sort(key=lambda s: (not s["live"], -(s["last_activity"] or 0)))
    return summaries


# Watch-loop hot path: pure string comparison, no per-path filesystem calls
# (change batches can be thousands of paths during a render).
import os as _os

_PROJECTS_ROOT_STR = _os.path.normcase(str(PROJECTS_DIR.resolve()))


def _project_of_change(path_str: str) -> Optional[str]:
    """Map a changed filesystem path to a project id (None = irrelevant)."""
    norm = _os.path.normcase(_os.path.normpath(path_str))
    if not norm.startswith(_PROJECTS_ROOT_STR):
        return None
    rel = norm[len(_PROJECTS_ROOT_STR):].lstrip("\\/")
    if not rel:
        return None
    parts = rel.replace("\\", "/").split("/")
    if _IGNORE_PARTS.intersection(parts):
        return None
    return parts[0]


async def _watch_projects() -> None:
    """Background task: watch projects/ and publish debounced changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        return  # watcher unavailable → board still works via manual refresh
    if not PROJECTS_DIR.is_dir():
        return
    async for changes in awatch(PROJECTS_DIR, recursive=True, step=400):
        touched: set[str] = set()
        for _change, path_str in changes:
            pid = _project_of_change(path_str)
            if pid:
                touched.add(pid)
        for pid in touched:
            _invalidate_summary(pid)
            hub.publish(pid)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Own and cleanly stop the project watcher with FastAPI's lifespan API."""

    jobs = getattr(app.state, "jobs", None)
    if jobs is not None:
        await asyncio.to_thread(jobs.recover)  # only a serving process marks orphaned turns
    task = asyncio.create_task(_watch_projects())
    app.state.watch_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def create_app() -> FastAPI:
    app = FastAPI(title="Backlot", docs_url=None, redoc_url=None, lifespan=_lifespan)

    # ---- API ----------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "app": "backlot"}

    @app.get("/api/projects")
    async def projects() -> list:
        return await asyncio.to_thread(_cached_summaries)

    @app.get("/api/project/{project_id}/state")
    async def project_state(project_id: str) -> dict:
        project_dir = _safe_project_dir(project_id)
        return await asyncio.to_thread(load_board_state, project_dir)

    @app.get("/api/project/{project_id}/events")
    async def project_events(project_id: str, request: Request) -> StreamingResponse:
        _safe_project_dir(project_id)  # 404 early for unknown projects

        async def stream():
            q = hub.subscribe(project_id)
            try:
                yield _sse({"type": "hello", "project_id": project_id})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    # Coalesce bursts: drain anything else queued.
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": project_id})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    @app.get("/api/library/events")
    async def library_events(request: Request) -> StreamingResponse:
        async def stream():
            q = hub.subscribe()
            try:
                yield _sse({"type": "hello"})
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        changed = await asyncio.wait_for(q.get(), timeout=SSE_HEARTBEAT_SECONDS)
                    except asyncio.TimeoutError:
                        yield _sse({"type": "heartbeat", "ts": time.time()})
                        continue
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    yield _sse({"type": "change", "project_id": changed})
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # ---- Thumbnails (downscaled, cached on disk) ------------------------

    @app.get("/thumb/{project_id}/{file_path:path}")
    async def thumb(project_id: str, file_path: str, w: int = 640) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        width = min(THUMB_WIDTHS, key=lambda x: abs(x - w))
        cached = await asyncio.to_thread(_thumbnail_for, target, width)
        if cached is None:
            # Never fall back to raw video bytes for an <img> consumer (F-03);
            # non-thumbable images are safe to serve as-is.
            if target.suffix.lower() in {".mp4", ".webm", ".mov"}:
                raise HTTPException(status_code=404, detail="no poster frame available")
            return FileResponse(target)
        return FileResponse(cached, media_type="image/jpeg")

    # ---- Media (range requests handled by FileResponse) ---------------

    @app.get("/media/{project_id}/{file_path:path}")
    async def media(project_id: str, file_path: str) -> FileResponse:
        project_dir = _safe_project_dir(project_id)
        target = (project_dir / file_path).resolve()
        try:
            target.relative_to(project_dir.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="path escapes project")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="media not found")
        return FileResponse(target)

    # ---- Jobs (create & steer productions from the browser) ----------

    jobs = JobManager()
    app.state.jobs = jobs

    def _same_origin(request: Request) -> None:
        # Starting a job runs an agent with shell access, so refuse anything a
        # foreign page could trigger: require JSON (forces a CORS preflight)
        # and an Origin/Referer on this same loopback host.
        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(status_code=415, detail="JSON required")
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        host = request.headers.get("host", "")
        if not origin or urlparse(origin).netloc != host:
            raise HTTPException(status_code=403, detail="cross-origin request refused")

    @app.get("/api/options")
    async def api_options() -> dict:
        return await asyncio.to_thread(job_options)

    @app.get("/api/jobs")
    async def api_jobs() -> list:
        return await asyncio.to_thread(jobs.list)

    @app.get("/api/jobs/{job_id}")
    async def api_job(job_id: str) -> dict:
        try:
            return await asyncio.to_thread(jobs.get, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")

    @app.post("/api/jobs")
    async def api_create_job(request: Request) -> dict:
        _same_origin(request)
        form = await request.json()
        if not isinstance(form, dict):
            raise HTTPException(status_code=400, detail="invalid form")
        try:
            job = await asyncio.to_thread(jobs.create, form)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=503, detail=str(e))
        return {"id": job["id"]}

    @app.post("/api/jobs/{job_id}/reply")
    async def api_reply(job_id: str, request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        try:
            await asyncio.to_thread(jobs.reply, job_id, str((body or {}).get("text", "")))
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/close")
    async def api_close(job_id: str, request: Request) -> dict:
        _same_origin(request)
        try:
            job = await asyncio.to_thread(jobs.close, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        return {"ok": True, "status": job.get("status")}

    @app.post("/api/jobs/{job_id}/reopen")
    async def api_reopen(job_id: str, request: Request) -> dict:
        _same_origin(request)
        try:
            job = await asyncio.to_thread(jobs.reopen, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        return {"ok": True, "status": job.get("status")}

    @app.post("/api/jobs/{job_id}/delete")
    async def api_delete_job(job_id: str, request: Request) -> dict:
        _same_origin(request)
        body = await request.json() or {}
        try:
            report = await asyncio.to_thread(jobs.delete, job_id, bool(body.get("include_exports")),
                                             bool(body.get("dry_run")))
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        return {"ok": True, **report}

    @app.post("/api/jobs/{job_id}/cancel")
    async def api_cancel(job_id: str, request: Request) -> dict:
        _same_origin(request)
        try:
            await asyncio.to_thread(jobs.cancel, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        return {"ok": True}

    # ---- /clips (YouTube → social clips) ----------------------------------

    @app.get("/api/clips/options")
    async def api_clips_options() -> dict:
        return clipper.options()

    @app.post("/api/clips/probe")
    async def api_clips_probe(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        try:
            return await asyncio.to_thread(clipper.probe, str((body or {}).get("url", "")))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except ImportError:
            raise HTTPException(status_code=503, detail="yt-dlp is not installed (requirements-clips.txt).")

    # ---- /api/uploads (full video from the user's computer) --------------

    def _upload_call(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="upload not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))

    def _same_origin_raw(request: Request) -> None:
        # Chunks are raw bytes, not JSON: a custom header forces the CORS
        # preflight a foreign page can't pass, plus the same Origin check.
        if request.headers.get("x-owlcut-upload") != "1":
            raise HTTPException(status_code=403, detail="upload header missing")
        origin = request.headers.get("origin") or request.headers.get("referer") or ""
        if not origin or urlparse(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(status_code=403, detail="cross-origin request refused")

    @app.post("/api/uploads")
    async def api_upload_start(request: Request) -> dict:
        _same_origin(request)
        body = await request.json() or {}
        return await asyncio.to_thread(_upload_call, uploads.start, body.get("name"), body.get("size"))

    @app.get("/api/uploads/{uid}")
    async def api_upload_status(uid: str) -> dict:
        return await asyncio.to_thread(_upload_call, uploads.status, uid)

    @app.put("/api/uploads/{uid}")
    async def api_upload_chunk(uid: str, offset: int, request: Request) -> dict:
        _same_origin_raw(request)
        data = await request.body()  # client sends ≤16 MB per chunk
        return await asyncio.to_thread(_upload_call, uploads.append, uid, offset, [data])

    @app.post("/api/uploads/{uid}/finish")
    async def api_upload_finish(uid: str, request: Request) -> dict:
        _same_origin(request)
        return await asyncio.to_thread(_upload_call, uploads.finish, uid)

    @app.post("/api/uploads/{uid}/cancel")
    async def api_upload_cancel(uid: str, request: Request) -> dict:
        _same_origin(request)
        await asyncio.to_thread(_upload_call, uploads.cancel, uid)
        return {"ok": True}

    @app.get("/api/clips/{job_id}")
    async def api_clips_job(job_id: str) -> dict:
        try:
            job = await asyncio.to_thread(jobs.get, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        clips = await asyncio.to_thread(clipper.load_clips, job["project_id"])
        keep = ("id", "project_id", "kind", "title", "status", "last_message", "source", "form",
                "cost_usd", "turns", "renders", "stages", "project_exists", "updated_at")
        out = {k: job.get(k) for k in keep}
        # what the agent is doing right now (last tool call / remark) and when the
        # user last replied, so the review page can show live render progress
        log = job.get("log") or []
        act = next((e for e in reversed(log) if e.get("kind") in ("tool", "assistant")), None)
        out["activity"] = {"text": str(act.get("text", ""))[:200], "ts": act.get("ts")} if act else None
        reply = next((e for e in reversed(log) if e.get("kind") == "user" and not e.get("label")), None)
        out["last_reply_ts"] = reply.get("ts") if reply else None
        return {"job": out, "clips": clips}

    # ---- /ranking (local ranking videos, no agent) -----------------------

    def _ranking_call(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="ranking not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.get("/api/rankings/options")
    async def api_ranking_options() -> dict:
        return await asyncio.to_thread(ranking.options)

    @app.get("/api/rankings")
    async def api_rankings() -> list:
        return await asyncio.to_thread(ranking.list_all)

    @app.post("/api/rankings")
    async def api_ranking_create(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        return await asyncio.to_thread(ranking.create, str((body or {}).get("title", "")))

    @app.get("/api/rankings/{rid}")
    async def api_ranking_get(rid: str) -> dict:
        doc = await asyncio.to_thread(_ranking_call, ranking.get, rid)
        return {**doc, "problems": ranking.problems(doc)}

    @app.post("/api/rankings/{rid}")
    async def api_ranking_save(rid: str, request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid body")
        doc = await asyncio.to_thread(_ranking_call, ranking.save, rid, body)
        return {**doc, "problems": ranking.problems(doc)}

    @app.post("/api/rankings/{rid}/render")
    async def api_ranking_render(rid: str, request: Request) -> dict:
        _same_origin(request)
        return await asyncio.to_thread(_ranking_call, ranking.start_render, rid)

    @app.post("/api/rankings/{rid}/delete")
    async def api_ranking_delete(rid: str, request: Request) -> dict:
        _same_origin(request)
        await asyncio.to_thread(_ranking_call, ranking.delete, rid)
        return {"ok": True}

    @app.get("/api/rankings/{rid}/video")
    async def api_ranking_video(rid: str, download: int = 0) -> FileResponse:
        path = await asyncio.to_thread(_ranking_call, ranking.video_path, rid)
        if download:
            return FileResponse(path, media_type="video/mp4", filename=f"{rid}.mp4")
        return FileResponse(path, media_type="video/mp4")

    # ---- /api/quickclips (local clips, no Claude) ------------------------

    def _quick_call(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))

    @app.get("/api/quickclips/options")
    async def api_quick_options() -> dict:
        return quickclips.options()

    @app.get("/api/quickclips")
    async def api_quick_list() -> list:
        return await asyncio.to_thread(quickclips.list_all)

    @app.post("/api/quickclips")
    async def api_quick_create(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="invalid form")
        return await asyncio.to_thread(_quick_call, quickclips.create, body)

    @app.get("/api/quickclips/{qid}")
    async def api_quick_get(qid: str) -> dict:
        return await asyncio.to_thread(_quick_call, quickclips.get, qid)

    @app.post("/api/quickclips/{qid}/cancel")
    async def api_quick_cancel(qid: str, request: Request) -> dict:
        _same_origin(request)
        return await asyncio.to_thread(_quick_call, quickclips.cancel, qid)

    @app.post("/api/quickclips/{qid}/delete")
    async def api_quick_delete(qid: str, request: Request) -> dict:
        _same_origin(request)
        body = await request.json() or {}
        report = await asyncio.to_thread(_quick_call, quickclips.delete, qid,
                                         bool(body.get("include_exports")), bool(body.get("dry_run")))
        return {"ok": True, **(report or {})}

    @app.get("/api/quickclips/{qid}/clip/{cid}")
    async def api_quick_clip(qid: str, cid: str, download: int = 0) -> FileResponse:
        path = await asyncio.to_thread(_quick_call, quickclips.clip_path, qid, cid, "video")
        if download:
            return FileResponse(path, media_type="video/mp4", filename=f"{qid}-{cid}.mp4")
        return FileResponse(path, media_type="video/mp4")

    @app.get("/api/quickclips/{qid}/thumb/{cid}")
    async def api_quick_thumb(qid: str, cid: str) -> FileResponse:
        path = await asyncio.to_thread(_quick_call, quickclips.clip_path, qid, cid, "thumb")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/quickclips/{qid}/zip")
    async def api_quick_zip(qid: str) -> FileResponse:
        path = await asyncio.to_thread(_quick_call, quickclips.bundle_zip, qid)
        # the same clips also land in Documents/OwlCutVideos (re-created if that folder was removed)
        await asyncio.to_thread(quickclips.export, qid)
        return FileResponse(path, media_type="application/zip", filename=f"{qid}.zip")

    def _folder_info(folder: Path) -> dict:
        try:
            shown = "~/" + str(folder.relative_to(Path.home()))
        except ValueError:
            shown = str(folder)
        return {"folder": str(folder), "shown": shown}

    def _reveal(target: Optional[Path]) -> dict:
        """Open a folder in Finder (the server runs on the user's own Mac)."""
        if not target or not target.is_dir():
            raise HTTPException(status_code=404, detail="Not saved to OwlCutVideos yet.")
        if sys.platform == "darwin" and shutil.which("open"):
            subprocess.Popen(["open", str(target)])
            return {"opened": True}
        return {"opened": False, "folder": str(target)}

    @app.get("/api/quickclips/{qid}/saved")
    async def api_quick_saved(qid: str) -> dict:
        folder = await asyncio.to_thread(exports.existing_folder, "clips", f"quick:{qid}")
        return _folder_info(folder) if folder else {"folder": None}

    @app.post("/api/quickclips/{qid}/save")
    async def api_quick_save(qid: str, request: Request) -> dict:
        _same_origin(request)
        await asyncio.to_thread(_quick_call, quickclips.get, qid)
        folder = await asyncio.to_thread(quickclips.export, qid)
        if not folder:
            raise HTTPException(status_code=500, detail="Couldn't save the clips to OwlCutVideos.")
        return _folder_info(folder)

    @app.post("/api/quickclips/{qid}/reveal")
    async def api_quick_reveal(qid: str, request: Request) -> dict:
        _same_origin(request)
        return _reveal(await asyncio.to_thread(exports.existing_folder, "clips", f"quick:{qid}"))

    # ---- /publish (publishing kit) --------------------------------------

    def _clips_job(job_id: str) -> dict:
        job = jobs.get(job_id)
        if job.get("kind") != "clips":
            raise KeyError(job_id)
        return job

    def _platforms(job: dict) -> list:
        return list((job.get("form") or {}).get("platforms") or [])

    # ---- Branding (watermark) ------------------------------------------

    @app.get("/api/branding")
    async def api_branding() -> dict:
        return {"settings": await asyncio.to_thread(branding.get), "options": branding.options()}

    @app.put("/api/branding")
    async def api_branding_save(request: Request) -> dict:
        _same_origin(request)
        body = await request.json() or {}
        return {"settings": await asyncio.to_thread(branding.save, body)}

    @app.put("/api/branding/logo")
    async def api_branding_logo(request: Request) -> dict:
        _same_origin_raw(request)
        data = await request.body()
        try:
            return {"settings": await asyncio.to_thread(branding.set_logo, data)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/branding/logo/remove")
    async def api_branding_logo_remove(request: Request) -> dict:
        _same_origin(request)
        return {"settings": await asyncio.to_thread(branding.remove_logo)}

    @app.put("/api/branding/side-logo")
    async def api_branding_side_logo(request: Request) -> dict:
        _same_origin_raw(request)
        data = await request.body()
        try:
            return {"settings": await asyncio.to_thread(branding.set_side_logo, data)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/branding/side-logo/remove")
    async def api_branding_side_logo_remove(request: Request) -> dict:
        _same_origin(request)
        return {"settings": await asyncio.to_thread(branding.remove_side_logo)}

    @app.get("/api/branding/side-logo.png")
    async def api_branding_side_logo_png(size: int = 256, shape: str = "") -> Response:
        try:
            data = await asyncio.to_thread(branding.side_logo_png, size, shape or None)
        except KeyError:
            raise HTTPException(status_code=404, detail="no side logo")
        return Response(content=data, media_type="image/png")

    @app.get("/api/branding/logo.png")
    async def api_branding_logo_png(size: int = 256, ring: Optional[int] = None) -> Response:
        try:
            data = await asyncio.to_thread(branding.round_logo_png, size, None if ring is None else bool(ring))
        except KeyError:
            raise HTTPException(status_code=404, detail="no logo")
        return Response(content=data, media_type="image/png")

    @app.get("/api/branding/samples")
    async def api_branding_samples() -> list:
        return await asyncio.to_thread(branding.list_samples)

    @app.get("/api/branding/preview.png")
    async def api_branding_preview(kind: str = "clips", w: int = 432, draft: str = "",
                                   sample: str = "demo-wide", t: float = 0.35) -> Response:
        kind = kind if kind in branding.KINDS else "clips"
        try:
            d = json.loads(draft) if draft else None
        except ValueError:
            d = None
        data = await asyncio.to_thread(branding.preview_png, kind, max(160, min(1080, w)),
                                       d if isinstance(d, dict) else None, sample[:200], t)
        return Response(content=data, media_type="image/png")

    # ---- Saved in OwlCutVideos (pick a video, post it) -----------------

    def _lib_call(fn, *args):
        try:
            return fn(*args)
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found — was it moved or deleted in Finder?")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/saved")
    async def api_saved() -> dict:
        items = await asyncio.to_thread(library.list_saved)
        return {"root": _folder_info(exports.root())["shown"], "productions": items}

    @app.get("/api/saved/video")
    async def api_saved_video(path: str, download: int = 0) -> FileResponse:
        p = await asyncio.to_thread(_lib_call, library.resolve, path)
        if download:
            return FileResponse(p, media_type="video/mp4", filename=p.name)
        return FileResponse(p, media_type="video/mp4")

    @app.get("/api/saved/thumb")
    async def api_saved_thumb(path: str) -> FileResponse:
        p = await asyncio.to_thread(_lib_call, library.thumb, path)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/saved/post")
    async def api_saved_post(path: str) -> dict:
        return await asyncio.to_thread(_lib_call, library.post_state, path)

    @app.put("/api/saved/post")
    async def api_saved_post_save(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="Invalid body.")
        return await asyncio.to_thread(_lib_call, library.save_post, body.get("path"), body.get("platform"), body)

    @app.post("/api/saved/reveal")
    async def api_saved_reveal(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        return {"opened": await asyncio.to_thread(_lib_call, library.reveal, (body or {}).get("path"))}

    # ---- Social accounts: TikTok ----------------------------------------

    def _tk_call(fn, *args):
        try:
            return fn(*args)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except KeyError:
            raise HTTPException(status_code=404, detail="Not found — was it moved or deleted in Finder?")
        except social_tiktok.TikTokError as e:
            raise HTTPException(status_code=502, detail=f"TikTok: {e}")
        except OSError as e:
            raise HTTPException(status_code=502, detail=f"Couldn't reach TikTok: {e}")

    @app.get("/api/social/tiktok")
    async def api_tiktok() -> dict:
        return await asyncio.to_thread(social_tiktok.status)

    @app.put("/api/social/tiktok/keys")
    async def api_tiktok_keys(request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        return await asyncio.to_thread(_tk_call, social_tiktok.save_keys, body if isinstance(body, dict) else {})

    @app.post("/api/social/tiktok/forget")
    async def api_tiktok_forget(request: Request) -> dict:
        _same_origin(request)
        return await asyncio.to_thread(social_tiktok.forget_keys)

    @app.post("/api/social/tiktok/disconnect")
    async def api_tiktok_disconnect(request: Request) -> dict:
        _same_origin(request)
        await asyncio.to_thread(social_tiktok.disconnect)
        return await asyncio.to_thread(social_tiktok.status)

    @app.get("/api/social/tiktok/connect")
    async def api_tiktok_connect() -> Response:
        try:
            return RedirectResponse(await asyncio.to_thread(social_tiktok.auth_url), status_code=302)
        except ValueError as e:
            return RedirectResponse("/settings?tiktok_error=" + quote(str(e)) + "#social", status_code=302)

    @app.get("/api/social/tiktok/callback")
    async def api_tiktok_callback(code: str = "", state: str = "", error: str = "", error_description: str = "") -> Response:
        try:
            await asyncio.to_thread(social_tiktok.finish_login, code, state, error_description or error)
            return RedirectResponse("/settings?tiktok=connected#social", status_code=302)
        except (social_tiktok.TikTokError, OSError, KeyError) as e:
            return RedirectResponse("/settings?tiktok_error=" + quote(str(e)[:300]) + "#social", status_code=302)

    @app.post("/api/saved/tiktok")
    async def api_saved_to_tiktok(request: Request) -> dict:
        """Send a saved video to the connected TikTok account's inbox (draft)."""
        _same_origin(request)
        body = await request.json()
        rel = (body or {}).get("path")
        path = await asyncio.to_thread(_lib_call, library.resolve, rel)
        res = await asyncio.to_thread(_tk_call, social_tiktok.upload_to_inbox, path)
        await asyncio.to_thread(library.mark_sent, rel, "tiktok", res["publish_id"])
        return res

    @app.get("/api/social/tiktok/publish/{publish_id}")
    async def api_tiktok_publish_status(publish_id: str) -> dict:
        if not re.fullmatch(r"[\w.~-]{1,128}", publish_id):
            raise HTTPException(status_code=400, detail="Invalid id.")
        return await asyncio.to_thread(_tk_call, social_tiktok.publish_status, publish_id)

    @app.get("/api/publish")
    async def api_publish_list() -> list:
        def _list() -> list:
            out = []
            for j in jobs.list():
                if j.get("kind") != "clips":
                    continue
                try:
                    job = jobs.get(j["id"])
                except KeyError:
                    continue
                st = clipper.publish_state(job["project_id"], _platforms(job))
                slots = [e for c in st["clips"] for e in c["platforms"].values()]
                first = st["clips"][0] if st["clips"] else None
                out.append({
                    "id": j["id"], "title": j.get("title"), "status": j.get("status"), "updated_at": j.get("updated_at"),
                    "project_id": job["project_id"], "clips": len(st["clips"]),
                    "posts": len(slots), "posted": sum(1 for e in slots if e["posted"]),
                    "platforms": [st["labels"][p] for p in st["platforms"]],
                    "poster": (first or {}).get("thumbnail"),
                })
            return out
        return await asyncio.to_thread(_list)

    @app.get("/api/publish/{job_id}")
    async def api_publish_job(job_id: str) -> dict:
        try:
            job = await asyncio.to_thread(_clips_job, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        state = await asyncio.to_thread(clipper.publish_state, job["project_id"], _platforms(job))
        return {"job": {k: job.get(k) for k in ("id", "project_id", "title", "status", "source")}, **state}

    @app.post("/api/publish/{job_id}/{clip_id}/{platform}")
    async def api_publish_save(job_id: str, clip_id: str, platform: str, request: Request) -> dict:
        _same_origin(request)
        body = await request.json()
        try:
            job = await asyncio.to_thread(_clips_job, job_id)
            return await asyncio.to_thread(clipper.save_edit, job["project_id"], clip_id, platform, body or {})
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/publish/{job_id}/kit.zip")
    async def api_publish_zip(job_id: str) -> Response:
        try:
            job = await asyncio.to_thread(_clips_job, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        data = await asyncio.to_thread(clipper.build_bundle, job["project_id"], _platforms(job), job.get("title") or "")
        name = f"kit-{job['project_id']}.zip"
        return Response(content=data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    def _save_kit(job: dict) -> dict:
        folder = clipper.save_kit(job["project_id"], _platforms(job), job.get("title") or "", job.get("created_at"))
        return _folder_info(folder)

    @app.post("/api/publish/{job_id}/kit/save")
    async def api_publish_save(job_id: str, request: Request) -> dict:
        _same_origin(request)
        try:
            job = await asyncio.to_thread(_clips_job, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        try:
            return await asyncio.to_thread(_save_kit, job)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Couldn't save the kit: {exc}")

    @app.post("/api/publish/{job_id}/kit/reveal")
    async def api_publish_reveal(job_id: str, request: Request) -> dict:
        """Open the saved kit in Finder (the server runs on the user's own Mac)."""
        _same_origin(request)
        try:
            job = await asyncio.to_thread(_clips_job, job_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="job not found")
        folder = exports.existing_folder("clips", f"project:{job['project_id']}")
        return _reveal(folder / clipper.KIT_DIR if folder else None)

    # ---- UI ------------------------------------------------------------

    @app.get("/publish")
    async def publish_page() -> HTMLResponse:
        return _ui_html("publish.html")

    @app.get("/publish/{job_id}")
    async def publish_job_page(job_id: str) -> HTMLResponse:
        return _ui_html("publish.html")

    @app.get("/ranking")
    async def ranking_page() -> HTMLResponse:
        return _ui_html("ranking.html")

    @app.get("/ranking/{rid}")
    async def ranking_edit_page(rid: str) -> HTMLResponse:
        return _ui_html("ranking.html")

    @app.get("/settings")
    async def settings_page() -> HTMLResponse:
        return _ui_html("settings.html")

    @app.get("/clips")
    async def clips_page() -> HTMLResponse:
        return _ui_html("clips.html")

    @app.get("/clips/quick/{qid}")
    async def quick_clips_page(qid: str) -> HTMLResponse:
        return _ui_html("quick.html")

    @app.get("/clips/{job_id}")
    async def clips_review_page(job_id: str) -> HTMLResponse:
        return _ui_html("clips-review.html")

    @app.get("/new")
    async def new_page() -> HTMLResponse:
        return _ui_html("new.html", ("board.css", "create.css", "new.js"))

    @app.get("/job/{job_id}")
    async def job_page(job_id: str) -> HTMLResponse:
        return _ui_html("job.html", ("board.css", "create.css", "job.js"))

    @app.get("/p/{project_id}")
    async def board_page(project_id: str) -> HTMLResponse:
        return _ui_html("board.html", ("board.css", "board.js"))

    @app.get("/p/{project_path:path}")
    async def board_page_path(project_path: str) -> HTMLResponse:
        return _ui_html("board.html", ("board.css", "board.js"))

    @app.get("/")
    async def library_page() -> HTMLResponse:
        return _ui_html("index.html", ("board.css", "library.js"))

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=UI_DIR), name="ui")
    if clipper.PREVIEW_DIR.is_dir():
        app.mount("/clip-previews", StaticFiles(directory=clipper.PREVIEW_DIR), name="clip-previews")

    # The board is a long-lived SPA: a tab keeps running whatever board.js it
    # loaded, and browsers heuristically cache /ui assets. no-cache forces a
    # conditional revalidation (cheap 304 via ETag) on every load so UI fixes
    # show up on a plain refresh. Media/thumb responses keep normal caching.
    @app.middleware("http")
    async def ui_no_cache(request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path in ("/", "/new", "/clips", "/publish", "/ranking", "/settings", "/api/saved") or path.startswith(("/api/branding", "/ui", "/p/", "/job/", "/api/jobs", "/clips/", "/api/clips/", "/api/uploads", "/api/quickclips", "/publish/", "/api/publish", "/api/saved/post", "/api/social", "/ranking/")) or (path.startswith("/api/rankings") and not path.endswith("/video")):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # Loopback binding alone does not prevent browser DNS-rebinding requests.
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"], www_redirect=False,
    )
    return app


def _safe_project_dir(project_id: str) -> Path:
    # ':' rejects Windows drive-relative ids like "C:" (PROJECTS_DIR / "C:"
    # collapses back to PROJECTS_DIR itself).
    if any(c in project_id for c in "/\\:") or project_id in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid project id")
    project_dir = PROJECTS_DIR / project_id
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"unknown project: {project_id}")
    return project_dir


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _thumbnail_for(source: Path, width: int) -> Optional[Path]:
    """Downscale an image (or extract a video poster frame) to a cached JPEG."""
    suffix = source.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    is_video = suffix in {".mp4", ".webm", ".mov"}
    if not (is_image or is_video):
        return None
    try:
        import hashlib
        stat = source.stat()
        key = hashlib.sha1(
            f"{source}|{stat.st_mtime_ns}|{stat.st_size}|{width}".encode()
        ).hexdigest()[:20]
        cached = THUMB_CACHE_DIR / f"{key}.jpg"
        if cached.is_file():
            return cached
        THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        # Unique temp per request — concurrent misses for the same source
        # must not write (and replace from) the same temp file.
        import uuid
        tmp = THUMB_CACHE_DIR / f"{key}.{uuid.uuid4().hex[:8]}.tmp.jpg"
        if is_video:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-ss", "1.5",
                 "-i", str(source), "-frames:v", "1",
                 "-vf", f"scale={width}:-2", str(tmp)],
                capture_output=True, timeout=30,
            )
            if result.returncode != 0 or not tmp.is_file():
                return None
        else:
            from PIL import Image
            with Image.open(source) as img:
                img = img.convert("RGB")
                img.thumbnail((width, width * 3))
                img.save(tmp, "JPEG", quality=82)
        tmp.replace(cached)
        return cached
    except Exception:
        return None


app = create_app()
