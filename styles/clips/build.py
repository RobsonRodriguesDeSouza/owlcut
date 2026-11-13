"""Build HyperFrames projects for vertical clips with the OwlCut clip kit.

One builder serves two callers so preview and delivery cannot drift apart:

* the OwlCut style gallery (``python -m styles.clips.build previews``) renders
  a short sample of every caption/intro look to ``styles/clips/previews/*.mp4``;
* the clip-factory agent builds one project per clip with
  ``build_clip_project(...)`` and renders it with ``npx hyperframes render``.

Styles are declared once in ``STYLES`` below (also served to the /clips form).
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
KIT_DIR = HERE / "kit"
PREVIEW_DIR = HERE / "previews"
HF_VERSION = "0.9.41"
GSAP_URL = "https://cdn.jsdelivr.net/npm/gsap@3.15.0/dist/gsap.min.js"
FONTS_URL = ("https://fonts.googleapis.com/css2?family=Anton&family=Montserrat:wght@500;800"
             "&family=Poppins:wght@700&display=swap")

STYLES: dict[str, list[dict[str, str]]] = {
    "captions": [
        {"id": "highlight", "label": "Highlight", "desc": "Bold caps; the spoken word gets a coloured block.",
         "registry": "caption-highlight"},
        {"id": "karaoke", "label": "Karaoke", "desc": "Line in a light pill; each word darkens as it is spoken.",
         "registry": "caption-pill-karaoke"},
        {"id": "clean", "label": "Clean", "desc": "White with a soft shadow, revealed word by word. Calm, good for interviews.",
         "registry": "caption-clip-wipe"},
        {"id": "impact", "label": "Impact", "desc": "One huge word at a time, slamming in from alternating sides.",
         "registry": "caption-kinetic-slam"},
        {"id": "none", "label": "No captions", "desc": "Just the video (and the intro, if any).", "registry": ""},
    ],
    "intros": [
        {"id": "hook", "label": "Hook", "desc": "The hook line blurs in at the top for the first 2 seconds.",
         "registry": "blur-in"},
        {"id": "letters", "label": "Rising letters", "desc": "The hook in big letters that rise one by one.",
         "registry": "bottom-up-letters"},
        {"id": "title-card", "label": "Title card", "desc": "White card with a colour tab: clip title and channel name.",
         "registry": "lt-clean-bar"},
        {"id": "none", "label": "No intro", "desc": "Starts straight on the speech.", "registry": ""},
    ],
    "outros": [
        {"id": "cta", "label": "Call to action", "desc": "Coloured pill in the last 2 s (e.g. “Follow for part 2”)."},
        {"id": "none", "label": "No call to action", "desc": "Ends on the last line."},
    ],
    "accents": [
        {"id": "#ff2d55", "label": "Red"},
        {"id": "#ffd400", "label": "Yellow"},
        {"id": "#c5d28c", "label": "Olive"},
        {"id": "#3d8bff", "label": "Blue"},
        {"id": "#ffffff", "label": "White"},
    ],
}

SAMPLE_WORDS_TEXT = ("Nobody tells you this, but the secret of a great clip lives in the first three seconds. "
                     "If the hook does not grab you, nothing else matters.")


def sample_words(text: str = SAMPLE_WORDS_TEXT, start: float = 0.25, wps: float = 3.1) -> list[dict[str, Any]]:
    """Evenly timed sample words (previews only; real clips use transcriber word timings)."""
    out, t = [], start
    for w in text.split():
        d = max(0.18, min(0.6, (len(w) + 2) / (wps * 6)))
        out.append({"text": w, "start": round(t, 3), "end": round(t + d, 3)})
        t += d + (0.28 if w.endswith((".", ",", "?", "!")) else 0.04)
    return out


def accent_ink(hex_color: str) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return "#ffffff"
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return "#111111" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


def _page(*, duration: float, width: int, height: int, video_rel: Optional[str], config: dict[str, Any],
          kit_prefix: str, title: str) -> str:
    if video_rel:
        background = (
            f'<video id="src" class="clip" data-start="0" data-duration="{duration:.3f}" data-track-index="0" '
            f'data-has-audio="true" src="{html.escape(video_rel)}" playsinline '
            'style="position:absolute;inset:0;width:100%;height:100%;object-fit:cover"></video>')
    else:  # preview ground: a stand-in "speaker" over a dark gradient, laid out in % so any size works
        background = (
            f'<div id="ground" class="clip" data-start="0" data-duration="{duration:.3f}" data-track-index="0" '
            'style="position:absolute;inset:0;overflow:hidden;background:radial-gradient(120% 80% at 30% 20%,#3b4a5c 0%,#1a1f27 55%,#0c0e12 100%)">'
            '<div id="glow" style="position:absolute;width:85%;aspect-ratio:1;left:-20%;top:27%;border-radius:50%;'
            'background:radial-gradient(circle,rgba(255,196,120,.35),rgba(255,196,120,0) 70%)"></div>'
            '<div style="position:absolute;left:20%;width:60%;bottom:-6%;height:52%;border-radius:50% 50% 0 0/30% 30% 0 0;'
            'background:linear-gradient(180deg,#56606d,#2a3038)"></div>'
            '<div style="position:absolute;left:33%;width:34%;top:19%;height:24%;border-radius:50%;'
            'background:linear-gradient(180deg,#8d97a3,#5b6470)"></div></div>')
    ground_motion = "" if video_rel else (
        'tl.to("#glow",{xPercent:60,yPercent:-30,duration:%.3f,ease:"sine.inOut"},0);' % duration)
    cfg = json.dumps(config, ensure_ascii=False)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width={width}, height={height}" />
    <title>{html.escape(title)}</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="{FONTS_URL}" rel="stylesheet" />
    <script src="{GSAP_URL}"></script>
    <link rel="stylesheet" href="{kit_prefix}kit/clip-kit.css" />
    <script src="{kit_prefix}kit/clip-kit.js"></script>
    <style>
      * {{ margin: 0; padding: 0; box-sizing: border-box; }}
      html, body {{ width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
      #root {{ position: relative; width: {width}px; height: {height}px; overflow: hidden; background: #000; }}
    </style>
  </head>
  <body>
    <div id="root" data-composition-id="main" data-start="0" data-duration="{duration:.3f}"
         data-width="{width}" data-height="{height}" data-fps="30">
      {background}
      <div id="ck" class="clip" data-start="0" data-duration="{duration:.3f}" data-track-index="1"
           style="position:absolute;inset:0;z-index:5"></div>
    </div>
    <script>
      window.__timelines = window.__timelines || {{}};
      const tl = gsap.timeline({{ paused: true }});
      {ground_motion}
      ClipKit.mount(tl, document.getElementById("ck"), {cfg});
      tl.set({{}}, {{}}, {duration:.3f});
      window.__timelines["main"] = tl;
      tl.seek(0);
    </script>
  </body>
</html>
"""


def kit_config(*, duration: float, words: list[dict[str, Any]], caption: str = "highlight",
               intro: Optional[dict[str, Any]] = None, outro: Optional[dict[str, Any]] = None,
               accent: str = "#ff2d55", keywords: Optional[list[str]] = None,
               width: int = 1080, height: int = 1920) -> dict[str, Any]:
    return {
        "width": width, "height": height, "duration": round(duration, 3),
        "words": [{"text": str(w["text"]), "start": round(float(w["start"]), 3), "end": round(float(w["end"]), 3)}
                  for w in words if str(w.get("text", "")).strip()],
        "caption": caption,
        "intro": intro or {"style": "none"},
        "outro": outro or {"style": "none"},
        "accent": accent, "accentInk": accent_ink(accent),
        "keywords": keywords or [],
    }


def build_clip_project(out_dir: Path, *, video: Path, duration: float, words: list[dict[str, Any]],
                       caption: str = "highlight", intro: Optional[dict[str, Any]] = None,
                       outro: Optional[dict[str, Any]] = None, accent: str = "#ff2d55",
                       keywords: Optional[list[str]] = None, width: int = 1080, height: int = 1920,
                       title: str = "Clip") -> Path:
    """Write a self-contained HyperFrames project for one clip.

    ``video`` is the already trimmed (and reframed) clip; word times are in
    seconds from the start of that clip. Render with:
    ``npx --yes hyperframes@0.9.41 render <out_dir> -o <file>.mp4 --quality delivery``.
    """
    out_dir = Path(out_dir)
    (out_dir / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copytree(KIT_DIR, out_dir / "kit", dirs_exist_ok=True)
    target = out_dir / "assets" / Path(video).name
    if Path(video).resolve() != target.resolve():
        shutil.copy2(video, target)
    cfg = kit_config(duration=duration, words=words, caption=caption, intro=intro, outro=outro,
                     accent=accent, keywords=keywords, width=width, height=height)
    (out_dir / "index.html").write_text(
        _page(duration=duration, width=width, height=height, video_rel=f"assets/{target.name}",
              config=cfg, kit_prefix="", title=title), encoding="utf-8")
    (out_dir / "clip.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    return out_dir / "index.html"


# ---------------------------------------------------------------------------
# Style gallery previews
# ---------------------------------------------------------------------------

PREVIEW_HOOK = "The secret is in the first 3 seconds"


def _preview_specs() -> list[tuple[str, dict[str, Any]]]:
    words = sample_words(start=0.3)
    dur = 6.0
    specs = []
    for c in STYLES["captions"]:
        if c["id"] == "none":
            continue
        specs.append((f"caption-{c['id']}", kit_config(duration=dur, words=words, caption=c["id"],
                                                         keywords=["secret", "hook"], width=540, height=960)))
    intro_words = sample_words(start=2.6)
    for i in STYLES["intros"]:
        if i["id"] == "none":
            continue
        specs.append((f"intro-{i['id']}", kit_config(
            duration=4.2, words=intro_words, caption="clean",
            intro={"style": i["id"], "text": PREVIEW_HOOK, "sub": "Example Channel", "duration": 2.6},
            keywords=["secret"], width=540, height=960)))
    specs.append(("outro-cta", kit_config(
        duration=3.2, words=[], caption="none", outro={"style": "cta", "text": "Follow for part 2", "duration": 2.6},
        width=540, height=960)))
    return specs


def build_previews(render: bool = True, only: Optional[str] = None) -> list[Path]:
    """Write .build/<id>.html compositions and render them to previews/<id>.mp4."""
    build = HERE / ".build"
    build.mkdir(exist_ok=True)
    PREVIEW_DIR.mkdir(exist_ok=True)
    outs = []
    for pid, cfg in _preview_specs():
        if only and only not in pid:
            continue
        comp = build / f"{pid}.html"
        comp.write_text(_page(duration=cfg["duration"], width=cfg["width"], height=cfg["height"], video_rel=None,
                              config=cfg, kit_prefix="../", title=pid), encoding="utf-8")
        out = PREVIEW_DIR / f"{pid}.mp4"
        if render:
            subprocess.run(["npx", "--yes", f"hyperframes@{HF_VERSION}", "render", str(HERE), "-c", f".build/{pid}.html",
                            "-o", str(out), "--quality", "draft", "--quiet"], check=True)
        outs.append(out)
    return outs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("previews", help="render the style gallery previews")
    p.add_argument("--only", help="render only ids containing this text")
    p.add_argument("--no-render", action="store_true")
    sub.add_parser("styles", help="print the style catalogue as JSON")
    args = ap.parse_args()
    if args.cmd == "previews":
        for o in build_previews(render=not args.no_render, only=args.only):
            print(o)
    elif args.cmd == "styles":
        print(json.dumps(STYLES, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
