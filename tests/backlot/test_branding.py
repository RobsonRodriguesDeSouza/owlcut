"""Watermark settings, round logo, badge placement and burn-in (backlot/branding.py)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess

import pytest
from PIL import Image

from backlot import branding

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _png(w=300, h=200, color=(200, 30, 30, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def test_settings_defaults_and_validation():
    s = branding.get()
    assert s["handle"] == "" and not s["has_logo"] and s["apply"] == {"video": True, "clips": True, "ranking": True}
    assert not branding.active("clips")                       # nothing to show yet
    s = branding.save({"handle": " my page ", "pos_x": 250, "pos_y": "x", "size": "huge", "opacity": 5,
                       "apply": {"ranking": False, "bogus": True}})
    assert s["handle"] == "@mypage" and s["pos_x"] == 100 and s["pos_y"] == 0 and s["size"] == "medium"
    assert s["opacity"] == 20 and s["apply"] == {"video": True, "clips": True, "ranking": False}
    assert branding.save({"handle": "@@x"})["handle"] == "@x"
    assert branding.active("clips") and not branding.active("ranking")


def test_logo_is_square_cropped_and_drawn_round():
    s = branding.set_logo(_png(300, 200))
    assert s["has_logo"]
    logo = Image.open(branding.BRAND_DIR / "logo.png")
    assert logo.size == (512, 512)
    rnd = Image.open(io.BytesIO(branding.round_logo_png(100)))
    assert rnd.mode == "RGBA" and rnd.getpixel((0, 0))[3] == 0          # corners transparent
    assert rnd.getpixel((50, 50))[:3] == (200, 30, 30)                   # centre is the logo
    with pytest.raises(ValueError):
        branding.set_logo(b"not an image")
    assert not branding.remove_logo()["has_logo"]


def test_badge_placement_respects_position_and_reserved_band():
    branding.save({"handle": "@mypage", "pos_x": 100})
    img = branding.render_badge(1080)
    assert img is not None and img.width < 1080
    x, y = branding.place(1080, 1920, img.width, img.height, "top-right")
    assert x + img.width <= 1080 - 50 and 100 < y < 200
    assert branding.place(1080, 1920, 100, 50, "top-left", top_reserved=360)[1] > 360
    assert branding.place(1080, 1920, 100, 50, "top-center")[0] == 490
    png = Image.open(io.BytesIO(branding.preview_png("ranking", 216)))
    assert png.size == (216, 384)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_branded_copy_burns_the_logo_in(tmp_path):
    src = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=640x360:d=1:r=10",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(src)], check=True)
    assert branding.branded_copy(src, "video") == src                   # nothing configured yet
    branding.set_logo(_png(64, 64, (255, 255, 255, 255)))
    branding.save({"opacity": 100, "pos_x": 0, "pos_y": 0})
    out = branding.branded_copy(src, "video")
    assert out != src and out.is_file()
    assert branding.branded_copy(src, "video") == out                   # cached
    frame = tmp_path / "f.png"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1", "-y", str(frame)], check=True)
    img = Image.open(frame).convert("RGB")
    d = round(640 * branding.SIZES["medium"])
    x, y = branding.place(640, 360, 0, 0, "top-left")
    assert sum(img.getpixel((x + d // 2 + 6, y + d // 2 + 6))) > 600     # white logo where it should be
    assert sum(img.getpixel((600, 300))) < 60                           # rest untouched
    branding.save({"apply": {"video": False}})
    assert branding.branded_copy(src, "video") == src


def test_settings_api(tmp_path):
    from fastapi.testclient import TestClient
    from backlot.server import create_app

    c = TestClient(create_app(), base_url="http://127.0.0.1:4750")
    h = {"origin": "http://127.0.0.1:4750"}
    assert c.get("/api/branding").json()["settings"]["handle"] == ""
    r = c.put("/api/branding", json={"handle": "owl"}, headers=h)
    assert r.status_code == 200 and r.json()["settings"]["handle"] == "@owl"
    assert c.put("/api/branding", json={"handle": "x"}).status_code == 403            # no origin
    assert c.put("/api/branding/logo", content=_png(), headers=h).status_code == 403  # no upload header
    r = c.put("/api/branding/logo", content=_png(), headers={**h, "x-owlcut-upload": "1"})
    assert r.status_code == 200 and r.json()["settings"]["has_logo"]
    assert c.get("/api/branding/logo.png").headers["content-type"] == "image/png"
    assert c.get("/api/branding/preview.png?kind=video").status_code == 200
    assert c.get("/settings").status_code == 200


def test_preview_uses_unsaved_draft_without_storing_it():
    a = branding.preview_png("clips", 216)
    b = branding.preview_png("clips", 216, {"handle": "@draft"})
    assert a != b                                                        # badge drawn from the draft
    assert branding.get()["handle"] == ""                               # ...but nothing was saved


def test_side_watermark_keeps_shape_and_sits_on_the_side():
    # a wide transparent PNG with a visible bar in the middle — gets trimmed to the bar
    img = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
    for x in range(100, 300):
        for y in range(80, 120):
            img.putpixel((x, y), (255, 0, 0, 255))
    buf = io.BytesIO(); img.save(buf, "PNG")
    s = branding.set_side_logo(buf.getvalue())
    assert s["has_side_logo"] and Image.open(branding.BRAND_DIR / "side_logo.png").size == (200, 40)
    assert branding.active("clips")                                     # side alone is enough
    doc = branding.save({"side_x": 100, "side_y": 50, "side_opacity": 100,
                         "side_size": "medium", "side_shape": "original"})
    over = branding.render_overlay(1080, 1920, doc)
    box = over.getchannel("A").getbbox()
    ax, ay, aw, ah = branding.area(1080, 1920)
    assert box[2] == ax + aw and box[0] > 700                           # right edge of the safe area
    assert abs((box[1] + box[3]) / 2 - (ay + ah / 2)) < 2               # vertically centred in it
    assert box[2] - box[0] == round(1080 * branding.SIDE_SIZES["medium"])  # longest side = 20% of 1080
    rnd = branding.render_side(1080, 1920, {**doc, "side_shape": "round"})
    assert rnd.width == rnd.height
    # ranking: never inside the title band
    top = branding.save({"side_y": 0})
    y = branding.place_side(1080, 1920, 100, 400, top, top_reserved=360)[1]
    assert y > 360
    assert not branding.save({"side_enabled": False})["side_enabled"]
    assert branding.render_overlay(1080, 1920) is None                  # nothing else configured
    assert not branding.remove_side_logo()["has_side_logo"]
    with pytest.raises(ValueError):
        branding.set_side_logo(_png(10, 10, (0, 0, 0, 0)))              # fully transparent


def _luma(path, t):
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=8:8,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    return sum(out) / max(1, len(out))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_clips_fade_from_and_to_black(tmp_path):
    src = tmp_path / "white.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=white:s=320x568:d=4:r=25",
                    "-f", "lavfi", "-i", "sine=d=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-y", str(src)], check=True)
    assert branding.fade_for("video", 4) == 0                            # AI videos: no fade
    assert branding.fade_for("clips", 4) == 0.6
    assert branding.fade_for("clips", 1.0) == 0.25                       # never more than a quarter
    out = branding.branded_copy(src, "clips")                            # fade only, no watermark set
    assert out != src
    assert _luma(out, 0.0) < 40                                          # starts dark
    assert _luma(out, 2.0) > 200                                         # full picture in the middle
    assert _luma(out, 3.95) < 60                                         # ends dark
    branding.save({"fade_enabled": False})
    assert branding.branded_copy(src, "clips") == src
    assert branding.save({"fade_duration": 7})["fade_duration"] == 0.6   # invalid value ignored


def test_marks_stay_on_the_picture_in_blurred_background_layouts():
    from backlot import quickclips
    branding.save({"handle": "@owl", "pos_x": 100, "pos_y": 100})       # bottom-right corner of the picture
    box = quickclips.fit_box(1920, 1080)                                 # 16:9 footage inside a 9:16 frame
    bx, by, bw, bh = box
    assert (bw, bh) == (1080, 608) and by > 500
    over = branding.render_overlay(1080, 1920, branding.get(), box=box)
    l, t, r, b = over.getchannel("A").getbbox()
    assert by <= t and b <= by + bh                                      # not on the blurred band
    # the side watermark too
    img = Image.new("RGBA", (300, 300), (255, 0, 0, 255))
    buf = io.BytesIO(); img.save(buf, "PNG"); branding.set_side_logo(buf.getvalue())
    doc = branding.save({"handle": "", "side_y": 0, "side_opacity": 100})
    l, t, r, b = branding.render_overlay(1080, 1920, doc, box=box).getchannel("A").getbbox()
    assert by <= t and b <= by + bh and l >= bx
    # a full-frame picture keeps the platform-safe top position
    assert branding.place(1080, 1920, 100, 50, "top-left", box=(0, 0, 1080, 1920)) == branding.place(1080, 1920, 100, 50, "top-left")


def test_preview_scenes_and_samples():
    branding.save({"handle": "@owl"})
    ids = [s["id"] for s in branding.list_samples()]
    assert ids[:2] == ["demo-wide", "demo-tall"]
    wide = Image.open(io.BytesIO(branding.preview_png("clips", 216, sample="demo-wide")))
    tall = Image.open(io.BytesIO(branding.preview_png("clips", 216, sample="demo-tall")))
    video = Image.open(io.BytesIO(branding.preview_png("video", 384, sample="demo-wide")))
    assert wide.size == tall.size == (216, 384) and video.size == (384, 216)
    assert wide.tobytes() != tall.tobytes()
    bogus = Image.open(io.BytesIO(branding.preview_png("ranking", 216, sample="quick:../x:q01")))
    assert bogus.size == (216, 384)                                      # unknown sample: falls back, no error


def test_sliders_move_marks_and_old_settings_migrate():
    branding.save({"handle": "@owl", "opacity": 100})
    def where(**kw):
        return branding.render_overlay(1080, 1920, {**branding.get(), **kw}).getchannel("A").getbbox()
    a, b = where(pos_x=0, pos_y=0), where(pos_x=100, pos_y=100)
    assert b[0] > a[0] + 500 and b[1] > a[1] + 1000                     # really moves across and down
    mid = where(pos_x=50, pos_y=50)
    assert a[0] < mid[0] < b[0] and a[1] < mid[1] < b[1]
    # settings written before the sliders existed
    branding.BRAND_DIR.mkdir(parents=True, exist_ok=True)
    (branding.BRAND_DIR / "branding.json").write_text(json.dumps(
        {"handle": "@old", "position": "top-right", "side_position": "left", "side_height": "lower"}))
    g = branding.get()
    assert (g["pos_x"], g["pos_y"], g["side_x"], g["side_y"]) == (100, 0, 0, 70)


def test_round_logo_can_use_the_whole_screen():
    from backlot import quickclips
    box = quickclips.fit_box(1920, 1080)
    doc = branding.save({"handle": "@owl", "opacity": 100, "pos_x": 0, "pos_y": 0, "pos_area": "frame"})
    t = branding.render_overlay(1080, 1920, doc, box=box).getchannel("A").getbbox()[1]
    assert t < box[1]                                                    # above the picture, on the blurred band
    doc = branding.save({"pos_y": 100})
    b = branding.render_overlay(1080, 1920, doc, box=box).getchannel("A").getbbox()[3]
    assert b > box[1] + box[3]                                           # below it
    doc = branding.save({"pos_area": "picture"})
    l, t, r, b = branding.render_overlay(1080, 1920, doc, box=box).getchannel("A").getbbox()
    assert box[1] <= t and b <= box[1] + box[3]                          # back on the picture
    assert branding.save({"pos_area": "nope"})["pos_area"] == "picture"
