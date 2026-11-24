"""Local quick clips (backlot/quickclips.py): planning, captions, end-to-end."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from backlot import quickclips as qc
from backlot import uploads

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _segments(n=40, each=6.0, gap=0.3):
    """n sentences of `each` seconds; every 3rd one is a question."""
    segs, words, t = [], [], 0.0
    for i in range(n):
        text = f"Sentence number {i} goes here" + ("?" if i % 3 == 0 else ".")
        segs.append({"start": t, "end": t + each, "text": text})
        toks = text.split()
        step = each / len(toks)
        for k, tok in enumerate(toks):
            words.append({"start": t + k * step, "end": t + (k + 1) * step - 0.05, "w": tok})
        t += each + gap
    return segs, words


def _opts(**kw):
    return qc._clean_form({"length": "short", "mode": "best", "count": 3, **kw})


def test_best_mode_picks_n_non_overlapping_sentence_windows():
    segs, words = _segments()
    rms = [-20.0] * 300
    out = qc.plan_clips(segs, words, rms, 300.0, _opts())
    assert len(out) == 3
    spans = sorted((c["start"], c["end"]) for c in out)
    assert all(a[1] <= b[0] for a, b in zip(spans, spans[1:]))          # no overlap
    for c in out:
        assert 15 <= c["end"] - c["start"] <= 31                         # within the range (+ pads)
        assert c["text"].endswith((".", "?"))                            # whole sentences
        assert 0 <= c["score"] <= 100 and c["title"]


def test_all_mode_covers_the_video_in_order():
    segs, words = _segments(n=30)
    out = qc.plan_clips(segs, words, [], 190.0, _opts(mode="all"))
    assert len(out) >= 6
    assert all(a["end"] <= b["start"] + 0.5 for a, b in zip(out, out[1:]))
    covered = sum(c["end"] - c["start"] for c in out)
    assert covered > 0.85 * (segs[-1]["end"] - segs[0]["start"])


def test_long_pause_starts_a_new_clip():
    segs, words = _segments(n=10)
    for s in segs[5:]:            # 20 s of silence before sentence 5
        s["start"] += 20
        s["end"] += 20
    out = qc.plan_clips(segs, words, [], 100.0, _opts(mode="all"))
    assert not any(c["start"] < segs[4]["end"] < segs[5]["start"] < c["end"] for c in out)


def test_no_speech_falls_back_to_even_parts():
    out = qc.plan_clips([], [], [], 120.0, _opts(length="short", mode="best", count=2))
    assert len(out) == 2 and all(c["title"].startswith("Part") for c in out)
    assert qc.plan_clips([], [], [], 0, _opts()) == []


def test_caption_chunks_and_ass():
    words = [{"start": i * 0.4, "end": i * 0.4 + 0.35, "w": w} for i, w in
             enumerate("Hello world, this is a {tricky} test of captions!".split())]
    chunks = qc.caption_chunks(words)
    assert [len(c) for c in chunks][0] == 2                              # breaks after the comma
    assert all(len(c) <= 3 for c in chunks)
    ass = qc.build_ass(words, "highlight", "#FFD400", 5.0)
    assert "PlayResY: 1920" in ass and "&H0000D4FF" in ass                # accent as &HBBGGRR
    assert "{tricky}" not in ass and "(TRICKY)" in ass                    # braces can't inject tags
    dialogue = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    assert len(dialogue) == len(words)                                    # one event per highlighted word
    clean = qc.build_ass(words, "clean", "#FFD400", 5.0)
    assert len([l for l in clean.splitlines() if l.startswith("Dialogue:")]) == len(chunks)


def test_clean_form_rejects_bad_values():
    o = qc._clean_form({"length": "x", "mode": "y", "count": "999", "framing": "z", "captions": "q", "accent": "red"})
    assert o == {"length": "medium", "mode": "best", "count": 20, "framing": "auto",
                 "captions": "highlight", "accent": qc.ACCENTS[0], "focus_start": None, "focus_end": None}


def test_ids_are_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(qc, "QUICK_DIR", tmp_path)
    for bad in ("../x", "A", ""):
        with pytest.raises(KeyError):
            qc.get(bad)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_end_to_end_without_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(qc, "QUICK_DIR", tmp_path / "quick")
    monkeypatch.setattr(uploads, "UPLOAD_DIR", tmp_path / "uploads")
    src = tmp_path / "wide.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=40",
                    "-c:v", "libx264", "-y", str(src)], check=True)
    data = src.read_bytes()
    uid = uploads.start("wide.mp4", len(data))["id"]
    uploads.append(uid, 0, [data])
    uploads.finish(uid)

    from backlot import branding
    branding.save({"handle": "@owl"})                                    # watermark overlay in the graph
    doc = qc.create({"upload_id": uid, "length": "short", "mode": "all", "captions": "highlight"})
    qid = doc["id"]
    deadline = time.time() + 120
    while time.time() < deadline and qc.get(qid)["status"] == "running":
        time.sleep(0.3)
    doc = qc.get(qid)
    assert doc["status"] == "done", doc.get("error")
    assert len(doc["clips"]) == 1 and doc["clips"][0]["title"] == "Part 1"   # 40 s, no audio → one 22.5 s part
    out = qc.clip_path(qid, "q01")
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
                                       "-of", "json", str(out)], capture_output=True, text=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert any(s["codec_type"] == "audio" for s in probe["streams"])     # silent track added
    first = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-frames:v", "1", "-vf", "scale=8:8,format=gray",
                            "-f", "rawvideo", "-"], capture_output=True).stdout
    assert sum(first) / len(first) < 20                                  # starts from black (fade in)
    assert qc.clip_path(qid, "q01", "thumb").is_file()
    assert qc.bundle_zip(qid).stat().st_size > 0
    with pytest.raises(ValueError):                                      # upload can't be reused
        qc.create({"upload_id": uid})
    qc.delete(qid)
    with pytest.raises(KeyError):
        qc.get(qid)


def test_focus_range_limits_where_clips_come_from():
    segs, words = _segments(n=40)
    opts = qc._clean_form({"length": "short", "mode": "all", "focus_start": "1:00", "focus_end": "2:00"})
    assert opts["focus_start"] == 60 and opts["focus_end"] == 120
    out = qc.plan_clips(segs, words, [], 260.0, opts)
    assert out and all(59 <= c["start"] and c["end"] <= 121 for c in out)
    assert qc._clean_form({"focus_start": "2:00", "focus_end": "1:00"})["focus_end"] is None


def test_delete_reports_and_removes(tmp_path, monkeypatch):
    monkeypatch.setattr(qc, "QUICK_DIR", tmp_path / "quick")
    d = tmp_path / "quick" / "abc-123"
    (d / "clips").mkdir(parents=True)
    (d / "quick.json").write_text(json.dumps({"id": "abc-123", "title": "T", "status": "cancelled", "clips": []}))
    (d / "clips" / "q01.mp4").write_bytes(b"z" * 300)
    assert qc.delete("abc-123", dry_run=True)["bytes"] >= 300 and d.is_dir()
    qc.delete("abc-123")
    assert not d.exists()


def test_zip_download_recreates_the_owlcutvideos_folder(tmp_path, monkeypatch):
    """The folder in OwlCutVideos was removed by hand: downloading the zip saves the clips there again."""
    import json as _json
    import shutil as _sh
    from fastapi.testclient import TestClient
    from backlot import exports, quickclips, server

    monkeypatch.setattr(quickclips, "QUICK_DIR", tmp_path / "quick")
    qid = "demo-abc123"
    d = tmp_path / "quick" / qid
    (d / "clips").mkdir(parents=True)
    (d / "clips" / "q01.mp4").write_bytes(b"clip")
    (d / "quick.json").write_text(_json.dumps({"id": qid, "status": "done", "title": "Demo", "created_at": 1_700_000_000,
                                                "clips": [{"id": "q01", "title": "Hello", "start": 0, "end": 5}]}))
    first = quickclips.export(qid)
    _sh.rmtree(first)
    client = TestClient(server.create_app(), base_url="http://127.0.0.1:4750")
    assert client.get(f"/api/quickclips/{qid}/saved").json()["folder"] is None
    assert client.get(f"/api/quickclips/{qid}/zip").status_code == 200
    saved = client.get(f"/api/quickclips/{qid}/saved").json()["folder"]
    assert saved and (Path(saved) / "01 - Hello.mp4").read_bytes() == b"clip"
    assert Path(saved).parent == exports.root() / "Clips"
