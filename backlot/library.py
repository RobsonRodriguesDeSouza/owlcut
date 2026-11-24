"""Everything saved in Documents/OwlCutVideos, ready to post.

Lists the productions OwlCut mirrored into the output folder (see ``exports``),
serves their videos and covers, and keeps, per video and per social network,
the post text (title, caption, hashtags) and whether it was posted.

Paths from the browser are always relative to the output root and are checked
to stay inside it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from backlot import exports
from backlot.clipper import LIMITS, PLATFORMS, _clean_tags

BACKLOT_DIR = Path(__file__).resolve().parents[1] / ".backlot"
POSTS_FILE = BACKLOT_DIR / "saved_posts.json"
THUMBS_DIR = BACKLOT_DIR / "thumbs" / "saved"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm"}
POST_PLATFORMS = ["tiktok", "reels", "shorts"]
KIT_DIR = "Publishing kit"
_lock = threading.Lock()
_STAMP = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{2})-(\d{2})(?: \(\d+\))?$")


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def resolve(rel: str) -> Path:
    """A video inside the output folder; ValueError for anything else."""
    rel = str(rel or "").strip()
    if not rel or rel.startswith(("/", "~")) or "\x00" in rel:
        raise ValueError("Invalid path.")
    base = exports.root().resolve()
    path = (base / rel).resolve()
    if base not in path.parents:
        raise ValueError("Invalid path.")
    if path.suffix.lower() not in VIDEO_EXT:
        raise ValueError("Not a video.")
    if not path.is_file():
        raise KeyError(rel)
    return path


def _rel(path: Path) -> str:
    return str(path.relative_to(exports.root()))


def _title_and_date(folder_name: str) -> tuple[str, Optional[str]]:
    m = _STAMP.search(folder_name)
    if not m:
        return folder_name, None
    title = folder_name[: m.start()]
    title = re.sub(r"^Quick clips\s*[—-]\s*", "", title)
    title = re.sub(r"_\d{14}$", "", title)             # quick clips keep the upload's timestamp in the name
    return title.strip() or folder_name, f"{m.group(1)} {m.group(2)}:{m.group(3)}"


def _clip_label(name: str) -> str:
    stem = Path(name).stem
    stem = re.sub(r"\s*\((?:9x16|4x5|16x9|1x1)\)$", "", stem)
    return re.sub(r"^\d{1,3}\s*-\s*", "", stem).strip() or stem


# ---------------------------------------------------------------------------
# post state
# ---------------------------------------------------------------------------

def _load_posts() -> dict[str, Any]:
    try:
        data = json.loads(POSTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_posts(data: dict[str, Any]) -> None:
    POSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = POSTS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(POSTS_FILE)


def post_state(rel: str) -> dict[str, Any]:
    path = resolve(rel)
    saved = _load_posts().get(rel) or {}
    label = _clip_label(path.name)
    out = {}
    for p in POST_PLATFORMS:
        e = saved.get(p) or {}
        out[p] = {
            "title": str(e.get("title", label))[: LIMITS[p].get("title", 200)],
            "caption": str(e.get("caption", label)),
            "hashtags": _clean_tags(e.get("hashtags", [])),
            "posted": bool(e.get("posted")),
            "posted_at": e.get("posted_at"),
            "sent_at": e.get("sent_at"),
        }
    return {"path": rel, "name": path.name, "label": label, "size": path.stat().st_size,
            "platforms": POST_PLATFORMS, "labels": {p: PLATFORMS[p]["label"] for p in POST_PLATFORMS},
            "limits": {p: LIMITS[p] for p in POST_PLATFORMS}, "posts": out}


def save_post(rel: str, platform: str, body: dict[str, Any]) -> dict[str, Any]:
    resolve(rel)
    if platform not in POST_PLATFORMS:
        raise ValueError("Unknown network.")
    lim = LIMITS[platform]
    entry: dict[str, Any] = {}
    if "title" in body and "title" in lim:
        title = str(body.get("title") or "").strip()
        if len(title) > lim["title"]:
            raise ValueError(f"The title is longer than {lim['title']} characters.")
        entry["title"] = title
    if "caption" in body:
        caption = str(body.get("caption") or "")
        if len(caption) > lim["caption"]:
            raise ValueError(f"The caption is longer than {lim['caption']} characters.")
        entry["caption"] = caption
    if "hashtags" in body:
        tags = _clean_tags(body.get("hashtags"))
        if len(tags) > lim["hashtags"]:
            raise ValueError(f"Use at most {lim['hashtags']} hashtags.")
        entry["hashtags"] = tags
    if "posted" in body:
        entry["posted"] = bool(body.get("posted"))
        entry["posted_at"] = time.time() if entry["posted"] else None
    with _lock:
        data = _load_posts()
        data.setdefault(rel, {}).setdefault(platform, {}).update(entry)
        _save_posts(data)
    return post_state(rel)["posts"][platform]


# ---------------------------------------------------------------------------
# listing
# ---------------------------------------------------------------------------

def list_saved() -> list[dict[str, Any]]:
    """Productions in the output folder, newest first, each with its videos (the publishing-kit
    duplicates are left out: they are the same clips)."""
    base = exports.root()
    if not base.is_dir():
        return []
    posts = _load_posts()
    out = []
    for kind, cat in exports.CATEGORIES.items():
        cdir = base / cat
        if not cdir.is_dir():
            continue
        for folder in cdir.iterdir():
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            videos = []
            for f in sorted(folder.iterdir()):
                if f.is_file() and f.suffix.lower() in VIDEO_EXT and not f.name.startswith("."):
                    rel = _rel(f)
                    st = f.stat()
                    done = [p for p in POST_PLATFORMS if ((posts.get(rel) or {}).get(p) or {}).get("posted")]
                    videos.append({"path": rel, "name": f.name, "label": _clip_label(f.name),
                                   "size": st.st_size, "posted": done})
            if not videos:
                continue
            title, when = _title_and_date(folder.name)
            out.append({"folder": _rel(folder), "kind": kind, "category": cat, "title": title,
                        "date": when, "mtime": folder.stat().st_mtime,
                        "has_kit": (folder / KIT_DIR).is_dir(), "videos": videos})
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# covers, Finder
# ---------------------------------------------------------------------------

def thumb(rel: str) -> Path:
    path = resolve(rel)
    st = path.stat()
    key = hashlib.sha1(f"{path}:{st.st_size}:{st.st_mtime_ns}".encode()).hexdigest()[:16]
    out = THUMBS_DIR / f"{key}.jpg"
    if out.is_file():
        return out
    if not shutil.which("ffmpeg"):
        raise KeyError("no ffmpeg")
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.jpg")
    for at in ("3", "1", "0"):                       # well past the fade from black, if the clip is long enough
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", at, "-i", str(path), "-frames:v", "1",
                        "-vf", "scale=360:-2", "-q:v", "4", str(tmp)], capture_output=True, timeout=30)
        if tmp.is_file() and tmp.stat().st_size:
            tmp.replace(out)
            return out
    raise KeyError("no frame")


def reveal(rel: str) -> bool:
    """Select the file in Finder (the server runs on the user's own Mac)."""
    path = resolve(rel)
    if shutil.which("open"):
        subprocess.Popen(["open", "-R", str(path)])
        return True
    return False


def mark_sent(rel: str, platform: str, publish_id: str) -> None:
    """Remember that the video went to the network's inbox (TikTok drafts)."""
    with _lock:
        data = _load_posts()
        e = data.setdefault(rel, {}).setdefault(platform, {})
        e["sent_at"] = time.time()
        e["publish_id"] = publish_id
        _save_posts(data)
