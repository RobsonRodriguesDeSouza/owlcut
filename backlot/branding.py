"""Watermark (logo + @handle) burned into finished videos.

Settings page: /settings. Storage: ``.backlot/branding/``
    branding.json      handle, position, size, opacity, which kinds get it
    logo_source.<ext>  the image as uploaded
    logo.png           512×512 square crop on white (drawn as a circle, optional white ring)
    side_logo.png      the second, "side" watermark — kept in its own shape and transparency

The watermark is drawn once, with Pillow, as a transparent "badge" PNG (round
logo + handle, soft shadow, opacity baked into the alpha) sized for the target
frame width. Renders then just ``overlay`` it — so the in-app preview is the
same image the videos get.

Where it applies:
  * Local clips and rankings: burned in during their own FFmpeg render.
  * Agent productions (AI videos, agent clips): a branded copy is made when the
    video is mirrored into ~/Documents/OwlCutVideos (cached in .backlot/branded/).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

from backlot.state import REPO_ROOT

log = logging.getLogger("owlcut.branding")

BRAND_DIR = REPO_ROOT / ".backlot" / "branding"
BRANDED_CACHE = REPO_ROOT / ".backlot" / "branded"
LOGO_MAX_BYTES = 15 * 1024 * 1024
LOGO_SIZE = 512

POSITIONS = {"top-left": "Top left", "top-center": "Top center", "top-right": "Top right"}
SIZES = {"small": 0.075, "medium": 0.10, "large": 0.13}        # logo diameter ÷ frame width
KINDS = {"video": "AI videos", "clips": "Clips", "ranking": "Ranking"}
SIDE_POSITIONS = {"left": "Left side", "right": "Right side"}
SIDE_HEIGHTS = {"upper": ("Upper", 0.30), "middle": ("Middle", 0.50), "lower": ("Lower", 0.62)}
SIDE_SIZES = {"small": 0.14, "medium": 0.20, "large": 0.28}     # longest side ÷ the frame's short side
SIDE_SHAPES = {"original": "Original shape", "round": "Round"}
FADES = {0.3: "Quick", 0.6: "Smooth", 1.0: "Slow"}
AREAS = {"picture": "On the video picture", "frame": "Whole screen"}   # where the round logo may move             # seconds of fade from / to black
# Positions are sliders: 0–100 % across / down the area a mark may use (the picture, minus a margin).
DEFAULTS: dict[str, Any] = {
    "handle": "", "pos_x": 0, "pos_y": 0, "pos_area": "picture", "size": "medium", "opacity": 85,
    "ring": True, "apply": {"video": True, "clips": True, "ranking": True},
    "side_enabled": True, "side_x": 0, "side_y": 50, "side_size": "medium",
    "side_opacity": 40, "side_shape": "original",
    "fade_enabled": True, "fade_duration": 0.6,
}

_FONTS = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _logo() -> Path:
    return BRAND_DIR / "logo.png"


def _side_logo() -> Path:
    return BRAND_DIR / "side_logo.png"


def clean_handle(raw: Any) -> str:
    h = "".join(str(raw or "").split())                 # no spaces at all
    h = h.lstrip("@")[:30]
    return f"@{h}" if h else ""


def get() -> dict[str, Any]:
    doc = json.loads(json.dumps(DEFAULTS))
    try:
        saved = json.loads((BRAND_DIR / "branding.json").read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            doc.update({k: v for k, v in saved.items() if k in DEFAULTS and k != "apply"})
            if isinstance(saved.get("apply"), dict):
                doc["apply"].update({k: bool(v) for k, v in saved["apply"].items() if k in KINDS})
            # settings saved before the sliders: fixed choices → slider values
            if "pos_x" not in saved and saved.get("position") in POSITIONS:
                doc["pos_x"] = {"top-left": 0, "top-center": 50, "top-right": 100}[saved["position"]]
            if "side_x" not in saved and saved.get("side_position") in SIDE_POSITIONS:
                doc["side_x"] = 0 if saved["side_position"] == "left" else 100
            if "side_y" not in saved and saved.get("side_height") in SIDE_HEIGHTS:
                doc["side_y"] = {"upper": 20, "middle": 50, "lower": 70}[saved["side_height"]]
    except (OSError, ValueError):
        pass
    logo, side = _logo(), _side_logo()
    doc["has_logo"] = logo.is_file()
    doc["logo_version"] = int(logo.stat().st_mtime) if logo.is_file() else 0
    doc["has_side_logo"] = side.is_file()
    doc["side_logo_version"] = int(side.stat().st_mtime) if side.is_file() else 0
    return doc


def clean(body: dict[str, Any], cur: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """``cur`` settings with the valid fields of ``body`` applied (nothing is written)."""
    cur = cur or get()
    out = {k: cur[k] for k in DEFAULTS}
    if "handle" in body:
        out["handle"] = clean_handle(body["handle"])
    for key in ("pos_x", "pos_y", "side_x", "side_y"):
        if key in body:
            try:
                out[key] = max(0, min(100, round(float(body[key]))))
            except (TypeError, ValueError):
                pass
    if body.get("size") in SIZES:
        out["size"] = body["size"]
    if "opacity" in body:
        try:
            out["opacity"] = max(20, min(100, int(body["opacity"])))
        except (TypeError, ValueError):
            pass
    if "ring" in body:
        out["ring"] = bool(body["ring"])
    if isinstance(body.get("apply"), dict):
        out["apply"] = {k: bool(body["apply"].get(k, cur["apply"][k])) for k in KINDS}
    if "side_enabled" in body:
        out["side_enabled"] = bool(body["side_enabled"])
    for key, allowed in (("side_size", SIDE_SIZES), ("side_shape", SIDE_SHAPES), ("pos_area", AREAS)):
        if body.get(key) in allowed:
            out[key] = body[key]
    if "fade_enabled" in body:
        out["fade_enabled"] = bool(body["fade_enabled"])
    try:
        if "fade_duration" in body and float(body["fade_duration"]) in FADES:
            out["fade_duration"] = float(body["fade_duration"])
    except (TypeError, ValueError):
        pass
    if "side_opacity" in body:
        try:
            out["side_opacity"] = max(10, min(100, int(body["side_opacity"])))
        except (TypeError, ValueError):
            pass
    return out


def save(body: dict[str, Any]) -> dict[str, Any]:
    out = clean(body)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BRAND_DIR / "branding.json.tmp"
    tmp.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(BRAND_DIR / "branding.json")
    return get()


def options() -> dict[str, Any]:
    return {"sizes": list(SIZES), "kinds": KINDS, "areas": AREAS,
            "side_sizes": list(SIDE_SIZES), "side_shapes": SIDE_SHAPES,
            "fades": [{"value": k, "label": v} for k, v in FADES.items()]}


def fade_for(kind: str, duration: float) -> float:
    """Seconds of fade from/to black for a ``kind`` video of ``duration`` s (0 = none). Clips only."""
    doc = get()
    if kind != "clips" or not doc["fade_enabled"] or duration <= 0:
        return 0.0
    return round(min(float(doc["fade_duration"]), duration / 4), 3)


def fade_filters(d: float, duration: float) -> tuple[str, str]:
    """(video, audio) filter snippets for a fade from black at the start and to black at the end."""
    out_at = max(0.0, duration - d)
    return (f"fade=t=in:st=0:d={d:.3f}:color=black,fade=t=out:st={out_at:.3f}:d={d:.3f}:color=black",
            f"afade=t=in:st=0:d={d:.3f},afade=t=out:st={out_at:.3f}:d={d:.3f}")


def _has_side(doc: dict[str, Any]) -> bool:
    return bool(doc["side_enabled"] and doc["has_side_logo"])


def active(kind: str) -> bool:
    doc = get()
    return bool(doc["apply"].get(kind)) and bool(doc["has_logo"] or doc["handle"] or _has_side(doc))


# ---------------------------------------------------------------------------
# Logo: square crop → circle with an anti-aliased edge
# ---------------------------------------------------------------------------

def _read_image(data: bytes):
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise ValueError("The file is empty.")
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError("The image is larger than 15 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("That file isn't an image OwlCut can read (use PNG, JPG or WebP).")
    fmt = (img.format or "png").lower()
    return ImageOps.exif_transpose(img).convert("RGBA"), ("jpg" if fmt == "jpeg" else fmt)


def set_side_logo(data: bytes) -> dict[str, Any]:
    """The second watermark: keeps its shape and transparency, trimmed to its visible pixels."""
    from PIL import Image

    img, fmt = _read_image(data)
    box = img.getchannel("A").getbbox()
    if not box:
        raise ValueError("The image is fully transparent.")
    img = img.crop(box)
    img.thumbnail((1024, 1024), Image.LANCZOS)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for old in BRAND_DIR.glob("side_logo_source.*"):
        old.unlink()
    (BRAND_DIR / f"side_logo_source.{fmt}").write_bytes(data)
    tmp = BRAND_DIR / "side_logo.tmp.png"
    img.save(tmp, "PNG")
    tmp.replace(_side_logo())
    return get()


def remove_side_logo() -> dict[str, Any]:
    for p in list(BRAND_DIR.glob("side_logo_source.*")) + [_side_logo()]:
        p.unlink(missing_ok=True)
    return get()


def set_logo(data: bytes) -> dict[str, Any]:
    from PIL import Image, ImageOps, UnidentifiedImageError

    if not data:
        raise ValueError("The file is empty.")
    if len(data) > LOGO_MAX_BYTES:
        raise ValueError("The image is larger than 15 MB.")
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        raise ValueError("That file isn't an image OwlCut can read (use PNG, JPG or WebP).")
    img = ImageOps.exif_transpose(img).convert("RGBA")
    side = min(img.size)
    left, top = (img.width - side) // 2, (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)

    # transparent areas of a PNG logo sit on white, so the circle is solid
    base = Image.new("RGBA", img.size, (255, 255, 255, 255))
    base.alpha_composite(img)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    fmt = (Image.open(io.BytesIO(data)).format or "png").lower()
    for old in BRAND_DIR.glob("logo_source.*"):
        old.unlink()
    (BRAND_DIR / f"logo_source.{'jpg' if fmt == 'jpeg' else fmt}").write_bytes(data)
    tmp = BRAND_DIR / "logo.tmp.png"
    base.convert("RGB").save(tmp, "PNG")
    tmp.replace(_logo())
    return get()


def remove_logo() -> dict[str, Any]:
    for p in list(BRAND_DIR.glob("logo_source.*")) + [_logo()]:
        p.unlink(missing_ok=True)
    return get()


def _circle(img, diameter: int, ring: bool):
    """The square logo as an anti-aliased circle (optionally with a white ring)."""
    from PIL import Image, ImageDraw

    ss = 4
    big = diameter * ss
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.LANCZOS)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    if ring:
        border = max(2, round(diameter * 0.045))
        out.paste(Image.new("RGBA", (diameter, diameter), (255, 255, 255, 255)), (0, 0), mask)
        inner = diameter - 2 * border
        logo = img.convert("RGBA").resize((inner, inner), Image.LANCZOS)
        imask = Image.new("L", (inner * ss, inner * ss), 0)
        ImageDraw.Draw(imask).ellipse((0, 0, inner * ss - 1, inner * ss - 1), fill=255)
        out.paste(logo, (border, border), imask.resize((inner, inner), Image.LANCZOS))
    else:
        out.paste(img.convert("RGBA").resize((diameter, diameter), Image.LANCZOS), (0, 0), mask)
    return out


def side_logo_png(size: int = 256, shape: Optional[str] = None) -> bytes:
    """The side watermark alone (for the settings page), in the chosen shape, full opacity."""
    if not _side_logo().is_file():
        raise KeyError("no side logo")
    doc = {**get(), "side_enabled": True, "side_opacity": 100, "side_size": "medium"}
    if shape in SIDE_SHAPES:
        doc["side_shape"] = shape
    size = max(32, min(1024, size))
    img = render_side(round(size / SIDE_SIZES["medium"]), round(size / SIDE_SIZES["medium"]), doc)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def round_logo_png(size: int = 256, ring: Optional[bool] = None) -> bytes:
    """The circle logo alone (for the settings page)."""
    from PIL import Image

    if not _logo().is_file():
        raise KeyError("no logo")
    buf = io.BytesIO()
    _circle(Image.open(_logo()), max(32, min(1024, size)), get()["ring"] if ring is None else ring).save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Badge (logo + handle) and where it goes
# ---------------------------------------------------------------------------

def _font(px: int):
    from PIL import ImageFont

    for f in _FONTS:
        if Path(f).is_file():
            return ImageFont.truetype(f, px)
    return ImageFont.load_default()


def render_badge(frame_w: int, doc: Optional[dict[str, Any]] = None):
    """The watermark as a transparent RGBA image for a frame ``frame_w`` px wide, or None."""
    from PIL import Image, ImageDraw, ImageFilter

    doc = doc or get()
    has_logo = doc["has_logo"]
    handle = doc["handle"]
    if not (has_logo or handle):
        return None
    d = max(24, round(frame_w * SIZES[doc["size"]]))
    pad = max(6, d // 6)                                 # room for the shadow
    gap = max(6, d // 6)
    font = _font(max(14, round(d * 0.36)))
    text_w = text_h = 0
    if handle:
        l, t, r, b = font.getbbox(handle)
        text_w, text_h = r - l, b - t
    right = doc["pos_x"] > 50                            # text sits on the inner side of the logo
    w = (d if has_logo else 0) + (gap if has_logo and handle else 0) + text_w + 2 * pad
    h = max(d if has_logo else 0, text_h) + 2 * pad
    fg = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(fg)

    # text after the logo, or before it when the badge hugs the right edge
    if has_logo:
        lx = w - pad - d if right and handle else pad
        fg.alpha_composite(_circle(Image.open(_logo()), d, doc["ring"]), (lx, (h - d) // 2))
    if handle:
        tx = pad if right or not has_logo else pad + d + gap
        l, t, _, _ = font.getbbox(handle)
        draw.text((tx - l, (h - text_h) // 2 - t), handle, font=font, fill=(255, 255, 255, 255),
                  stroke_width=max(1, d // 40), stroke_fill=(0, 0, 0, 90))

    # soft drop shadow under everything, so it reads on light and dark footage
    alpha = fg.getchannel("A")
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.55)).filter(ImageFilter.GaussianBlur(max(2, d // 14))))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(shadow, (0, max(1, d // 30)))
    out.alpha_composite(fg)
    if doc["opacity"] < 100:
        a = out.getchannel("A").point(lambda v: int(v * doc["opacity"] / 100))
        out.putalpha(a)
    return out


Box = Optional[tuple[int, int, int, int]]   # (x, y, w, h) of the real picture inside the frame


def _inner(frame_w: int, frame_h: int, box: Box) -> Box:
    """``box`` when the picture doesn't fill the frame (letterbox / blurred background), else None."""
    if not box:
        return None
    x, y, w, h = box
    return None if (x, y, w, h) == (0, 0, frame_w, frame_h) else (int(x), int(y), int(w), int(h))


def render_side(frame_w: int, frame_h: int, doc: dict[str, Any], box: Box = None):
    """The side watermark sized for the frame (opacity applied), or None."""
    from PIL import Image

    if not _has_side(doc):
        return None
    img = Image.open(_side_logo()).convert("RGBA")
    basis = min(frame_w, frame_h)
    box = _inner(frame_w, frame_h, box)
    if box:                                              # a wide picture: size from the picture
        basis = min(basis, box[2], round(box[3] * 1.5))
    longest = max(16, round(basis * SIDE_SIZES[doc["side_size"]]))
    if doc["side_shape"] == "round":
        side = min(img.size)
        left, top = (img.width - side) // 2, (img.height - side) // 2
        img = _circle(img.crop((left, top, left + side, top + side)), longest, False)
    else:
        scale = longest / max(img.size)
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    if doc["side_opacity"] < 100:
        img.putalpha(img.getchannel("A").point(lambda v: int(v * doc["side_opacity"] / 100)))
    return img


def area(frame_w: int, frame_h: int, top_reserved: int = 0, box: Box = None) -> tuple[int, int, int, int]:
    """(x, y, w, h) a mark may move in: the picture minus a margin. On a full vertical frame the
    margins keep clear of the platforms' top bar and bottom caption/buttons."""
    box = _inner(frame_w, frame_h, box)
    if box:
        bx, by, bw, bh = box
        mx, my = max(10, round(bw * 0.035)), max(10, round(bh * 0.05))
        return bx + mx, by + my, bw - 2 * mx, bh - 2 * my
    vertical = frame_h > frame_w
    mx = round(frame_w * (0.05 if vertical else 0.03))
    top = round(frame_h * (0.075 if vertical else 0.045))
    bottom = round(frame_h * (0.12 if vertical else 0.045))
    if top_reserved:
        top = top_reserved + round(frame_h * 0.015)
    return mx, top, frame_w - 2 * mx, max(1, frame_h - top - bottom)


def place_xy(frame_w: int, frame_h: int, w: int, h: int, px: float, py: float, top_reserved: int = 0,
             box: Box = None) -> tuple[int, int]:
    """Top-left corner of a ``w``×``h`` mark at slider position (px %, py %)."""
    ax, ay, aw, ah = area(frame_w, frame_h, top_reserved, box)
    x = ax + round(max(0, aw - w) * px / 100)
    y = ay + round(max(0, ah - h) * py / 100)
    return max(0, min(frame_w - w, x)), max(0, min(frame_h - h, y))


def place_side(frame_w: int, frame_h: int, w: int, h: int, doc: dict[str, Any], top_reserved: int = 0,
               box: Box = None) -> tuple[int, int]:
    return place_xy(frame_w, frame_h, w, h, doc["side_x"], doc["side_y"], top_reserved, box)


def render_overlay(frame_w: int, frame_h: int, doc: Optional[dict[str, Any]] = None, top_reserved: int = 0,
                   box: Box = None):
    """Everything (top badge + side watermark) on one transparent frame-sized image, or None.

    ``box``: where the real picture sits (blurred-background layouts) — marks stay on it."""
    from PIL import Image

    doc = doc or get()
    badge = render_badge(frame_w, doc)
    side = render_side(frame_w, frame_h, doc, box)
    if badge is None and side is None:
        return None
    canvas = Image.new("RGBA", (frame_w, frame_h), (0, 0, 0, 0))
    if side is not None:
        canvas.alpha_composite(side, place_side(frame_w, frame_h, side.width, side.height, doc, top_reserved, box))
    if badge is not None:
        whole = doc.get("pos_area") == "frame"           # the whole screen, blurred band and title band included
        canvas.alpha_composite(badge, place_xy(frame_w, frame_h, badge.width, badge.height, doc["pos_x"], doc["pos_y"],
                                               0 if whole else top_reserved, None if whole else box))
    return canvas


def place(frame_w: int, frame_h: int, badge_w: int, badge_h: int, position: str, top_reserved: int = 0,
          box: Box = None) -> tuple[int, int]:
    """Old fixed choices (top-left / top-center / top-right) on the slider placement."""
    px = {"top-left": 0, "top-center": 50, "top-right": 100}.get(position, 0)
    return place_xy(frame_w, frame_h, badge_w, badge_h, px, 0, top_reserved, box)


def badge_for(kind: str, frame_w: int, frame_h: int, workdir: Path, top_reserved: int = 0, box: Box = None
              ) -> Optional[tuple[Path, int, int]]:
    """(png path, x, y) to overlay on a ``frame_w``×``frame_h`` render of ``kind``, or None.

    The png is frame-sized (badge and side watermark already in place), so x = y = 0."""
    try:
        if not active(kind):
            return None
        img = render_overlay(frame_w, frame_h, get(), top_reserved, box)
        if img is None:
            return None
        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "watermark.png"
        img.save(path, "PNG")
        return path, 0, 0
    except Exception as exc:  # a bad logo must not stop a render
        log.warning("watermark unavailable: %s", exc)
        return None


_SCENES: dict[tuple, tuple] = {}                         # preview backgrounds, newest last
DEMOS = {"demo-wide": "Example · wide video (blurred background)", "demo-tall": "Example · vertical video (full screen)"}


def _demo_picture(w: int, h: int):
    """A neutral stand-in for footage: sky, sun, hills, a road."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(70 + 150 * t), int(120 + 80 * t), int(200 - 40 * t)))
    d.ellipse((w * 0.68, h * 0.12, w * 0.68 + h * 0.22, h * 0.34), fill=(255, 226, 150))
    d.polygon([(0, h * 0.62), (w * 0.3, h * 0.45), (w * 0.62, h * 0.6), (w, h * 0.48), (w, h), (0, h)], fill=(58, 104, 72))
    d.polygon([(w * 0.42, h), (w * 0.49, h * 0.6), (w * 0.53, h * 0.6), (w * 0.66, h)], fill=(70, 70, 76))
    return img


def list_samples() -> list[dict[str, str]]:
    """Footage the preview can use: two examples, then frames of the user's recent clips."""
    out = [{"id": k, "label": v} for k, v in DEMOS.items()]
    try:
        from backlot import quickclips
        for q in quickclips.list_all()[:8]:
            for c in quickclips.get(q["id"]).get("clips", [])[:3]:
                out.append({"id": f"quick:{q['id']}:{c['id']}", "label": f"{q.get('title') or q['id']} · {c.get('title') or c['id']}"[:80]})
    except Exception as exc:
        log.warning("preview samples: %s", exc)
    return out[:20]


def _sample_frame(sample: str, t: float):
    """(PIL image, source w, source h) for a sample id, or None."""
    from PIL import Image

    if sample in DEMOS:
        return _demo_picture(1920, 1080) if sample == "demo-wide" else _demo_picture(1080, 1920)
    if not sample.startswith("quick:"):
        return None
    from backlot import quickclips
    _, qid, cid = (sample.split(":") + ["", ""])[:3]
    doc = quickclips.get(qid)
    clip = next((c for c in doc.get("clips") or [] if c["id"] == cid), None)
    src = quickclips._dir(qid) / (doc.get("source") or {}).get("path", "")
    if not clip or not src.is_file():
        return None
    span = max(0.5, clip["end"] - clip["start"])
    at = clip["start"] + (t % 1.0) * span * 0.9 + span * 0.05
    res = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                          "-vf", "scale='min(1920,iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                         capture_output=True, timeout=30)
    if res.returncode != 0 or not res.stdout:
        return None
    return Image.open(io.BytesIO(res.stdout)).convert("RGB")


def _scene(kind: str, pic):
    """Lay ``pic`` out the way that kind of render does: (frame RGBA, picture box or None, reserved top)."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

    def cover(img, w, h):
        s = max(w / img.width, h / img.height)
        img = img.resize((max(w, round(img.width * s)), max(h, round(img.height * s))), Image.LANCZOS)
        x, y = (img.width - w) // 2, (img.height - h) // 2
        return img.crop((x, y, x + w, y + h))

    def blurred(img, w, h):
        bg = cover(img, w, h).filter(ImageFilter.GaussianBlur(40))
        return ImageEnhance.Brightness(ImageEnhance.Color(bg).enhance(0.85)).enhance(0.8)

    if kind == "video":
        return cover(pic, 1920, 1080).convert("RGBA"), None, 0
    from backlot import quickclips, ranking
    W, H = quickclips.W, quickclips.H
    wide = pic.width / pic.height >= 0.9
    if kind == "ranking":
        if wide:
            fh = min(ranking.VIDEO_MAX_H, int(round(W * pic.height / pic.width / 2)) * 2)
            fw = min(W, int(round(fh * pic.width / pic.height / 2)) * 2)
            frame = blurred(pic, W, H)
            box = ((W - fw) // 2, ranking.VIDEO_Y, fw, fh)
            frame.paste(pic.resize((fw, fh), Image.LANCZOS), box[:2])
        else:
            frame, box = cover(pic, W, H), None
        frame = frame.convert("RGBA")
        band = Image.new("RGBA", (W, ranking.HEADER_H), (11, 19, 43, 230))
        frame.alpha_composite(band)
        return frame, box, ranking.HEADER_H
    if not wide:
        return cover(pic, W, H).convert("RGBA"), None, 0
    box = quickclips.fit_box(pic.width, pic.height)
    frame = blurred(pic, W, H)
    frame.paste(pic.resize(box[2:], Image.LANCZOS), box[:2])
    return frame.convert("RGBA"), box, 0


def preview_png(kind: str = "clips", width: int = 432, draft: Optional[dict[str, Any]] = None,
                sample: str = "demo-wide", t: float = 0.35) -> bytes:
    """A frame laid out like the real render of ``kind`` — from an example or one of the user's clips —
    with the watermark exactly where renders put it. ``draft``: unsaved settings from the page."""
    from PIL import Image

    doc = get()
    if draft:
        doc = {**doc, **clean(draft, doc)}
    key = (kind, sample or "demo-wide", round(t, 2))
    cached = _SCENES.get(key)
    if cached is None:                                   # the footage layout is slow; only the marks change while dragging
        try:
            pic = _sample_frame(sample or "demo-wide", t)
        except Exception as exc:
            log.warning("preview sample %s: %s", sample, exc)
            pic = None
        if pic is None:
            pic = _demo_picture(1920, 1080)
        cached = _scene(kind, pic)
        _SCENES[key] = cached
        while len(_SCENES) > 12:
            _SCENES.pop(next(iter(_SCENES)))
    frame, box, reserved = cached
    frame = frame.copy()
    over = render_overlay(frame.width, frame.height, doc, reserved, box)
    if over is not None:
        frame.alpha_composite(over)
    frame = frame.convert("RGB").resize((width, round(frame.height * width / frame.width)), Image.LANCZOS)
    buf = io.BytesIO()
    frame.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Branded copies of finished agent videos
# ---------------------------------------------------------------------------

def _state_key(kind: str) -> str:
    doc = get()
    parts = [kind, doc["logo_version"], doc["side_logo_version"]] + [doc[k] for k in DEFAULTS if k != "apply"]
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()[:12]


def _probe_wh(src: Path) -> Optional[tuple[int, int]]:
    res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height", "-of", "json", str(src)], capture_output=True, text=True, timeout=60)
    try:
        s = json.loads(res.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except (ValueError, KeyError, IndexError):
        return None


def _probe_duration(src: Path) -> float:
    res = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return 0.0


def branded_copy(src: Path, kind: str) -> Path:
    """``src`` finished for delivery (cached): watermark burned in and, for clips, the fade from/to
    black. Returns ``src`` when there's nothing to add or it fails."""
    if src.suffix.lower() not in {".mp4", ".mov", ".m4v"} or not shutil.which("ffmpeg"):
        return src
    if not active(kind) and not (kind == "clips" and get()["fade_enabled"]):
        return src
    try:
        st = src.stat()
        src_key = hashlib.sha1(str(src.resolve()).encode()).hexdigest()[:12]
        ver = hashlib.sha1(f"{st.st_size}:{st.st_mtime_ns}:{_state_key(kind)}".encode()).hexdigest()[:10]
        out = BRANDED_CACHE / f"{src_key}_{ver}{src.suffix.lower()}"
        if out.is_file():
            return out
        with _lock:
            if out.is_file():
                return out
            wh = _probe_wh(src)
            if not wh:
                return src
            work = BRANDED_CACHE / f".{src_key}"
            badge = badge_for(kind, wh[0], wh[1], work)
            dur = _probe_duration(src)
            fade = fade_for(kind, dur)
            if not badge and not fade:
                return src
            inputs, steps, audio = ["-i", str(src)], [], ["-c:a", "copy"]
            if badge:
                png, x, y = badge
                inputs += ["-i", str(png)]
                steps.append(f"overlay={x}:{y}:format=auto")
            if fade:                                     # after the overlay: everything fades together
                vfade, afade = fade_filters(fade, dur)
                steps.append(vfade)
                audio = ["-af", afade, "-c:a", "aac", "-b:a", "192k"]
            graph = f"{'[0:v][1:v]' if badge else '[0:v]'}{','.join(steps + ['format=yuv420p'])}[v]"
            BRANDED_CACHE.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.stem + ".tmp" + out.suffix)
            res = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", graph,
                 "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                 *audio, "-movflags", "+faststart", str(tmp)],
                capture_output=True, text=True, timeout=3600)
            shutil.rmtree(work, ignore_errors=True)
            if res.returncode != 0:
                tmp.unlink(missing_ok=True)
                log.warning("branding %s failed: %s", src, res.stderr[-300:])
                return src
            for old in BRANDED_CACHE.glob(f"{src_key}_*"):   # older versions of the same video
                if old != tmp:
                    old.unlink(missing_ok=True)
            tmp.replace(out)
            return out
    except Exception as exc:
        log.warning("branding %s failed: %s", src, exc)
        return src
