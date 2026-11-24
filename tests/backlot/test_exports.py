"""Finished videos mirrored into OwlCutVideos/<category>/<title>_<date-time>/ (backlot/exports.py)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from backlot import exports


def test_folder_layout_and_copies(tmp_path, monkeypatch):
    out = tmp_path / "OwlCutVideos"
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(out))
    src = tmp_path / "final.mp4"
    src.write_bytes(b"video")
    ts = 1_790_000_000.0
    folder = exports.publish("video", "project:x", 'Por que o céu é azul? / v2', ts, [(src, "final.mp4")])
    stamp = __import__("time").strftime("%Y-%m-%d_%H-%M", __import__("time").localtime(ts))
    assert folder == out / "AIvideos" / f"Por que o céu é azul v2_{stamp}"
    dest = folder / "final.mp4"
    assert dest.read_bytes() == b"video"
    assert os.stat(dest).st_ino != os.stat(src).st_ino            # its own file (a clone), not a hard link
    # same production -> same folder, even later
    assert exports.publish("video", "project:x", "renamed", ts + 9999, [(src, "final.mp4")]) == folder
    # a re-render that replaces the file gets re-linked
    src.unlink(); src.write_bytes(b"new video!")
    exports.publish("video", "project:x", "x", ts, [(src, "final.mp4")])
    assert dest.read_bytes() == b"new video!"


def test_categories_and_nothing_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(tmp_path / "o"))
    assert exports.publish("clips", "quick:a", "A", 0, [(tmp_path / "missing.mp4", "x.mp4")]) is None
    assert not (tmp_path / "o").exists()
    f = tmp_path / "r.mp4"; f.write_bytes(b"1")
    assert exports.publish("ranking", "ranking:r", "Top 6", 0, [(f, "Top 6.mp4")]).parent.name == "RankingVideos"
    assert exports.publish("clips", "quick:b", "Top 6", 0, [(f, "1.mp4")]).parent.name == "Clips"
    # two productions with the same name and minute get separate folders
    a = exports.publish("clips", "quick:c", "Same", 0, [(f, "1.mp4")])
    b = exports.publish("clips", "quick:d", "Same", 0, [(f, "1.mp4")])
    assert a != b and b.name.endswith("(2)")


def test_agent_clips_are_named_by_title(tmp_path):
    p = tmp_path / "proj"
    (p / "clips").mkdir(parents=True)
    (p / "renders" / "clips").mkdir(parents=True)
    (p / "clips" / "clips.json").write_text(json.dumps({"clips": [{"id": "c01", "title": "A roda"}]}))
    (p / "renders" / "clips" / "c01_9x16.mp4").write_bytes(b"v")
    (p / "renders" / "clips" / "c01_cover.jpg").write_bytes(b"j")
    names = sorted(n for _, n in exports.project_files(p, "clips"))
    assert names == ["c01 - A roda (9x16).mp4", "c01 - A roda (cover).jpg"]


def test_never_raises(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("not a dir")
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(blocker))       # root can't be created
    f = tmp_path / "v.mp4"; f.write_bytes(b"1")
    assert exports.publish("video", "k", "n", 0, [(f, "v.mp4")]) is None


def test_exports_are_visible_in_finder(tmp_path, monkeypatch):
    """App files live under .backlot, which macOS flags hidden; the export must not inherit that flag,
    and an old hard-linked export gets replaced by its own file."""
    import stat
    import sys
    import pytest
    if sys.platform != "darwin":
        pytest.skip("BSD file flags")
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(tmp_path / "out"))
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"clip")
    os.chflags(src, os.stat(src).st_flags | stat.UF_HIDDEN)
    folder = exports.publish("clips", "quick:q", "Q", 1_790_000_000, [(src, "a.mp4")])
    dest = folder / "a.mp4"
    assert not os.stat(dest).st_flags & stat.UF_HIDDEN
    assert os.stat(src).st_flags & stat.UF_HIDDEN                  # the app's own file is untouched
    dest.unlink(); os.link(src, dest)                               # export made by an older version
    exports.publish("clips", "quick:q", "Q", 1_790_000_000, [(src, "a.mp4")])
    assert os.stat(dest).st_ino != os.stat(src).st_ino and not os.stat(dest).st_flags & stat.UF_HIDDEN
    assert dest.read_bytes() == b"clip"
