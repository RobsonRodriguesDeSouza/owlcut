"""OwlCut clips — YouTube video → reviewed social clips.

Python here is only intake + persistence (AGENT_GUIDE: "Python = tools +
persistence"). The production itself runs as a clip-factory pipeline driven by
the agent through ``JobManager``; this module:

* probes a link for the preview card (title, channel, duration, captions),
* turns the /clips form into the agent's briefing,
* reads the clips contract the agent maintains at
  ``projects/<id>/clips/clips.json`` for the review page,
* keeps the user's publishing edits and builds the per-network export bundle.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from backlot.state import PROJECTS_DIR
from styles.clips.build import PREVIEW_DIR, STYLES

CLIPS_REL = "clips/clips.json"
EDITS_REL = "clips/publish_edits.json"
UI_BASE = "http://127.0.0.1:4750"

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}

PLATFORMS = {
    "tiktok": {"label": "TikTok", "aspect": "9:16", "max_seconds": 180},
    "reels": {"label": "Instagram Reels", "aspect": "9:16", "max_seconds": 90},
    "shorts": {"label": "YouTube Shorts", "aspect": "9:16", "max_seconds": 60},
    "feed": {"label": "Feed (Instagram/LinkedIn)", "aspect": "4:5", "max_seconds": 60},
    "youtube": {"label": "YouTube (landscape)", "aspect": "16:9", "max_seconds": 600},
}

LENGTHS = {
    "short": {"label": "Short · 15–30 s", "min": 15, "max": 30},
    "medium": {"label": "Medium · 30–60 s", "min": 30, "max": 60},
    "long": {"label": "Long · 60–90 s", "min": 60, "max": 90},
}

LIMITS = {
    "tiktok": {"caption": 2200, "hashtags": 30},
    "reels": {"caption": 2200, "hashtags": 30},
    "shorts": {"title": 100, "caption": 5000, "hashtags": 15},
    "feed": {"caption": 2200, "hashtags": 30},
    "youtube": {"title": 100, "caption": 5000, "hashtags": 15},
}


# ---------------------------------------------------------------------------
# Link parsing
# ---------------------------------------------------------------------------

def _parse_t(raw: str) -> Optional[float]:
    """YouTube t/start values: '90', '90s', '1m30s', '1h2m3s'."""
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(h * 3600 + mi * 60 + s)


def parse_link(url: str) -> dict[str, Any]:
    """Validate a YouTube link; return {url, video_id, start} or raise ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Paste the YouTube video link.")
    if not re.match(r"^https?://", url):
        url = "https://" + url
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        raise ValueError("For now OwlCut accepts YouTube links only (youtube.com or youtu.be).")
    q = parse_qs(u.query)
    video_id = None
    if host == "youtu.be":
        video_id = u.path.strip("/").split("/")[0] or None
    elif u.path.startswith("/watch"):
        video_id = (q.get("v") or [None])[0]
    else:
        m = re.match(r"^/(?:shorts|live|embed)/([\w-]{6,})", u.path)
        video_id = m.group(1) if m else None
    if not video_id or not re.fullmatch(r"[\w-]{6,20}", video_id):
        raise ValueError("That link doesn't point to a video. Use a video URL, not a channel or playlist.")
    start = _parse_t((q.get("t") or q.get("start") or [""])[0])
    clean = f"https://www.youtube.com/watch?v={video_id}"
    return {"url": clean, "video_id": video_id, "start": start}


def fmt_ts(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_ts(text: str) -> Optional[float]:
    """'1:23', '01:02:03', '83' → seconds."""
    text = (text or "").strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) > 3 or any(n < 0 for n in nums):
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


# ---------------------------------------------------------------------------
# Probe (preview card)
# ---------------------------------------------------------------------------

def probe(url: str) -> dict[str, Any]:
    """Metadata only — nothing is downloaded."""
    link = parse_link(url)
    import yt_dlp  # local import: optional dependency (requirements-clips.txt)

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(link["url"], download=False) or {}
    except Exception as exc:  # yt-dlp raises many types
        msg = str(exc)
        if "Private video" in msg or "Sign in" in msg:
            raise ValueError("This video is private or requires a login.") from exc
        raise ValueError(f"Couldn't read this video: {msg[:180]}") from exc

    subs = sorted((info.get("subtitles") or {}).keys())
    auto = sorted((info.get("automatic_captions") or {}).keys())
    chapters = [
        {"title": c.get("title", ""), "start": c.get("start_time", 0), "end": c.get("end_time", 0)}
        for c in (info.get("chapters") or [])
    ]
    return {
        **link,
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration") or 0,
        "duration_text": fmt_ts(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "is_live": bool(info.get("is_live")),
        "captions": {"manual": subs[:12], "auto_count": len(auto)},
        "chapters": chapters[:40],
        "width": info.get("width"),
        "height": info.get("height"),
    }


# ---------------------------------------------------------------------------
# Form options + briefing
# ---------------------------------------------------------------------------

def options() -> dict[str, Any]:
    return {
        "counts": [3, 5, 8, 10],
        "lengths": [{"value": k, "label": v["label"]} for k, v in LENGTHS.items()],
        "platforms": [{"value": k, "label": v["label"], "aspect": v["aspect"]} for k, v in PLATFORMS.items()],
        "languages": [
            {"value": "original", "label": "Same language as the video"},
            {"value": "en-US", "label": "English"},
            {"value": "pt-BR", "label": "Portuguese (Brazil)"},
            {"value": "es", "label": "Spanish"},
        ],
        "styles": _styles_with_previews(),
        "approvals": [
            {"value": "gates", "label": "Review the clips before rendering (recommended)"},
            {"value": "final", "label": "Only review the finished videos"},
        ],
    }


def _styles_with_previews() -> dict[str, Any]:
    out: dict[str, Any] = {}
    prefix = {"captions": "caption", "intros": "intro", "outros": "outro"}
    for group, items in STYLES.items():
        rows = []
        for it in items:
            row = {"value": it["id"], "label": it["label"], "desc": it.get("desc", "")}
            if group in prefix:
                f = PREVIEW_DIR / f"{prefix[group]}-{it['id']}.mp4"
                row["preview"] = f"/clip-previews/{f.name}?v={int(f.stat().st_mtime)}" if f.is_file() else None
            rows.append(row)
        out[group] = rows
    return out


def _style_ids(group: str) -> set[str]:
    return {it["id"] for it in STYLES[group]}


def style_text(form: dict[str, Any], project_id: str) -> str:
    caption = form.get("caption") if form.get("caption") in _style_ids("captions") else "highlight"
    intro = form.get("intro") if form.get("intro") in _style_ids("intros") else "hook"
    outro = form.get("outro") if form.get("outro") in _style_ids("outros") else "none"
    accent = form.get("accent") if form.get("accent") in _style_ids("accents") else "#ff2d55"
    cta = (str(form.get("cta_text") or "").strip() or "Follow for more")[:40]
    label = {g: {it["id"]: it["label"] for it in STYLES[g]} for g in STYLES}
    intro_text = {
        "hook": "the clip's `hook` (a short line, up to ~8 words)",
        "letters": "the clip's `hook`, cut down to 3-6 punchy words",
        "title-card": "the clip's `title`, with the channel name as `sub`",
        "none": "(no intro)",
    }[intro]
    return f"""## Clip look (chosen by the user — do not change)

- Captions: **{label['captions'][caption]}** (`caption="{caption}"`)
- Intro: **{label['intros'][intro]}** (`intro.style="{intro}"`), text: {intro_text}
- Ending: **{label['outros'][outro]}** (`outro.style="{outro}"`{f', text "{cta}"' if outro == "cta" else ''})
- Accent colour: `{accent}`

Build every clip with the clip kit (the same engine that rendered the previews the user picked from):
`styles/clips/build.py` → `build_clip_project(out_dir, video=<9:16 clip, already trimmed and reframed>,
duration=<s>, words=<words with start/end relative to the clip start>, caption=..., intro={{"style": ..., "text": ...,
"sub": ..., "duration": 2.4}}, outro={{"style": ..., "text": ...}}, accent=..., keywords=[2-4 key words of the clip])`.
Encode the raw clip with dense keyframes (`-c:v libx264 -crf 18 -g 30 -keyint_min 30 -c:a aac`).
Use `out_dir=projects/{project_id}/hyperframes/<id>_<aspect>/` and render with
`npx --yes hyperframes@0.9.41 render <out_dir> -o projects/{project_id}/renders/clips/<id>_<aspect>.mp4 --quality delivery`.
Before each final render, check 2-3 frames with `npx hyperframes snapshot` (captions readable, clear of the app buttons).
For 4:5 pass width=1080, height=1350. Words come from `transcriber` (word timestamps) — never invent timings.
"""


def contract_text(project_id: str) -> str:
    return f"""## Contract with the /clips review page (REQUIRED)

The OwlCut review page reads `projects/{project_id}/{CLIPS_REL}`. Create that file as soon as you have
candidates (end of the script stage) and UPDATE it at every stage (selection, render, publish). Write it
atomically (.tmp file + rename). Format (JSON, times in seconds of the original video):

```json
{{
  "version": 1,
  "source": {{"url": "...", "title": "...", "channel": "...", "duration_seconds": 0,
             "video_path": "assets/video/source.mp4", "transcript_path": "assets/transcript.json"}},
  "status": "analyzing | awaiting_selection | rendering | awaiting_review | published",
  "clips": [
    {{
      "id": "c01",
      "title": "short, strong clip title",
      "hook": "the line spoken in the first 2-3 seconds",
      "start": 123.4, "end": 158.9,
      "score": 87,
      "scores": {{"hook": 9, "coherence": 8, "value": 9, "energy": 8, "platform_fit": 9}},
      "reason": "why this segment works on its own",
      "transcript": "what is said in the segment",
      "crop": "centred face | two speakers | screen/slides | hard",
      "status": "candidate | selected | rejected | rendered",
      "renders": {{"9:16": "renders/clips/c01_9x16.mp4"}},
      "thumbnail": "renders/clips/c01_cover.jpg",
      "publish": {{
        "tiktok": {{"caption": "post caption (hook on the first line)", "hashtags": ["#tag1", "#tag2"]}},
        "shorts": {{"title": "title up to 100 characters", "caption": "description", "hashtags": ["#shorts"]}}
      }}
    }}
  ]
}}
```

- `video_path`, `renders` and `thumbnail` are relative to `projects/{project_id}/`.
- Sort `clips` by `score` (0-100), highest first. Also include the strongest rejected candidates
  (status `rejected`) so the user can bring them back.
- Start and end never cut a sentence in half; check against the word-timestamped transcript.
- Publish stage: fill `publish` for every rendered clip, only for the requested platforms
  (keys: tiktok, reels, shorts, feed, youtube). Caption in the requested language, hook on the first line,
  3-5 specific hashtags (no pile of generic #fyp), never promise what the clip doesn't deliver.
  `title` is required for shorts and youtube (max 100 characters). If the video isn't the user's own,
  end the caption with "Credit: <original channel>". Then set the overall status to `published`.
  The user reviews and copies the texts at {UI_BASE}/publish/{project_id}.
"""


def build_prompt(form: dict[str, Any], project_id: str, meta: dict[str, Any]) -> str:
    o = options()
    count = int(form.get("count") or 5)
    length = LENGTHS.get(form.get("length") or "medium", LENGTHS["medium"])
    plats = [p for p in (form.get("platforms") or ["tiktok", "reels", "shorts"]) if p in PLATFORMS]
    if not plats:
        plats = ["tiktok", "reels", "shorts"]
    aspects = sorted({PLATFORMS[p]["aspect"] for p in plats})
    plat_lines = ", ".join(f"{PLATFORMS[p]['label']} ({PLATFORMS[p]['aspect']}, up to {PLATFORMS[p]['max_seconds']} s)" for p in plats)

    focus_start = parse_ts(str(form.get("focus_start") or "")) if form.get("focus_start") else meta.get("start")
    focus_end = parse_ts(str(form.get("focus_end") or "")) if form.get("focus_end") else None
    if focus_start is not None and focus_end is not None and focus_end > focus_start:
        focus = (f"The user marked {fmt_ts(focus_start)}–{fmt_ts(focus_end)}: prefer clips inside it "
                 "(you may still suggest 1-2 from outside if clearly better; say so in `reason`).")
    elif focus_start is not None:
        focus = (f"The user pasted the link at {fmt_ts(focus_start)}: the first clip must start from that point "
                 "(adjusted to begin on a full sentence); the others can come from the whole video.")
    else:
        focus = "No range marked: mine the whole video and spread the clips across its content."

    lang = form.get("language") or "original"
    lang_line = _label(o["languages"], lang)
    approvals = form.get("approvals") or "gates"
    duration = int(meta.get("duration") or 0)
    notes = (form.get("notes") or "").strip()

    approval_text = (
        "Stop at the script-stage gate with the candidates in clips.json (overall status `awaiting_selection`). "
        "The user's reply carries the final selection and start/end adjustments — apply them exactly."
        if approvals == "gates" else
        "The user pre-approved the selection: pick the best candidates yourself (log it in decision_log), "
        "but STOP at the final gate with the rendered clips (overall status `awaiting_review`)."
    )

    lines = [
        "CLIPS request from the OwlCut /clips page (the OpenMontage web UI).",
        "",
        "This is a source-footage request (\"cut this video into clips\"), not a reference request:",
        "use the **clip-factory** pipeline (pipeline_defs/clip-factory.yaml) with each stage's director skill.",
        "",
        "## Brief",
        f"- Source video: {meta.get('url')}",
        f"- Title: {meta.get('title') or '(unknown)'} — channel: {meta.get('channel') or '(unknown)'}",
        f"- Original length: {fmt_ts(duration)} ({duration} s)",
        f"- YouTube captions: {', '.join(meta.get('captions', {}).get('manual') or []) or 'auto-generated only or none'}",
        f"- Number of clips: {count}",
        f"- Length of each clip: {length['min']}–{length['max']} s",
        f"- Platforms: {plat_lines}",
        f"- Formats to render per clip: {', '.join(aspects)}",
        f"- Language of captions and texts: {lang_line}",
        f"- Range of interest: {focus}",
        f"- User notes: {notes or '(none)'}",
        "",
        "## How to produce",
        f"1. Download with `video_downloader` (format `video`, max_resolution `1080p`, "
        f"max_duration_seconds {max(duration + 120, 600)}) into `projects/{project_id}/assets/video/` and "
        "rename the video to `source.mp4`.",
        "2. Transcription: try `transcript_fetcher` (YouTube captions) to map the content quickly; for the "
        "candidate segments use `transcriber` (faster-whisper) with word timestamps — animated captions and "
        "cut points depend on them.",
        "3. Rank candidates with the script-director criteria (hook, stands alone, value, energy, platform fit). "
        "Generate at least twice as many candidates as requested and select the best.",
        "4. For 9:16 and 4:5 use `face_tracker` + `auto_reframe` (framing that follows the face). "
        "Render each clip to `renders/clips/<id>_<aspect>.mp4` (aspect written with x: 9x16, 4x5, 16x9) "
        "and make a cover `renders/clips/<id>_cover.jpg`.",
        "5. Captions, intro and ending come from the clip kit (section below) — it already keeps clear of the "
        "platforms' UI (bottom and right edge free in 9:16).",
        "",
        style_text(form, project_id),
        contract_text(project_id),
        "## Session rules",
        "1. Read AGENT_GUIDE.md before anything else and follow the pipeline strictly (Rule Zero, director skills, "
        "checkpoints, schemas).",
        f"2. Use exactly the project_id `{project_id}` (folder projects/{project_id}/) and create project.json "
        "right away (init_project with pipeline_type `clip-factory`).",
        f"3. {approval_text}",
        "4. You are running without an interactive terminal: AskUserQuestion is NOT available. At each gate, write "
        "the checkpoint as awaiting_human and END the turn with a short final message in English: what was done, "
        f"what to review and the options. Say the visual review is at {UI_BASE}/clips/{project_id}. "
        "The answer arrives as the next message.",
        "5. Never replace choices from the brief on your own. If something blocks (download failed, protected "
        "video, missing tool), stop and explain the alternatives.",
        "6. Activate the venv (`source .venv/bin/activate`) and run scripts with `PYTHONPATH=.` from the repo root.",
    ]
    return "\n".join(lines)


def _label(opts: list[dict], value: Any) -> str:
    for opt in opts:
        if str(opt["value"]) == str(value):
            return opt["label"]
    return str(value)


# ---------------------------------------------------------------------------
# Clips contract (read side)
# ---------------------------------------------------------------------------

def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def load_clips(project_id: str) -> Optional[dict[str, Any]]:
    """Read and normalise projects/<id>/clips/clips.json; None when absent."""
    path = PROJECTS_DIR / project_id / CLIPS_REL
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"error": "clips.json is being written or is invalid; the page will retry on its own.", "clips": []}
    if not isinstance(data, dict):
        return None
    project_dir = PROJECTS_DIR / project_id

    def _exists(rel: Any) -> Optional[str]:
        if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return None
        return rel if (project_dir / rel).is_file() else None

    clips = []
    for i, c in enumerate(data.get("clips") or []):
        if not isinstance(c, dict):
            continue
        start, end = _num(c.get("start")), _num(c.get("end"))
        if start is None or end is None or end <= start:
            continue
        renders = {}
        for aspect, rel in (c.get("renders") or {}).items() if isinstance(c.get("renders"), dict) else []:
            ok = _exists(rel)
            if ok:
                renders[str(aspect)] = ok
        clips.append({
            "id": str(c.get("id") or f"c{i + 1:02d}")[:24],
            "title": str(c.get("title") or "")[:160],
            "hook": str(c.get("hook") or "")[:300],
            "start": start,
            "end": end,
            "duration": round(end - start, 2),
            "score": _num(c.get("score")),
            "scores": c.get("scores") if isinstance(c.get("scores"), dict) else {},
            "reason": str(c.get("reason") or "")[:600],
            "transcript": str(c.get("transcript") or "")[:2000],
            "crop": str(c.get("crop") or "")[:80],
            "status": str(c.get("status") or "candidate"),
            "renders": renders,
            "thumbnail": _exists(c.get("thumbnail")),
            "publish": c.get("publish") if isinstance(c.get("publish"), dict) else None,
        })
    src = data.get("source") if isinstance(data.get("source"), dict) else {}
    return {
        "version": data.get("version", 1),
        "status": str(data.get("status") or ""),
        "source": {
            "url": src.get("url"),
            "title": src.get("title"),
            "channel": src.get("channel"),
            "duration_seconds": _num(src.get("duration_seconds")),
            "video_path": _exists(src.get("video_path")),
        },
        "clips": clips,
    }


# ---------------------------------------------------------------------------
# Publishing kit (/publish)
# ---------------------------------------------------------------------------

def _clean_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    tags = []
    for t in raw or []:
        t = re.sub(r"[^\w]", "", str(t).strip().lstrip("#"), flags=re.UNICODE)
        if t and f"#{t}" not in tags:
            tags.append(f"#{t}")
    return tags


def _load_edits(project_id: str) -> dict[str, Any]:
    path = PROJECTS_DIR / project_id / EDITS_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def publish_state(project_id: str, platforms: list[str]) -> dict[str, Any]:
    """Rendered clips with per-platform texts: agent draft (clips.json) overlaid by user edits."""
    clips = load_clips(project_id) or {"clips": [], "source": {}, "status": ""}
    edits = _load_edits(project_id)
    plats = [p for p in platforms if p in PLATFORMS] or ["tiktok", "reels", "shorts"]
    rows = []
    for i, c in enumerate(clips.get("clips") or []):
        if not c.get("renders"):
            continue
        draft = c.get("publish") or {}
        per = {}
        for p in plats:
            d = draft.get(p) if isinstance(draft.get(p), dict) else {}
            e = (edits.get(c["id"]) or {}).get(p) or {}
            aspect = PLATFORMS[p]["aspect"]
            video = c["renders"].get(aspect) or next(iter(c["renders"].values()))
            per[p] = {
                "title": str(e.get("title", d.get("title", c.get("title") or "")))[:LIMITS[p].get("title", 200)],
                "caption": str(e.get("caption", d.get("caption", ""))),
                "hashtags": _clean_tags(e.get("hashtags", d.get("hashtags", []))),
                "posted": bool(e.get("posted")),
                "posted_at": e.get("posted_at"),
                "edited": bool(e),
                "has_draft": bool(d),
                "video": video,
                "aspect_ok": aspect in c["renders"],
            }
        rows.append({"id": c["id"], "rank": i + 1, "title": c.get("title"), "duration": c.get("duration"),
                     "thumbnail": c.get("thumbnail"), "renders": c["renders"], "platforms": per})
    return {"status": clips.get("status"), "source": clips.get("source"), "platforms": plats,
            "limits": {p: LIMITS[p] for p in plats},
            "labels": {p: PLATFORMS[p]["label"] for p in plats}, "clips": rows}


def save_edit(project_id: str, clip_id: str, platform: str, body: dict[str, Any]) -> dict[str, Any]:
    if platform not in PLATFORMS:
        raise ValueError("Unknown platform.")
    if not re.fullmatch(r"[\w-]{1,24}", clip_id or ""):
        raise ValueError("Invalid clip.")
    lim = LIMITS[platform]
    entry: dict[str, Any] = {}
    if "title" in body:
        title = str(body.get("title") or "").strip()
        if "title" in lim and len(title) > lim["title"]:
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
    path = PROJECTS_DIR / project_id / EDITS_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    edits = _load_edits(project_id)
    edits.setdefault(clip_id, {}).setdefault(platform, {}).update(entry)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(edits, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return edits[clip_id][platform]


def post_text(entry: dict[str, Any]) -> str:
    parts = [entry.get("caption", "").strip()]
    if entry.get("hashtags"):
        parts.append(" ".join(entry["hashtags"]))
    return "\n\n".join(p for p in parts if p)


def build_bundle(project_id: str, platforms: list[str], title: str = "") -> bytes:
    """Zip: <platform>/<NN>-<slug>.mp4 + .txt (+ cover), and a posting checklist."""
    import io
    import zipfile

    state = publish_state(project_id, platforms)
    project_dir = PROJECTS_DIR / project_id
    buf = io.BytesIO()
    lines = [f"Publishing kit — {title or project_id}", ""]
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for c in state["clips"]:
            plain = unicodedata.normalize("NFKD", c.get("title") or c["id"]).encode("ascii", "ignore").decode()
            slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:40] or c["id"]
            stem = f"{c['rank']:02d}-{slug}"
            for p, e in c["platforms"].items():
                folder = state["labels"][p].replace("/", "-")
                zf.write(project_dir / e["video"], f"{folder}/{stem}.mp4")
                txt = post_text(e)
                if p in ("shorts", "youtube") and e.get("title"):
                    txt = f"TITLE: {e['title']}\n\n{txt}"
                zf.writestr(f"{folder}/{stem}.txt", txt + "\n")
                if c.get("thumbnail"):
                    zf.write(project_dir / c["thumbnail"], f"{folder}/{stem}-cover{Path(c['thumbnail']).suffix}")
                lines.append(f"[{'x' if e['posted'] else ' '}] {state['labels'][p]} — {stem}")
        lines += ["", "Tip: post one clip a day per network, at the hours your audience watches most."]
        zf.writestr("CHECKLIST.txt", "\n".join(lines) + "\n")
    return buf.getvalue()
