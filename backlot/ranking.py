"""Ranking videos — pick up to 10 YouTube moments, trim them, render one 9:16 video.

Everything here runs locally (yt-dlp + ffmpeg); no agent is involved, so a
ranking costs no Claude usage.

Storage: ``.backlot/rankings/<id>/``
    ranking.json        the document the editor saves (title, entries, status)
    sources/            downloaded, frame-accurate sections (cached by cut)
    work/               per-entry renders + concat list
    ranking.mp4         the final video
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Optional

from backlot.clipper import parse_link
from backlot.state import REPO_ROOT

RANKINGS_DIR = REPO_ROOT / ".backlot" / "rankings"

W, H, FPS = 1080, 1920, 30
MAX_ENTRIES = 10
MIN_ENTRIES = 2
MAX_CUT_SECONDS = 90.0
MIN_CUT_SECONDS = 1.0
MAX_TITLE = 70
MAX_NAME = 48

ACCENTS = ["#5BC0DE", "#D97706", "#F4D6B0", "#FFD400", "#FF2D55", "#FFFFFF"]
ORDERS = {
    "countdown": "Countdown (#6 → #1)",
    "top": "Top first (#1 → #6)",
}

# Layout (1080×1920). Header band on top, video in the middle, rank badge
# below — all inside the TikTok/Reels/Shorts safe area.
HEADER_H = 360
VIDEO_Y = 400
VIDEO_MAX_H = 880

_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
]

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_render_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _dir(rid: str) -> Path:
    if not _ID_RE.match(rid or ""):
        raise KeyError(rid)
    d = RANKINGS_DIR / rid
    if not (d / "ranking.json").is_file():
        raise KeyError(rid)
    return d


def _slug(text: str) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "ranking"


def _write(rid: str, doc: dict[str, Any]) -> None:
    d = RANKINGS_DIR / rid
    d.mkdir(parents=True, exist_ok=True)
    doc["updated_at"] = time.time()
    tmp = d / f"ranking.json.{uuid.uuid4().hex[:6]}.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(d / "ranking.json")


def _empty_entry() -> dict[str, Any]:
    return {"url": "", "video_id": "", "name": "", "video_title": "", "channel": "",
            "thumbnail": "", "duration": 0, "start": None, "end": None}


def options() -> dict[str, Any]:
    return {
        "accents": ACCENTS,
        "orders": [{"value": k, "label": v} for k, v in ORDERS.items()],
        "max_entries": MAX_ENTRIES,
        "min_entries": MIN_ENTRIES,
        "max_cut": MAX_CUT_SECONDS,
        "ffmpeg": bool(_ffmpeg()),
    }


def create(title: str = "") -> dict[str, Any]:
    title = (title or "").strip()[:MAX_TITLE] or "Top 6"
    rid = f"{_slug(title)}-{uuid.uuid4().hex[:6]}"
    doc = {
        "id": rid,
        "title": title,
        "accent": ACCENTS[0],
        "order": "countdown",
        "entries": [_empty_entry() for _ in range(6)],
        "status": "draft",
        "progress": None,
        "error": "",
        "output": None,
        "created_at": time.time(),
    }
    _write(rid, doc)
    return doc


def get(rid: str) -> dict[str, Any]:
    d = _dir(rid)
    doc = json.loads((d / "ranking.json").read_text(encoding="utf-8"))
    out = d / "ranking.mp4"
    doc["output"] = (
        {"url": f"/api/rankings/{rid}/video?v={int(out.stat().st_mtime)}",
         "size": out.stat().st_size}
        if out.is_file() else None
    )
    # A render thread does not survive a server restart.
    if doc.get("status") == "rendering" and not _render_lock.locked():
        doc["status"], doc["error"] = "error", "Render was interrupted. Start it again."
    return doc


def list_all() -> list[dict[str, Any]]:
    if not RANKINGS_DIR.is_dir():
        return []
    rows = []
    for d in RANKINGS_DIR.iterdir():
        try:
            doc = get(d.name)
        except (KeyError, ValueError, OSError):
            continue
        filled = [e for e in doc.get("entries", []) if e.get("video_id")]
        rows.append({
            "id": doc["id"], "title": doc.get("title", ""), "status": doc.get("status"),
            "entries": len(filled), "thumbnail": filled[0]["thumbnail"] if filled else "",
            "has_video": bool(doc.get("output")), "updated_at": doc.get("updated_at", 0),
        })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows


def delete(rid: str) -> None:
    d = _dir(rid)
    if get(rid).get("status") == "rendering":
        raise RuntimeError("This ranking is rendering.")
    shutil.rmtree(d)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, 2) if f == f and f >= 0 else None


def _clean_entry(raw: Any) -> dict[str, Any]:
    e = _empty_entry()
    if not isinstance(raw, dict):
        return e
    url = str(raw.get("url") or "").strip()
    if url:
        try:
            link = parse_link(url)
            e["url"], e["video_id"] = link["url"], link["video_id"]
        except ValueError:
            e["url"] = url[:300]
    e["name"] = str(raw.get("name") or "").strip()[:MAX_NAME]
    e["video_title"] = str(raw.get("video_title") or "")[:200]
    e["channel"] = str(raw.get("channel") or "")[:120]
    thumb = str(raw.get("thumbnail") or "")
    e["thumbnail"] = thumb if thumb.startswith("https://") else ""
    e["duration"] = _num(raw.get("duration")) or 0
    e["start"], e["end"] = _num(raw.get("start")), _num(raw.get("end"))
    return e


def save(rid: str, body: dict[str, Any]) -> dict[str, Any]:
    doc = get(rid)
    if doc.get("status") == "rendering":
        raise RuntimeError("Wait for the render to finish before editing.")
    if "title" in body:
        doc["title"] = str(body.get("title") or "").strip()[:MAX_TITLE]
    if body.get("accent") in ACCENTS:
        doc["accent"] = body["accent"]
    if body.get("order") in ORDERS:
        doc["order"] = body["order"]
    if isinstance(body.get("entries"), list):
        entries = [_clean_entry(e) for e in body["entries"][:MAX_ENTRIES]]
        doc["entries"] = entries or [_empty_entry()]
    if doc.get("status") == "error":
        doc["status"], doc["error"] = "draft", ""
    doc.pop("output", None)
    _write(rid, doc)
    return get(rid)


def problems(doc: dict[str, Any]) -> list[str]:
    """What blocks a render, in plain words (empty list = ready)."""
    out = []
    if not (doc.get("title") or "").strip():
        out.append("Add a header title.")
    filled = [(i, e) for i, e in enumerate(doc.get("entries", [])) if e.get("video_id")]
    if len(filled) < MIN_ENTRIES:
        out.append(f"Add at least {MIN_ENTRIES} videos.")
    for i, e in filled:
        s, t = e.get("start"), e.get("end")
        label = f"Slot {i + 1}"
        if s is None or t is None:
            out.append(f"{label}: set the start and end of the cut.")
            continue
        if t - s < MIN_CUT_SECONDS:
            out.append(f"{label}: the cut must be at least {MIN_CUT_SECONDS:.0f}s.")
        elif t - s > MAX_CUT_SECONDS:
            out.append(f"{label}: the cut can be at most {MAX_CUT_SECONDS:.0f}s.")
        if e.get("duration") and t > e["duration"] + 0.5:
            out.append(f"{label}: the end is past the end of the video.")
    for i, e in enumerate(doc.get("entries", [])):
        if e.get("url") and not e.get("video_id"):
            out.append(f"Slot {i + 1}: this is not a YouTube video link.")
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _ffmpeg() -> Optional[str]:
    return shutil.which("ffmpeg")


def _font() -> str:
    for f in _FONT_CANDIDATES:
        if os.path.isfile(f):
            return f
    try:  # matplotlib ships DejaVu in the venv
        import matplotlib
        f = Path(matplotlib.get_data_path()) / "fonts" / "ttf" / "DejaVuSans-Bold.ttf"
        if f.is_file():
            return str(f)
    except Exception:
        pass
    raise RuntimeError("No bold TrueType font found for the text overlays.")


def _progress(rid: str, step: str, done: int, total: int, message: str) -> None:
    doc = json.loads((RANKINGS_DIR / rid / "ranking.json").read_text(encoding="utf-8"))
    doc["progress"] = {"step": step, "done": done, "total": total, "message": message}
    _write(rid, doc)


def _set_status(rid: str, status: str, error: str = "") -> None:
    doc = json.loads((RANKINGS_DIR / rid / "ranking.json").read_text(encoding="utf-8"))
    doc["status"], doc["error"] = status, error
    if status != "rendering":
        doc["progress"] = None
    _write(rid, doc)


def _download(entry: dict[str, Any], dest_dir: Path) -> Path:
    """Download exactly [start, end] of the video (frame-accurate)."""
    start, end = float(entry["start"]), float(entry["end"])
    key = f"{entry['video_id']}_{int(start * 100)}_{int(end * 100)}"
    for f in dest_dir.glob(f"{key}.*"):
        if f.suffix in (".mp4", ".mkv", ".webm") and f.stat().st_size > 0:
            return f
    import yt_dlp
    from yt_dlp.utils import download_range_func

    dest_dir.mkdir(parents=True, exist_ok=True)
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / f"{key}.%(ext)s"),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "overwrites": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([entry["url"]])
    for f in dest_dir.glob(f"{key}.*"):
        if f.suffix in (".mp4", ".mkv", ".webm") and f.stat().st_size > 0:
            return f
    raise RuntimeError("The download produced no video file.")


def _probe_media(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:stream_side_data=rotation:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    info = json.loads(out.stdout or "{}")
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), {})
    w, h = int(v.get("width") or 1920), int(v.get("height") or 1080)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        rot = abs(int(sd.get("rotation", 0) or 0))
    if rot in (90, 270):
        w, h = h, w
    return {
        "w": w, "h": h,
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "duration": float((info.get("format") or {}).get("duration") or 0),
    }


def _wrap(text: str, width: int, max_lines: int) -> list[str]:
    lines = textwrap.wrap(text, width=width, break_long_words=True) or [""]
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip()[: max(1, width - 1)] + "…"
    return lines


def _fit_text(text: str, sizes: list[tuple[int, int]], max_lines: int) -> tuple[list[str], int]:
    """Pick the biggest font size whose wrap fits in max_lines. sizes = [(px, chars/line)]."""
    for px, chars in sizes:
        lines = textwrap.wrap(text, width=chars) or [""]
        if len(lines) <= max_lines:
            return lines, px
    px, chars = sizes[-1]
    return _wrap(text, chars, max_lines), px


def _esc(path: Path | str) -> str:
    """Escape a path for use inside a filtergraph option value."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _hex(color: str) -> str:
    return "0x" + color.lstrip("#").upper()


def _text_filters(lines: list[str], px: int, top: int, color: str, font: str, workdir: Path,
                  tag: str, fade: bool, box: bool = False) -> list[str]:
    """One drawtext per line, centred, starting at y=top. Text goes through files."""
    filters = []
    gap = int(px * 1.18)
    for n, line in enumerate(lines):
        tf = workdir / f"{tag}_{n}.txt"
        tf.write_text(line, encoding="utf-8")
        alpha = f":alpha='min(1,max(0,(t-{0.12 + n * 0.08:.2f})/0.35))'" if fade else ""
        boxopt = ":box=1:boxcolor=black@0.55:boxborderw=16" if box else ""
        filters.append(
            f"drawtext=fontfile='{_esc(font)}':textfile='{_esc(tf)}':expansion=none"
            f":fontsize={px}:fontcolor={color}:x=(w-text_w)/2:y={top + n * gap}"
            f":shadowcolor=black@0.6:shadowx=0:shadowy=4{boxopt}{alpha}"
        )
    return filters


def _render_entry(src: Path, out: Path, *, rank: int, name: str, title: str, accent: str,
                  font: str, workdir: Path, tag: str) -> None:
    m = _probe_media(src)
    dur = m["duration"]
    vertical = m["w"] / m["h"] < 0.9

    chains = []
    if vertical:
        # Fill the frame; text sits on translucent bands.
        chains.append(
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[base]"
        )
        badge_y = 1230
    else:
        fh = min(VIDEO_MAX_H, int(round(W * m["h"] / m["w"] / 2)) * 2)
        fw = min(W, int(round(fh * m["w"] / m["h"] / 2)) * 2)
        chains.append(
            f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
            f"gblur=sigma=38,eq=brightness=-0.22:saturation=0.8,setsar=1,fps={FPS}[bg]"
        )
        chains.append(f"[0:v]scale={fw}:{fh},setsar=1,fps={FPS}[fg]")
        chains.append(f"[bg][fg]overlay=x=(W-w)/2:y={VIDEO_Y}[base]")
        badge_y = VIDEO_Y + fh + 46

    title_lines, title_px = _fit_text(title, [(84, 19), (72, 23), (62, 27)], 3)
    title_block = int(title_px * 1.18) * len(title_lines)
    title_top = 70 + max(0, (HEADER_H - 90 - title_block) // 2)

    name_lines, name_px = _fit_text(name, [(64, 24), (54, 29), (46, 34)], 2) if name else ([], 0)
    rank_px = 150

    accent_c = _hex(accent)
    draw = [
        f"drawbox=x=0:y=0:w=iw:h={HEADER_H}:color=0x0B132B@{0.72 if vertical else 0.9}:t=fill",
        f"drawbox=x=(iw-220)/2:y={HEADER_H - 14}:w=220:h=8:color={accent_c}:t=fill",
    ]
    draw += _text_filters(title_lines, title_px, title_top, "white", font, workdir, f"{tag}_title", fade=False)
    draw += _text_filters([f"#{rank}"], rank_px, badge_y, accent_c, font, workdir, f"{tag}_rank", fade=True)
    if name_lines:
        draw += _text_filters(name_lines, name_px, badge_y + rank_px + 24, "white", font, workdir,
                              f"{tag}_name", fade=True, box=vertical)
    fade_out = max(0.0, dur - 0.2)
    draw += [f"fade=t=in:st=0:d=0.2", f"fade=t=out:st={fade_out:.2f}:d=0.2", "format=yuv420p"]
    from backlot import branding
    box = None if vertical else ((W - fw) // 2, VIDEO_Y, fw, fh)   # keep it on the picture, not the blur
    mark = branding.badge_for("ranking", W, H, workdir / f"{tag}_brand", top_reserved=HEADER_H, box=box)
    chains.append(f"[base]{','.join(draw)}{'[pre]' if mark else '[v]'}")

    if m["audio"]:
        a_src = "[0:a]"
        inputs = ["-i", str(src)]
    else:
        a_src = "[1:a]"
        inputs = ["-i", str(src), "-f", "lavfi", "-t", f"{dur:.2f}",
                  "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    chains.append(
        f"{a_src}aresample=48000,aformat=channel_layouts=stereo,"
        f"{'loudnorm=I=-15:TP=-1.5:LRA=11,' if m['audio'] else ''}afade=t=in:st=0:d=0.15,"
        f"afade=t=out:st={max(0.0, dur - 0.25):.2f}:d=0.25[a]"
    )

    if mark:   # after the per-entry fades, so the watermark stays steady across entries
        png, mx, my = mark
        idx = inputs.count("-i")
        inputs += ["-i", str(png)]
        chains.append(f"[pre][{idx}:v]overlay={mx}:{my}:format=auto,format=yuv420p[v]")

    script = workdir / f"{tag}.filter"
    script.write_text(";\n".join(chains), encoding="utf-8")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", *inputs,
        "-/filter_complex", str(script),
        "-map", "[v]", "-map", "[a]", "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", str(out),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg failed on #{rank}: {res.stderr.strip()[-400:]}")


def ordered_entries(doc: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """(rank, entry) in playback order. Slot 1 is always rank #1."""
    filled = [e for e in doc.get("entries", []) if e.get("video_id")]
    ranked = [(i + 1, e) for i, e in enumerate(filled)]
    return list(reversed(ranked)) if doc.get("order", "countdown") == "countdown" else ranked


def _render(rid: str) -> None:
    d = RANKINGS_DIR / rid
    try:
        doc = get(rid)
        font = _font()
        items = ordered_entries(doc)
        total = len(items) * 2 + 1
        work = d / "work"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)

        sources = []
        for n, (rank, e) in enumerate(items):
            _progress(rid, "download", n, total, f"Downloading #{rank} — {e.get('name') or e.get('video_title') or e['video_id']}")
            sources.append(_download(e, d / "sources"))

        parts = []
        for n, ((rank, e), src) in enumerate(zip(items, sources)):
            _progress(rid, "render", len(items) + n, total, f"Rendering #{rank}")
            part = work / f"part_{n:02d}.mp4"
            _render_entry(src, part, rank=rank, name=e.get("name") or "", title=doc["title"],
                          accent=doc.get("accent") or ACCENTS[0], font=font, workdir=work, tag=f"p{n:02d}")
            parts.append(part)

        _progress(rid, "join", total - 1, total, "Joining the clips")
        lst = work / "concat.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
        tmp_out = d / "ranking.tmp.mp4"
        res = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-c", "copy", "-movflags", "+faststart", str(tmp_out)],
            capture_output=True, text=True, timeout=600, cwd=work,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Joining failed: {res.stderr.strip()[-400:]}")
        tmp_out.replace(d / "ranking.mp4")
        _set_status(rid, "done")
        export(rid)
    except Exception as exc:  # surface any failure to the editor
        msg = str(exc)
        if "Sign in" in msg or "Private video" in msg:
            msg = "One of the videos is private or needs a login."
        _set_status(rid, "error", msg[:600])
    finally:
        _render_lock.release()


def start_render(rid: str) -> dict[str, Any]:
    doc = get(rid)
    issues = problems(doc)
    if issues:
        raise ValueError(" ".join(issues))
    if not _ffmpeg():
        raise RuntimeError("ffmpeg was not found on PATH.")
    if not _render_lock.acquire(blocking=False):
        raise RuntimeError("Another ranking is rendering. Wait for it to finish.")
    try:
        _set_status(rid, "rendering")
        _progress(rid, "start", 0, 1, "Starting")
        threading.Thread(target=_render, args=(rid,), daemon=True, name=f"ranking-{rid}").start()
    except Exception:
        _render_lock.release()
        raise
    return get(rid)


def export(rid: str):
    """Mirror the final video into ~/Documents/OwlCutVideos/RankingVideos/<title>_<date-time>/."""
    from backlot import exports
    try:
        d = _dir(rid)
        doc = get(rid)
    except (KeyError, OSError, ValueError):
        return None
    title = doc.get("title") or rid
    return exports.publish("ranking", f"ranking:{rid}", title, doc.get("created_at"),
                           [(d / "ranking.mp4", f"{title}.mp4")])


def video_path(rid: str) -> Path:
    p = _dir(rid) / "ranking.mp4"
    if not p.is_file():
        raise KeyError(rid)
    return p
