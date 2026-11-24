"""Finished videos, mirrored into a folder the user owns.

Every finished output also appears under ``~/Documents/OwlCutVideos``
(override with ``OWLCUT_OUTPUT_DIR``), one folder per production:

    OwlCutVideos/
      AIvideos/<Title>_<YYYY-MM-DD_HH-MM>/       videos made from scratch (New video)
      Clips/<Title>_<YYYY-MM-DD_HH-MM>/          clips to post (AI agent or Local)
      RankingVideos/<Title>_<YYYY-MM-DD_HH-MM>/  ranking videos

Files are APFS clones of what the app renders: separate, visible files that
share the disk blocks, so they take no extra space, and the app (library,
players, publish) keeps working from its own folders. Deleting something in the
app doesn't touch this folder, and vice versa. Elsewhere the file is copied.

Exporting must never break a render: every public function here swallows and
logs its own errors.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("owlcut.exports")

CATEGORIES = {"video": "AIvideos", "clips": "Clips", "ranking": "RankingVideos"}
INDEX_NAME = ".owlcut-index.json"
MEDIA_EXT = {".mp4", ".mov", ".webm", ".m4v", ".srt", ".vtt", ".png", ".jpg", ".jpeg"}
_lock = threading.Lock()


def root() -> Path:
    raw = os.environ.get("OWLCUT_OUTPUT_DIR")
    if not raw:
        try:
            from backlot.jobs import _dotenv_value  # .env support, like the agent model
            raw = _dotenv_value("OWLCUT_OUTPUT_DIR")
        except Exception:
            raw = None
    return Path(raw).expanduser() if raw else Path.home() / "Documents" / "OwlCutVideos"


def safe_name(text: str, max_len: int = 60) -> str:
    """A readable folder/file name: keeps accents and spaces, drops what Finder or other OSes reject."""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:max_len].rstrip(" .") or "Untitled"


def _stamp(ts: Optional[float]) -> str:
    return time.strftime("%Y-%m-%d_%H-%M", time.localtime(ts or time.time()))


def _load_index(base: Path) -> dict[str, str]:
    try:
        data = json.loads((base / INDEX_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def folder_for(kind: str, key: str, name: str, created_at: Optional[float]) -> Path:
    """The production's folder; the same one on every call (remembered in an index)."""
    base = root()
    cat = base / CATEGORIES[kind]
    ikey = f"{kind}:{key}"
    with _lock:
        index = _load_index(base)
        rel = index.get(ikey)
        if rel and (base / rel).is_dir():
            return base / rel
        stem = f"{safe_name(name)}_{_stamp(created_at)}"
        folder, n = cat / stem, 2
        while folder.exists():
            folder, n = cat / f"{stem} ({n})", n + 1
        folder.mkdir(parents=True)
        index[ikey] = str(folder.relative_to(base))
        tmp = base / (INDEX_NAME + ".tmp")
        tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(base / INDEX_NAME)
        return folder


_UF_HIDDEN = getattr(stat, "UF_HIDDEN", 0x8000)


def _clone(src: Path, dest: Path) -> bool:
    """APFS copy-on-write clone (macOS): a separate file that shares the disk blocks — no extra space."""
    if sys.platform != "darwin":
        return False
    try:
        import ctypes
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        return libc.clonefile(os.fsencode(str(src)), os.fsencode(str(dest)), 0) == 0
    except (OSError, AttributeError):
        return False


def _unhide(path: Path) -> None:
    """The app keeps its files under .backlot, whose contents macOS flags hidden — a copy carries that
    flag along, and Finder would show the export folder as empty."""
    try:
        flags = path.lstat().st_flags
        if flags & _UF_HIDDEN:
            os.chflags(path, flags & ~_UF_HIDDEN)
    except (OSError, AttributeError, NotImplementedError):
        pass


def _place(src: Path, dest: Path) -> bool:
    """Clone (or copy) src to dest unless dest already is an up-to-date copy. True when something changed.

    Not a hard link: a hard link *is* the app's file, so it shares its hidden flag (invisible in
    Finder) and any later change. A clone is its own file and, on APFS, costs no extra space.
    """
    try:
        if dest.exists():
            a, b = src.stat(), dest.stat()
            same_inode = (a.st_ino, a.st_dev) == (b.st_ino, b.st_dev)
            if not same_inode and a.st_size == b.st_size and b.st_mtime >= a.st_mtime:
                _unhide(dest)
                return False                       # an up-to-date copy
            dest.unlink()                          # old export or a hard link from earlier versions
        tmp = dest.with_name(f".{dest.name}.{os.getpid()}.part")
        tmp.unlink(missing_ok=True)
        if not _clone(src, tmp):
            shutil.copy2(src, tmp)
        _unhide(tmp)
        tmp.replace(dest)
        return True
    except OSError as exc:
        log.warning("export %s -> %s failed: %s", src, dest, exc)
        return False


def publish(kind: str, key: str, name: str, created_at: Optional[float],
            files: Iterable[tuple[Path, str]]) -> Optional[Path]:
    """Mirror ``files`` (source path, file name in the export folder) into the production's folder.

    Nothing is created until there is at least one file to export.
    """
    try:
        items = [(Path(s), safe_name(Path(n).stem, 90) + Path(n).suffix.lower()) for s, n in files]
        items = [(s, n) for s, n in items if s.is_file() and s.stat().st_size > 0]
        if not items:
            return None
        folder = folder_for(kind, key, name, created_at)
        for src, fname in items:
            _place(src, folder / fname)
        return folder
    except Exception as exc:  # never break the caller
        log.warning("export of %s:%s failed: %s", kind, key, exc)
        return None


def forget(kind: str, key: str, delete_files: bool = False) -> int:
    """Drop a production from the index; with ``delete_files`` also remove its folder. Returns bytes removed."""
    base = root()
    ikey = f"{kind}:{key}"
    with _lock:
        index = _load_index(base)
        rel = index.pop(ikey, None)
        if rel is None:
            return 0
        freed = 0
        folder = (base / rel).resolve()
        if delete_files and folder.is_dir() and base.resolve() in folder.parents:
            freed = dir_size(folder)
            shutil.rmtree(folder, ignore_errors=True)
        tmp = base / (INDEX_NAME + ".tmp")
        tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(base / INDEX_NAME)
        return freed


def dir_size(path: Path) -> int:
    """Bytes a folder really frees: hard-linked files shared with another place count as 0."""
    total = 0
    for f in path.rglob("*"):
        try:
            st = f.lstat()
        except OSError:
            continue
        if f.is_file() and st.st_nlink <= 1:
            total += st.st_size
    return total


def existing_folder(kind: str, key: str) -> Optional[Path]:
    base = root()
    rel = _load_index(base).get(f"{kind}:{key}")
    return base / rel if rel and (base / rel).is_dir() else None


# ---------------------------------------------------------------------------
# Agent productions (projects/<id>)
# ---------------------------------------------------------------------------

def project_files(project_dir: Path, kind: str) -> list[tuple[Path, str]]:
    renders = project_dir / "renders"
    out: list[tuple[Path, str]] = []
    if kind == "clips":
        titles: dict[str, str] = {}
        try:
            doc = json.loads((project_dir / "clips" / "clips.json").read_text(encoding="utf-8"))
            titles = {c["id"]: c.get("title") or "" for c in doc.get("clips") or [] if c.get("id")}
        except (OSError, ValueError, TypeError, KeyError):
            pass
        cdir = renders / "clips"
        if cdir.is_dir():
            for f in sorted(cdir.iterdir()):
                if f.suffix.lower() not in MEDIA_EXT:
                    continue
                cid, _, rest = f.stem.partition("_")
                label = f"{cid} - {titles[cid]}" if titles.get(cid) else cid
                out.append((f, f"{label} ({rest}){f.suffix}" if rest else f.name))
        return out
    if renders.is_dir():
        out = [(f, f.name) for f in sorted(renders.iterdir()) if f.is_file() and f.suffix.lower() in MEDIA_EXT]
    return out


def project_title(title: str, project_id: str) -> str:
    return re.sub(r"^Clips\s*[—-]\s*", "", title or project_id)


def project_folder(project_id: str, kind: str, title: str, created_at: Optional[float]) -> Path:
    """The OwlCutVideos folder of an agent production (created if needed)."""
    cat = "clips" if kind == "clips" else "video"
    return folder_for(cat, f"project:{project_id}", project_title(title, project_id), created_at)


def export_project(project_id: str, kind: str, title: str, created_at: Optional[float]) -> Optional[Path]:
    try:
        from backlot import branding
        from lib.paths import PROJECTS_DIR
        pdir = PROJECTS_DIR / project_id
        cat = "clips" if kind == "clips" else "video"
        files = [(branding.branded_copy(src, cat), name) for src, name in project_files(pdir, kind)]
        return publish(cat, f"project:{project_id}",
                       project_title(title, project_id), created_at, files)
    except Exception as exc:
        log.warning("export of project %s failed: %s", project_id, exc)
        return None


# ---------------------------------------------------------------------------
# One-off: export everything that already exists
# ---------------------------------------------------------------------------

def export_all() -> list[Path]:
    """Mirror every finished production already in the app (safe to run repeatedly)."""
    from backlot import quickclips, ranking
    from backlot.jobs import JOBS_DIR
    from lib.paths import PROJECTS_DIR

    done: list[Path] = []
    seen: set[str] = set()
    if JOBS_DIR.is_dir():
        for jf in sorted(JOBS_DIR.glob("*/job.json")):
            try:
                job = json.loads(jf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            seen.add(job.get("project_id") or "")
            p = export_project(job["project_id"], job.get("kind") or "video", job.get("title") or "", job.get("created_at"))
            if p:
                done.append(p)
    if PROJECTS_DIR.is_dir():
        for pdir in sorted(PROJECTS_DIR.iterdir()):
            meta_f = pdir / "project.json"
            if pdir.name in seen or not meta_f.is_file():
                continue
            try:
                meta = json.loads(meta_f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            kind = "clips" if meta.get("pipeline_type") == "clip-factory" else "video"
            created = None
            try:
                from datetime import datetime
                created = datetime.fromisoformat(meta["created_at"]).timestamp()
            except Exception:
                created = meta_f.stat().st_mtime
            p = export_project(pdir.name, kind, meta.get("title") or pdir.name, created)
            if p:
                done.append(p)
    for q in quickclips.list_all():
        p = quickclips.export(q["id"])
        if p:
            done.append(p)
    for r in ranking.list_all():
        p = ranking.export(r["id"])
        if p:
            done.append(p)
    return done


if __name__ == "__main__":
    for path in export_all():
        print(path)
