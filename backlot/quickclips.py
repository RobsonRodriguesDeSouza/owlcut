"""OwlCut quick clips — a long video → vertical captioned clips, fully local.

No Claude involved (zero plan usage). Everything runs on this machine:

1. source: a file uploaded through /api/uploads, or a YouTube link (yt-dlp);
2. transcription: faster-whisper with word timestamps (cached per job);
3. cutting: windows built from whole sentences, either the N "best" by a
   simple heuristic (speech pace, loudness, few pauses, a question/exclamation
   up front, ends on a full stop) or the whole video split into consecutive parts;
4. render: FFmpeg 1080×1920 (fill or blurred-background fit) with word-by-word
   captions burned in through libass.

Jobs live in ``.backlot/quickclips/<id>/quick.json`` (same pattern as rankings).
One job renders at a time; a server restart interrupts a running job.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import uuid
import wave
from pathlib import Path
from typing import Any, Callable, Optional

from backlot.state import REPO_ROOT

QUICK_DIR = REPO_ROOT / ".backlot" / "quickclips"

W, H, FPS = 1080, 1920, 30
MAX_CLIPS_ALL = 100
WHISPER_MODEL = "small"

LENGTHS = {
    "short": {"label": "Short · 15–30 s", "min": 15, "max": 30},
    "medium": {"label": "Medium · 30–60 s", "min": 30, "max": 60},
    "long": {"label": "Long · 60–90 s", "min": 60, "max": 90},
}
COUNTS = [3, 5, 8, 10]
FRAMINGS = {
    "auto": "Automatic (fill for vertical videos, blurred background for wide ones)",
    "fill": "Fill the screen (crops the sides — best for one person talking)",
    "fit": "Whole frame on a blurred background (nothing is cut off)",
}
CAPTIONS = {
    "highlight": "Word-by-word highlight",
    "clean": "Clean phrases",
    "none": "No captions",
}
ACCENTS = ["#FFD400", "#5BC0DE", "#FF2D55", "#7CFC9A", "#FFFFFF"]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_run_lock = threading.Lock()                 # one local job at a time
_cancel: dict[str, threading.Event] = {}
_procs: dict[str, subprocess.Popen] = {}
_FONT = "Arial Black"


class Cancelled(Exception):
    pass


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _dir(qid: str) -> Path:
    if not isinstance(qid, str) or not _ID_RE.match(qid):
        raise KeyError(qid)
    d = QUICK_DIR / qid
    if not (d / "quick.json").is_file():
        raise KeyError(qid)
    return d


def _read(qid: str) -> dict[str, Any]:
    return json.loads((_dir(qid) / "quick.json").read_text(encoding="utf-8"))


def _write(qid: str, doc: dict[str, Any]) -> None:
    d = QUICK_DIR / qid
    d.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = time.time()
    tmp = d / f"quick.json.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / "quick.json")


def _update(qid: str, **fields: Any) -> dict[str, Any]:
    doc = _read(qid)
    doc.update(fields)
    _write(qid, doc)
    return doc


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "clips"


def options() -> dict[str, Any]:
    return {
        "lengths": [{"value": k, "label": v["label"]} for k, v in LENGTHS.items()],
        "counts": COUNTS,
        "framings": [{"value": k, "label": v} for k, v in FRAMINGS.items()],
        "captions": [{"value": k, "label": v} for k, v in CAPTIONS.items()],
        "accents": ACCENTS,
        "max_all": MAX_CLIPS_ALL,
    }


def _public(doc: dict[str, Any]) -> dict[str, Any]:
    qid = doc["id"]
    out = dict(doc)
    clips = []
    for c in doc.get("clips") or []:
        c = dict(c)
        c["video_url"] = f"/api/quickclips/{qid}/clip/{c['id']}?v={int(c.get('rendered_at') or 0)}"
        c["download_url"] = f"/api/quickclips/{qid}/clip/{c['id']}?download=1"
        c["thumb_url"] = f"/api/quickclips/{qid}/thumb/{c['id']}?v={int(c.get('rendered_at') or 0)}"
        clips.append(c)
    out["clips"] = clips
    return out


def get(qid: str) -> dict[str, Any]:
    doc = _read(qid)
    # a worker thread does not survive a server restart
    if doc.get("status") == "running" and qid not in _cancel:
        doc["status"] = "error"
        doc["error"] = "The OwlCut server restarted while this was running. Start it again."
        _write(qid, doc)
    return _public(doc)


def export(qid: str):
    """Mirror the rendered clips into ~/Documents/OwlCutVideos/Clips/<title>_<date-time>/."""
    from backlot import exports
    try:
        d = _dir(qid)
        doc = _read(qid)
    except (KeyError, OSError, ValueError):
        return None
    files = []
    for i, c in enumerate(doc.get("clips") or [], 1):
        label = f"{i:02d} - {c.get('title') or c['id']}"
        files.append((d / "clips" / f"{c['id']}.mp4", f"{label}.mp4"))
    return exports.publish("clips", f"quick:{qid}", doc.get("title") or qid, doc.get("created_at"), files)


def list_all() -> list[dict[str, Any]]:
    out = []
    if not QUICK_DIR.is_dir():
        return out
    for p in QUICK_DIR.glob("*/quick.json"):
        try:
            doc = get(p.parent.name)
        except (KeyError, OSError, json.JSONDecodeError):
            continue
        out.append({k: doc.get(k) for k in ("id", "title", "status", "progress", "error", "created_at", "updated_at")}
                   | {"clip_count": len(doc.get("clips") or [])})
    out.sort(key=lambda d: d.get("updated_at") or 0, reverse=True)
    return out


def delete(qid: str, include_exports: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Delete a quick clips job (clips, the copy of the video, transcript). ``dry_run`` only reports."""
    from backlot import exports
    from backlot.jobs import _du
    d = _dir(qid)
    if qid in _cancel:
        raise RuntimeError("Cancel it first — it's still running.")
    export = exports.existing_folder("clips", f"quick:{qid}")
    report = {"id": qid, "bytes": _du(d, export) if include_exports else exports.dir_size(d),
              "export": str(export) if export else None, "with_exports_bytes": _du(d, export)}
    if dry_run:
        return report
    shutil.rmtree(d, ignore_errors=True)
    exports.forget("clips", f"quick:{qid}", delete_files=include_exports)
    return report


def clip_path(qid: str, cid: str, kind: str = "video") -> Path:
    d = _dir(qid)
    if not re.fullmatch(r"q\d{2,3}", cid or ""):
        raise KeyError(cid)
    p = d / "clips" / (f"{cid}.mp4" if kind == "video" else f"{cid}.jpg")
    if not p.is_file():
        raise KeyError(cid)
    return p


# ---------------------------------------------------------------------------
# Create / cancel
# ---------------------------------------------------------------------------

def _clean_form(form: dict[str, Any]) -> dict[str, Any]:
    length = form.get("length") if form.get("length") in LENGTHS else "medium"
    mode = "all" if form.get("mode") == "all" else "best"
    try:
        count = int(form.get("count") or 5)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(20, count))
    framing = form.get("framing") if form.get("framing") in FRAMINGS else "auto"
    captions = form.get("captions") if form.get("captions") in CAPTIONS else "highlight"
    accent = str(form.get("accent") or ACCENTS[0])
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = ACCENTS[0]
    from backlot.clipper import parse_ts
    f_start = parse_ts(str(form.get("focus_start") or "")) if form.get("focus_start") else None
    f_end = parse_ts(str(form.get("focus_end") or "")) if form.get("focus_end") else None
    if f_start is not None and f_end is not None and f_end <= f_start:
        f_end = None
    return {"length": length, "mode": mode, "count": count, "framing": framing,
            "captions": captions, "accent": accent.upper(), "focus_start": f_start, "focus_end": f_end}


def create(form: dict[str, Any]) -> dict[str, Any]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg was not found on this machine.")
    if _run_lock.locked():
        raise RuntimeError("Another local clips job is running. Wait for it to finish or cancel it.")
    opts = _clean_form(form)
    source: dict[str, Any]
    if form.get("upload_id"):
        from backlot import uploads
        try:
            info = uploads.ready_info(str(form["upload_id"]))
        except KeyError:
            raise ValueError("The uploaded video was not found; upload it again.")
        source = {"type": "upload", "upload_id": info["upload_id"], "name": info["name"],
                  "title": info.get("title") or Path(info["name"]).stem, "duration": info.get("duration")}
    else:
        from backlot.clipper import parse_link
        link = parse_link(str(form.get("url") or ""))   # ValueError with a user-facing message
        source = {"type": "youtube", "url": link["url"], "video_id": link["video_id"],
                  "title": str(form.get("source_title") or "").strip()[:120] or link["video_id"]}

    title = (str(form.get("title") or "").strip() or f"Quick clips — {source['title']}")[:120]
    qid = f"{_slug(source['title'])}-{uuid.uuid4().hex[:6]}"
    d = QUICK_DIR / qid
    d.mkdir(parents=True)
    if source["type"] == "upload":
        from backlot import uploads
        rel = uploads.claim_to(source["upload_id"], d, "source")
        source["path"] = rel
    doc = {
        "id": qid, "title": title, "source": source, "options": opts,
        "status": "running", "error": "", "clips": [],
        "progress": {"step": "source", "done": 0, "total": 0, "message": "Starting…"},
        "created_at": time.time(),
    }
    _write(qid, doc)
    _cancel[qid] = threading.Event()
    threading.Thread(target=_run, args=(qid,), daemon=True, name=f"quickclips-{qid}").start()
    return _public(doc)


def cancel(qid: str) -> dict[str, Any]:
    _dir(qid)
    ev = _cancel.get(qid)
    if ev:
        ev.set()
        proc = _procs.get(qid)
        if proc and proc.poll() is None:
            proc.terminate()
    else:
        doc = _read(qid)
        if doc.get("status") == "running":
            _update(qid, status="cancelled", error="")
    return get(qid)


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def _progress(qid: str, step: str, done: int, total: int, message: str) -> None:
    _update(qid, progress={"step": step, "done": done, "total": total, "message": message})


def _check(qid: str) -> None:
    ev = _cancel.get(qid)
    if ev and ev.is_set():
        raise Cancelled()


def _sh(qid: str, cmd: list[str], *, cwd: Optional[Path] = None, timeout: int = 3600) -> subprocess.CompletedProcess:
    _check(qid)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _procs[qid] = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError(f"{Path(cmd[0]).name} took too long.")
    finally:
        _procs.pop(qid, None)
    _check(qid)
    if proc.returncode != 0:
        raise RuntimeError(f"{Path(cmd[0]).name} failed: {err.strip()[-400:]}")
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _run(qid: str) -> None:
    with _run_lock:
        try:
            _pipeline(qid)
        except Cancelled:
            _update(qid, status="cancelled", error="",
                    progress={"step": "cancelled", "done": 0, "total": 0, "message": "Cancelled."})
        except Exception as exc:  # surface any failure on the page
            _update(qid, status="error", error=str(exc)[:600])
        finally:
            _cancel.pop(qid, None)
            _procs.pop(qid, None)


def _pipeline(qid: str) -> None:
    d = _dir(qid)
    doc = _read(qid)
    opts = doc["options"]
    src_info = doc["source"]

    # 1 — source
    if src_info["type"] == "youtube":
        _progress(qid, "source", 0, 0, "Downloading the video from YouTube…")
        src = _download(qid, src_info["url"], d)
        src_info["path"] = src.name
        _update(qid, source=src_info)
    src = d / src_info["path"]
    media = _probe(src)
    src_info.update({"duration": media["duration"], "width": media["w"], "height": media["h"], "has_audio": media["audio"]})
    _update(qid, source=src_info)

    # 2 — transcript
    words: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    rms: list[float] = []
    if media["audio"]:
        wav = d / "audio.wav"
        if not wav.is_file():
            _progress(qid, "audio", 0, 0, "Extracting the audio…")
            _sh(qid, ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                      "-c:a", "pcm_s16le", str(wav)])
        rms = _rms_per_second(wav)
        segments, words = _transcribe(qid, wav, media["duration"], d / "transcript.json")

    # 3 — windows
    _progress(qid, "pick", 0, 0, "Picking the clips…")
    windows = plan_clips(segments, words, rms, media["duration"], opts)
    if not windows:
        raise RuntimeError("Couldn't find any clip in this video (no speech and too short?).")

    # 4 — render
    out_dir = d / "clips"
    out_dir.mkdir(exist_ok=True)
    clips = []
    for i, w in enumerate(windows, 1):
        cid = f"q{i:02d}"
        _progress(qid, "render", i - 1, len(windows), f"Rendering clip {i} of {len(windows)}…")
        _render_clip(qid, src, media, w, words, opts, out_dir, cid)
        clip = {"id": cid, "start": round(w["start"], 2), "end": round(w["end"], 2),
                "duration": round(w["end"] - w["start"], 2), "title": w["title"], "transcript": w["text"],
                "score": w.get("score"), "rendered_at": time.time(),
                "size": (out_dir / f"{cid}.mp4").stat().st_size}
        clips.append(clip)
        _update(qid, clips=clips)
        export(qid)
    _update(qid, status="done", error="",
            progress={"step": "done", "done": len(clips), "total": len(clips), "message": "All clips are ready."})


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------

def _download(qid: str, url: str, dest: Path) -> Path:
    import yt_dlp

    def hook(st: dict[str, Any]) -> None:
        _check(qid)
        if st.get("status") == "downloading":
            done, total = st.get("downloaded_bytes") or 0, st.get("total_bytes") or st.get("total_bytes_estimate") or 0
            if total:
                pct = int(done * 100 / total)
                _progress(qid, "source", pct, 100, f"Downloading the video from YouTube… {pct}%")

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest / "source.%(ext)s"),
        "progress_hooks": [hook],
        "overwrites": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Cancelled:
        raise
    except Exception as exc:
        raise RuntimeError(f"Download failed: {str(exc)[:300]}") from exc
    for f in dest.glob("source.*"):
        if f.suffix in (".mp4", ".mkv", ".webm") and f.stat().st_size > 0:
            return f
    raise RuntimeError("The download produced no video file.")


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:stream_side_data=rotation:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    w, h = int(v.get("width") or 1920), int(v.get("height") or 1080)
    for sd in v.get("side_data_list", []) or []:
        if abs(int(sd.get("rotation", 0) or 0)) in (90, 270):
            w, h = h, w
    return {"w": w, "h": h, "audio": any(s.get("codec_type") == "audio" for s in streams),
            "duration": float((info.get("format") or {}).get("duration") or 0)}


def _rms_per_second(wav: Path) -> list[float]:
    """Loudness (dBFS) of each second of a 16 kHz mono PCM file."""
    import numpy as np
    with wave.open(str(wav), "rb") as f:
        rate = f.getframerate()
        data = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    n = len(data) // rate
    if n == 0:
        return []
    blocks = data[: n * rate].reshape(n, rate)
    rms = np.sqrt(np.mean(blocks ** 2, axis=1)) + 1e-9
    return [float(x) for x in 20 * np.log10(rms)]


def _transcribe(qid: str, wav: Path, duration: float, cache: Path) -> tuple[list, list]:
    if cache.is_file():
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data["segments"], data["words"]
    _progress(qid, "transcribe", 0, 100, "Loading the speech model…")
    from faster_whisper import WhisperModel
    model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    seg_iter, info = model.transcribe(str(wav), word_timestamps=True, vad_filter=True, beam_size=1)
    segments, words = [], []
    for seg in seg_iter:
        _check(qid)
        text = (seg.text or "").strip()
        if not text:
            continue
        segments.append({"start": float(seg.start), "end": float(seg.end), "text": text})
        for wd in seg.words or []:
            t = (wd.word or "").strip()
            if t:
                words.append({"start": float(wd.start), "end": float(wd.end), "w": t})
        pct = min(99, int(seg.end * 100 / duration)) if duration else 0
        _progress(qid, "transcribe", pct, 100, f"Transcribing the speech… {pct}%")
    cache.write_text(json.dumps({"language": info.language, "segments": segments, "words": words},
                                ensure_ascii=False), encoding="utf-8")
    return segments, words


# ---------------------------------------------------------------------------
# Picking the clips (pure functions — unit tested)
# ---------------------------------------------------------------------------

_TERMINAL = re.compile(r"[.!?…]['\")\]]*$")


def _window_text(segments: list[dict[str, Any]], a: int, b: int) -> str:
    return " ".join(s["text"] for s in segments[a:b + 1]).strip()


def _title_from(text: str) -> str:
    first = re.split(r"(?<=[.!?…])\s+", text.strip(), maxsplit=1)[0]
    first = first.strip(" .,…")
    return (first[:57].rsplit(" ", 1)[0] + "…") if len(first) > 60 else (first or "Clip")


def _windows_from(segments: list[dict[str, Any]], i: int, lo: float, hi: float, gap_break: float = 3.0) -> Optional[tuple[int, int]]:
    """Longest-sensible window starting at segment i: at least `lo` seconds,
    at most `hi`, preferring to end on a full sentence. None if impossible."""
    start = segments[i]["start"]
    best_end, best_any = None, None
    for j in range(i, len(segments)):
        if j > i and segments[j]["start"] - segments[j - 1]["end"] > gap_break:
            break
        dur = segments[j]["end"] - start
        if dur > hi:
            break
        if dur >= lo:
            best_any = j
            if _TERMINAL.search(segments[j]["text"]):
                best_end = j
                # stop at the first clean ending after ~70% of the range
                if dur >= lo + 0.7 * (hi - lo):
                    break
    if best_end is not None:
        return i, best_end
    if best_any is not None:
        return i, best_any
    return None


def _score(segments: list[dict[str, Any]], words: list[dict[str, Any]], rms: list[float],
           a: int, b: int, lo_db: float, hi_db: float) -> int:
    s, e = segments[a]["start"], segments[b]["end"]
    dur = max(0.1, e - s)
    ws = [w for w in words if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
    wps = len(ws) / dur
    pace = min(1.0, max(0.0, (wps - 1.2) / (3.4 - 1.2)))
    secs = rms[int(s): max(int(s) + 1, int(math.ceil(e)))] if rms else []
    if secs and hi_db > lo_db:
        energy = min(1.0, max(0.0, (sum(secs) / len(secs) - lo_db) / (hi_db - lo_db)))
    else:
        energy = 0.5
    gaps = sum(max(0.0, ws[k + 1]["start"] - ws[k]["end"] - 0.35) for k in range(len(ws) - 1)) if ws else dur
    fluency = 1.0 - min(1.0, gaps / dur * 2.5)
    head = segments[a]["text"]
    hook = 1.0 if re.search(r"[?!]", head) else 0.0
    clean_end = 1.0 if _TERMINAL.search(segments[b]["text"]) else 0.0
    score = 34 * pace + 28 * energy + 18 * fluency + 10 * hook + 10 * clean_end
    return int(round(min(100.0, max(0.0, score))))


def plan_clips(segments: list[dict[str, Any]], words: list[dict[str, Any]], rms: list[float],
               duration: float, opts: dict[str, Any]) -> list[dict[str, Any]]:
    lo, hi = LENGTHS[opts["length"]]["min"], LENGTHS[opts["length"]]["max"]
    f_start, f_end = opts.get("focus_start"), opts.get("focus_end")
    if f_start is not None or f_end is not None:   # only the range the user marked
        a = f_start or 0.0
        b = f_end if f_end is not None else float("inf")
        segments = [s for s in segments if s["start"] >= a - 0.5 and s["end"] <= b + 0.5]
    if not segments:
        return _plan_without_speech(duration, lo, hi, opts)
    sorted_db = sorted(rms) if rms else []
    lo_db = sorted_db[int(len(sorted_db) * 0.15)] if sorted_db else 0.0
    hi_db = sorted_db[int(len(sorted_db) * 0.95) - 1] if sorted_db else 0.0

    def make(a: int, b: int, score: Optional[int] = None) -> dict[str, Any]:
        s = max(0.0, segments[a]["start"] - 0.15)
        e = min(duration or segments[b]["end"] + 0.3, segments[b]["end"] + 0.35)
        text = _window_text(segments, a, b)
        return {"start": s, "end": e, "text": text, "title": _title_from(text),
                "score": score if score is not None else _score(segments, words, rms, a, b, lo_db, hi_db)}

    if opts["mode"] == "all":
        out, i = [], 0
        while i < len(segments) and len(out) < MAX_CLIPS_ALL:
            win = _windows_from(segments, i, lo, hi)
            if win is None:
                # too little speech before a long pause: glue it onto the next block if possible
                j = i
                while j + 1 < len(segments) and segments[j + 1]["end"] - segments[i]["start"] <= hi \
                        and segments[j + 1]["start"] - segments[j]["end"] <= 3.0:
                    j += 1
                if segments[j]["end"] - segments[i]["start"] >= lo * 0.5:
                    out.append(make(i, j))
                i = j + 1
                continue
            out.append(make(*win))
            i = win[1] + 1
        return out

    # best N: score every start, keep the top non-overlapping windows
    cands = []
    for i in range(len(segments)):
        win = _windows_from(segments, i, lo, hi)
        if win:
            a, b = win
            cands.append((_score(segments, words, rms, a, b, lo_db, hi_db), a, b))
    cands.sort(key=lambda t: (-t[0], segments[t[1]]["start"]))
    chosen: list[tuple[int, int, int]] = []
    for sc, a, b in cands:
        s, e = segments[a]["start"], segments[b]["end"]
        if all(e <= segments[ca]["start"] or s >= segments[cb]["end"] for _, ca, cb in chosen):
            chosen.append((sc, a, b))
        if len(chosen) >= opts["count"]:
            break
    return [make(a, b, sc) for sc, a, b in chosen]


def _plan_without_speech(duration: float, lo: float, hi: float, opts: dict[str, Any]) -> list[dict[str, Any]]:
    if duration <= 0:
        return []
    size = min(hi, max(lo, (lo + hi) / 2))
    if duration < lo:
        size = duration
    n_all = max(1, int(duration // size))
    starts = [k * size for k in range(min(n_all, MAX_CLIPS_ALL))]
    if opts["mode"] != "all" and len(starts) > opts["count"]:
        step = len(starts) / opts["count"]
        starts = [starts[int(k * step)] for k in range(opts["count"])]
    return [{"start": s, "end": min(duration, s + size), "text": "", "title": f"Part {k + 1}", "score": None}
            for k, s in enumerate(starts)]


# ---------------------------------------------------------------------------
# Captions (ASS) + render
# ---------------------------------------------------------------------------

def _ass_color(hex_color: str, alpha: str = "00") -> str:
    h = hex_color.lstrip("#")
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def caption_chunks(words: list[dict[str, Any]], max_words: int = 3, max_chars: int = 18) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for w in words:
        if cur:
            text_len = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            gap = w["start"] - cur[-1]["end"]
            if len(cur) >= max_words or text_len > max_chars or gap > 0.6 or re.search(r"[.!?,;:…]$", cur[-1]["w"]):
                chunks.append(cur)
                cur = []
        cur.append(w)
    if cur:
        chunks.append(cur)
    return chunks


def build_ass(words: list[dict[str, Any]], style: str, accent: str, clip_dur: float) -> str:
    """ASS subtitle file for one clip; `words` times are relative to the clip start."""
    upper = style == "highlight"
    size = 88 if style == "highlight" else 70
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{_FONT},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{7 if upper else 5},{2 if upper else 3},2,70,70,620,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    accent_c = _ass_color(accent)
    chunks = caption_chunks(words)
    for ci, ch in enumerate(chunks):
        c_start = ch[0]["start"]
        nxt = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else clip_dur
        c_end = min(nxt, ch[-1]["end"] + 0.35)
        if c_end <= c_start:
            continue
        toks = [_ass_escape(w["w"].upper() if upper else w["w"]) for w in ch]
        if style == "highlight":
            for wi, w in enumerate(ch):
                s = c_start if wi == 0 else w["start"]
                e = ch[wi + 1]["start"] if wi + 1 < len(ch) else c_end
                if e <= s:
                    continue
                parts = [f"{{\\c{accent_c}}}{t}{{\\c&H00FFFFFF&}}" if k == wi else t for k, t in enumerate(toks)]
                lines.append(f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Cap,,0,0,0,,{' '.join(parts)}")
        else:
            lines.append(f"Dialogue: 0,{_ass_time(c_start)},{_ass_time(c_end)},Cap,,0,0,0,,{' '.join(toks)}")
    return header + "\n".join(lines) + "\n"


def fit_box(src_w: int, src_h: int) -> tuple[int, int, int, int]:
    """(x, y, w, h) of the footage in the 'blurred background' layout (picture a little above centre)."""
    fh = min(H, int(round(W * src_h / max(1, src_w) / 2)) * 2)
    fw = min(W, int(round(fh * src_w / max(1, src_h) / 2)) * 2)
    return (W - fw) // 2, (H - fh) // 2 - 120, fw, fh


def resolve_framing(framing: str, src_w: int, src_h: int) -> str:
    return ("fill" if src_w / max(1, src_h) < 0.9 else "fit") if framing == "auto" else framing


def _render_clip(qid: str, src: Path, media: dict[str, Any], win: dict[str, Any], words: list[dict[str, Any]],
                 opts: dict[str, Any], out_dir: Path, cid: str) -> None:
    s, e = win["start"], win["end"]
    dur = max(0.5, e - s)
    framing = resolve_framing(opts["framing"], media["w"], media["h"])
    box = None                                           # where the real picture sits (None = whole frame)
    if framing == "fill":
        chains = [f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[base]"]
    else:
        box = fit_box(media["w"], media["h"])
        bx, by, fw, fh = box
        chains = [
            f"[0:v]split[a][b]",
            f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,"
            f"eq=brightness=-0.18:saturation=0.85,setsar=1,fps={FPS}[bg]",
            f"[b]scale={fw}:{fh},setsar=1,fps={FPS}[fg]",
            f"[bg][fg]overlay=x={bx}:y={by}[base]",
        ]
    from backlot import branding
    # on the picture itself, never on the blurred band — so cropping the video can't remove it
    mark = branding.badge_for("clips", W, H, out_dir / f".{cid}_brand", box=box)
    fade = branding.fade_for("clips", dur)               # from black at the start, to black at the end
    vout = "[vf]" if fade else "[v]"
    pre = "[pre]" if mark else vout
    captioned = False
    if opts["captions"] != "none" and words:
        rel = [{"start": w["start"] - s, "end": w["end"] - s, "w": w["w"]}
               for w in words if w["start"] >= s - 0.05 and w["end"] <= e + 0.05]
        if rel:
            (out_dir / f"{cid}.ass").write_text(build_ass(rel, opts["captions"], opts["accent"], dur), encoding="utf-8")
            chains.append(f"[base]ass={cid}.ass,format=yuv420p{pre}")
            captioned = True
    if not captioned:
        chains.append(f"[base]format=yuv420p{pre}")
    if media["audio"]:
        inputs = ["-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(src)]
        afade = branding.fade_filters(fade, dur)[1] if fade else \
            f"afade=t=in:st=0:d=0.08,afade=t=out:st={max(0.0, dur - 0.2):.2f}:d=0.2"
        chains.append(f"[0:a]aresample=48000,{afade}[aout]")
    else:
        inputs = ["-ss", f"{s:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                  "-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        chains.append("[1:a]anull[aout]")
    if mark:
        png, mx, my = mark
        idx = inputs.count("-i")
        inputs += ["-i", str(png)]
        chains.append(f"[pre][{idx}:v]overlay={mx}:{my}:format=auto,format=yuv420p{vout}")
    if fade:   # last, so captions and watermark fade with the picture
        chains.append(f"[vf]{branding.fade_filters(fade, dur)[0]},format=yuv420p[v]")
    script = out_dir / f"{cid}.filter"
    script.write_text(";\n".join(chains), encoding="utf-8")
    out = out_dir / f"{cid}.mp4"
    _sh(qid, ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-/filter_complex", script.name,
              "-map", "[v]", "-map", "[aout]", "-t", f"{dur:.3f}",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
              "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart", out.name],
        cwd=out_dir, timeout=1800)
    _sh(qid, ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(max(1.0, fade + 0.4), dur / 2):.2f}", "-i", out.name,
              "-frames:v", "1", "-vf", "scale=360:-2", f"{cid}.jpg"], cwd=out_dir, timeout=120)
    if mark:
        shutil.rmtree(mark[0].parent, ignore_errors=True)


def bundle_zip(qid: str) -> Path:
    """All clips in one .zip on disk (rebuilt when clips changed). Videos are
    already compressed, so they are stored, not deflated."""
    import zipfile
    doc = _read(qid)
    d = _dir(qid)
    clips = [c for c in doc.get("clips") or [] if (d / "clips" / f"{c['id']}.mp4").is_file()]
    if not clips:
        raise KeyError("no clips")
    out = d / "clips.zip"
    newest = max((d / "clips" / f"{c['id']}.mp4").stat().st_mtime for c in clips)
    if out.is_file() and out.stat().st_mtime >= newest:
        return out
    tmp = d / f"clips.zip.{uuid.uuid4().hex[:6]}.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for c in clips:
            z.write(d / "clips" / f"{c['id']}.mp4", f"{c['id']} - {_slug(c.get('title') or c['id'])}.mp4")
    tmp.replace(out)
    return out
