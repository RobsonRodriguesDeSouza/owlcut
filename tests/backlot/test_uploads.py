"""Local video uploads for /clips (backlot/uploads.py)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from backlot import clipper, uploads

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(uploads, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path


def _tiny_video(path, codec="libx264", seconds=2):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=duration={seconds}", "-shortest", "-c:v", codec, "-c:a", "aac", "-y", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_start_validates_name_and_size(roots):
    with pytest.raises(ValueError):
        uploads.start("notes.txt", 10)
    with pytest.raises(ValueError):
        uploads.start("video.mp4", 0)
    with pytest.raises(ValueError, match="disk space"):
        uploads.start("video.mp4", 10**18)
    up = uploads.start("../../evil/My Talk.MP4", 5)
    assert up["name"] == "My Talk.MP4" and up["received"] == 0 and up["status"] == "uploading"


def test_chunks_must_be_sequential_and_bounded(roots):
    uid = uploads.start("v.mp4", 6)["id"]
    assert uploads.append(uid, 0, [b"abc"])["received"] == 3
    with pytest.raises(RuntimeError, match="offset"):
        uploads.append(uid, 0, [b"abc"])            # retried chunk can't duplicate data
    with pytest.raises(ValueError):
        uploads.append(uid, 3, [b"defg"])           # past declared size
    assert uploads.status(uid)["received"] == 3
    assert uploads.append(uid, 3, [b"def"])["received"] == 6
    with pytest.raises(RuntimeError, match="incomplete"):
        uploads.finish(uploads.start("w.mp4", 9)["id"])


def test_unknown_or_malformed_ids(roots):
    for bad in ("nope", "../x", "0" * 32):
        with pytest.raises(KeyError):
            uploads.status(bad)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_full_flow_moves_file_into_project(roots):
    data = _tiny_video(roots / "src.mp4")
    uid = uploads.start("Minha palestra.mp4", len(data))["id"]
    half = len(data) // 2
    uploads.append(uid, 0, [data[:half]])
    uploads.append(uid, half, [data[half:]])
    done = uploads.finish(uid)
    assert done["status"] == "ready"
    assert done["info"]["width"] == 320 and done["info"]["has_audio"] and done["info"]["duration"] > 1
    meta = uploads.ready_info(uid)
    assert meta["title"] == "Minha palestra"

    src = uploads.claim(uid, "p1")
    assert src == {"original_path": "assets/video/source.mp4", "needs_conversion": False}
    assert (roots / "projects/p1/assets/video/source.mp4").read_bytes() == data
    with pytest.raises(ValueError):
        uploads.ready_info(uid)                     # can't be used by a second job
    with pytest.raises(RuntimeError):
        uploads.cancel(uid)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_non_browser_codec_is_flagged_for_conversion(roots):
    data = _tiny_video(roots / "src.mkv", codec="mpeg4")
    uid = uploads.start("clip.mkv", len(data))["id"]
    uploads.append(uid, 0, [data])
    uploads.finish(uid)
    assert uploads.claim(uid, "p2") == {"original_path": "assets/video/original.mkv", "needs_conversion": True}


def test_garbage_file_is_rejected(roots):
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    uid = uploads.start("fake.mp4", 4)["id"]
    uploads.append(uid, 0, [b"junk"])
    with pytest.raises(ValueError):
        uploads.finish(uid)


def test_prompt_for_local_file_skips_download():
    meta = {"local": True, "name": "talk.mov", "title": "talk", "size": 3 * 1024**3, "duration": 5400,
            "width": 1920, "height": 1080, "video_codec": "hevc", "audio_codec": "aac",
            "original_path": "assets/video/original.mov", "needs_conversion": True}
    prompt = clipper.build_prompt({"count": 5, "platforms": ["tiktok"]}, "p9", meta)
    assert "video_downloader" not in prompt and "transcript_fetcher" not in prompt
    assert "projects/p9/assets/video/original.mov" in prompt
    assert "projects/p9/assets/video/source.mp4" in prompt   # conversion target
    assert "1:30:00" in prompt and "3.00 GB" in prompt
    assert "20-minute segments" in prompt
