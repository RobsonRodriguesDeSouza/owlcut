"""Ranking videos: document cleaning, validation, order and local rendering."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from backlot import ranking


@pytest.fixture(autouse=True)
def _tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ranking, "RANKINGS_DIR", tmp_path / "rankings")


def _entry(vid, start=0, end=5, name=""):
    return {"url": f"https://youtu.be/{vid}", "start": start, "end": end, "name": name}


def test_create_and_get():
    doc = ranking.create("Top 6 goals")
    assert doc["id"].startswith("top-6-goals-")
    assert len(doc["entries"]) == 6
    got = ranking.get(doc["id"])
    assert got["title"] == "Top 6 goals" and got["output"] is None


def test_get_rejects_bad_ids():
    for bad in ("../etc", "", "A B", "x/y"):
        with pytest.raises(KeyError):
            ranking.get(bad)


def test_save_cleans_entries_and_options():
    rid = ranking.create("T")["id"]
    doc = ranking.save(rid, {
        "title": "  Best  ", "accent": "#bad", "order": "top",
        "entries": [_entry("AAAAAAAAAAA", 1, 4, "x" * 99), {"url": "https://example.com"},
                    {"url": "", "thumbnail": "javascript:alert(1)"}],
    })
    assert doc["title"] == "Best"
    assert doc["accent"] == ranking.ACCENTS[0]
    assert doc["order"] == "top"
    e = doc["entries"][0]
    assert e["video_id"] == "AAAAAAAAAAA" and len(e["name"]) == ranking.MAX_NAME
    assert doc["entries"][1]["video_id"] == ""
    assert doc["entries"][2]["thumbnail"] == ""


def test_save_caps_entries():
    rid = ranking.create("T")["id"]
    doc = ranking.save(rid, {"entries": [_entry("AAAAAAAAAAA")] * 20})
    assert len(doc["entries"]) == ranking.MAX_ENTRIES


def test_problems():
    rid = ranking.create("T")["id"]
    doc = ranking.get(rid)
    assert any("at least" in p for p in ranking.problems(doc))
    doc = ranking.save(rid, {"entries": [
        _entry("AAAAAAAAAAA", 0, 5), _entry("BBBBBBBBBBB", 3, 3.5),
        _entry("CCCCCCCCCCC", 0, 500), {"url": "https://youtu.be/DDDDDDDDDDD"},
        {"url": "https://example.com/x"},
    ]})
    probs = " ".join(ranking.problems(doc))
    assert "Slot 2" in probs and "at least" in probs
    assert "Slot 3" in probs and "at most" in probs
    assert "Slot 4: set the start" in probs
    assert "Slot 5: this is not a YouTube" in probs
    doc = ranking.save(rid, {"title": "", "entries": [_entry("AAAAAAAAAAA"), _entry("BBBBBBBBBBB")]})
    assert ranking.problems(doc) == ["Add a header title."]


def test_order():
    doc = {"order": "countdown", "entries": [
        {"video_id": "a"}, {"video_id": ""}, {"video_id": "b"}, {"video_id": "c"}]}
    assert [(r, e["video_id"]) for r, e in ranking.ordered_entries(doc)] == [(3, "c"), (2, "b"), (1, "a")]
    doc["order"] = "top"
    assert [r for r, _ in ranking.ordered_entries(doc)] == [1, 2, 3]


def test_fit_text_shrinks_then_truncates():
    lines, px = ranking._fit_text("Short", [(84, 19), (62, 27)], 3)
    assert lines == ["Short"] and px == 84
    lines, px = ranking._fit_text("word " * 60, [(84, 19), (62, 27)], 3)
    assert px == 62 and len(lines) == 3 and lines[-1].endswith("…")


def test_render_refuses_when_not_ready():
    rid = ranking.create("T")["id"]
    with pytest.raises(ValueError):
        ranking.start_render(rid)


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not on PATH")
def test_render_entry_landscape_and_vertical(tmp_path):
    probe = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    if " drawtext " not in probe.stdout:
        pytest.skip("ffmpeg without drawtext")
    font = ranking._font()
    for name, size, audio in (("land", "640x360", True), ("vert", "360x640", False)):
        src = tmp_path / f"{name}.mp4"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc2=s={size}:r=30:d=1.5"]
        if audio:
            cmd += ["-f", "lavfi", "-i", "sine=d=1.5", "-shortest"]
        subprocess.run(cmd + [str(src)], check=True)
        out = tmp_path / f"{name}_out.mp4"
        ranking._render_entry(src, out, rank=3, name="Ação 100% 'ok'", title="Top: 6 % test",
                              accent="#5BC0DE", font=font, workdir=tmp_path, tag=name)
        m = ranking._probe_media(out)
        assert (m["w"], m["h"]) == (ranking.W, ranking.H)
        assert m["audio"]
        assert 1.3 < m["duration"] < 1.7
