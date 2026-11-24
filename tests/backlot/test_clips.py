"""OwlCut /clips: link parsing, briefing, clips.json read side and publishing kit (no network)."""

from __future__ import annotations

import json

import pytest

from backlot import clipper


@pytest.mark.parametrize("url,vid,start", [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw", None),
    ("youtube.com/watch?v=jNQXAC9IVRw&t=90", "jNQXAC9IVRw", 90.0),
    ("https://youtu.be/jNQXAC9IVRw?t=1m5s", "jNQXAC9IVRw", 65.0),
    ("https://www.youtube.com/shorts/abcdefghijk", "abcdefghijk", None),
    ("https://m.youtube.com/watch?v=jNQXAC9IVRw&start=1h0m2s", "jNQXAC9IVRw", 3602.0),
])
def test_parse_link_accepts_youtube_forms(url, vid, start):
    link = clipper.parse_link(url)
    assert link["video_id"] == vid
    assert link["start"] == start
    assert link["url"] == f"https://www.youtube.com/watch?v={vid}"


@pytest.mark.parametrize("url", ["", "https://vimeo.com/1", "https://www.youtube.com/@canal",
                                 "https://www.youtube.com/playlist?list=PL123", "https://evil.example/watch?v=jNQXAC9IVRw"])
def test_parse_link_rejects_non_videos(url):
    with pytest.raises(ValueError):
        clipper.parse_link(url)


@pytest.mark.parametrize("text,seconds", [("83", 83.0), ("1:23", 83.0), ("01:02:03", 3723.0), ("", None), ("a:b", None), ("1:2:3:4", None)])
def test_parse_ts(text, seconds):
    assert clipper.parse_ts(text) == seconds


def test_build_prompt_carries_focus_platforms_and_contract():
    meta = {"url": "https://www.youtube.com/watch?v=x", "title": "T", "channel": "C", "duration": 1800,
            "start": 65.0, "captions": {"manual": ["pt"]}}
    prompt = clipper.build_prompt({"count": 3, "length": "short", "platforms": ["tiktok", "feed", "bogus"]}, "p1", meta)
    assert "clip-factory" in prompt
    assert "1:05" in prompt                       # focus from ?t=
    assert "4:5, 9:16" in prompt                  # both aspects, bogus platform dropped
    assert "projects/p1/clips/clips.json" in prompt
    assert "max_duration_seconds 1920" in prompt  # long sources are not rejected by the 600 s default


@pytest.fixture
def projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(clipper, "PROJECTS_DIR", root)
    return root


def _write(root, data):
    d = root / "p1" / "clips"
    d.mkdir(parents=True)
    (d / "clips.json").write_text(json.dumps(data), encoding="utf-8")
    return root / "p1"


def test_load_clips_normalises_and_guards_paths(projects_root):
    proj = _write(projects_root, {
        "source": {"video_path": "assets/video/source.mp4", "duration_seconds": 100},
        "status": "awaiting_selection",
        "clips": [
            {"id": "c01", "title": "ok", "start": 1, "end": 10, "score": 90,
             "renders": {"9:16": "renders/clips/c01_9x16.mp4", "4:5": "../../etc/passwd", "1:1": "/abs/path.mp4"}},
            {"id": "bad", "start": 10, "end": 5},          # end before start → dropped
            {"id": "c03", "start": "x", "end": 4},          # non-numeric → dropped
            "not a dict",
        ],
    })
    (proj / "assets/video").mkdir(parents=True)
    (proj / "assets/video/source.mp4").write_bytes(b"0")
    (proj / "renders/clips").mkdir(parents=True)
    (proj / "renders/clips/c01_9x16.mp4").write_bytes(b"0")

    out = clipper.load_clips("p1")
    assert out["status"] == "awaiting_selection"
    assert out["source"]["video_path"] == "assets/video/source.mp4"
    assert [c["id"] for c in out["clips"]] == ["c01"]
    assert out["clips"][0]["duration"] == 9
    assert out["clips"][0]["renders"] == {"9:16": "renders/clips/c01_9x16.mp4"}  # traversal and absolute dropped


def test_load_clips_missing_and_corrupt(projects_root):
    assert clipper.load_clips("nope") is None
    d = projects_root / "p1" / "clips"
    d.mkdir(parents=True)
    (d / "clips.json").write_text("{half", encoding="utf-8")
    out = clipper.load_clips("p1")
    assert out["clips"] == [] and "error" in out


# ---------------------------------------------------------------- style kit

def test_styles_are_offered_with_previews():
    styles = clipper.options()["styles"]
    ids = {c["value"] for c in styles["captions"]}
    assert {"highlight", "karaoke", "clean", "impact", "none"} <= ids
    for group in ("captions", "intros"):
        for item in styles[group]:
            if item["value"] != "none":
                assert item["preview"], f"{group}/{item['value']} has no rendered preview"


def test_style_text_falls_back_on_unknown_values():
    text = clipper.style_text({"caption": "<script>", "intro": "x", "accent": "red"}, "p1")
    assert 'caption="highlight"' in text and 'intro.style="hook"' in text and "#ff2d55" in text


def test_build_clip_project_writes_self_contained_project(tmp_path):
    from styles.clips.build import accent_ink, build_clip_project, sample_words

    video = tmp_path / "raw.mp4"
    video.write_bytes(b"0")
    words = sample_words("A short test sentence.", start=0.1)
    index = build_clip_project(tmp_path / "proj", video=video, duration=3.0, words=words,
                               caption="karaoke", intro={"style": "hook", "text": "Hi"}, accent="#ffd400")
    html = index.read_text(encoding="utf-8")
    assert 'data-width="1080"' in html and 'data-height="1920"' in html
    assert 'src="assets/raw.mp4"' in html and 'data-has-audio="true"' in html
    assert (tmp_path / "proj/kit/clip-kit.js").is_file()
    assert '"caption": "karaoke"' in html
    assert accent_ink("#ffd400") == "#111111" and accent_ink("#3d8bff") == "#ffffff"


# ---------------------------------------------------------------- publishing kit

def _published_project(root):
    proj = _write(root, {"source": {}, "status": "published", "clips": [
        {"id": "c01", "title": "Violet sky", "start": 0, "end": 9, "renders": {"9:16": "renders/clips/c01_9x16.mp4"},
         "publish": {"tiktok": {"caption": "Draft", "hashtags": ["ciencia", "#ciencia", "#céu!"]}}},
        {"id": "c02", "title": "No render", "start": 9, "end": 20},
    ]})
    (proj / "renders/clips").mkdir(parents=True)
    (proj / "renders/clips/c01_9x16.mp4").write_bytes(b"video")
    return proj


def test_publish_state_merges_draft_and_edits(projects_root):
    _published_project(projects_root)
    st = clipper.publish_state("p1", ["tiktok", "feed", "bogus"])
    assert st["platforms"] == ["tiktok", "feed"]
    assert [c["id"] for c in st["clips"]] == ["c01"]           # only rendered clips
    tk = st["clips"][0]["platforms"]["tiktok"]
    assert tk["caption"] == "Draft" and tk["hashtags"] == ["#ciencia", "#céu"]
    feed = st["clips"][0]["platforms"]["feed"]
    assert feed["aspect_ok"] is False and feed["video"].endswith("c01_9x16.mp4")  # falls back to the 9:16 render

    clipper.save_edit("p1", "c01", "tiktok", {"caption": "My version", "posted": True})
    tk = clipper.publish_state("p1", ["tiktok"])["clips"][0]["platforms"]["tiktok"]
    assert tk["caption"] == "My version" and tk["posted"] and tk["hashtags"] == ["#ciencia", "#céu"]


@pytest.mark.parametrize("platform,body", [
    ("myspace", {}),
    ("shorts", {"title": "x" * 101}),
    ("tiktok", {"caption": "x" * 2201}),
    ("tiktok", {"hashtags": [f"#t{i}" for i in range(31)]}),
])
def test_save_edit_validates(projects_root, platform, body):
    _published_project(projects_root)
    with pytest.raises(ValueError):
        clipper.save_edit("p1", "c01", platform, body)


def test_save_edit_rejects_bad_clip_id(projects_root):
    with pytest.raises(ValueError):
        clipper.save_edit("p1", "../../x", "tiktok", {})


def test_bundle_has_one_folder_per_network(projects_root):
    import io
    import zipfile

    _published_project(projects_root)
    zf = zipfile.ZipFile(io.BytesIO(clipper.build_bundle("p1", ["tiktok", "shorts"], "Demo")))
    names = set(zf.namelist())
    assert "TikTok/01-violet-sky.mp4" in names and "YouTube Shorts/01-violet-sky.mp4" in names
    assert zf.read("TikTok/01-violet-sky.txt").decode().startswith("Draft")
    assert zf.read("YouTube Shorts/01-violet-sky.txt").decode().startswith("TITLE: Violet sky")
    assert "CHECKLIST.txt" in names


def test_bundle_slug_keeps_accented_letters(projects_root):
    import io
    import zipfile

    proj = _write(projects_root, {"clips": [{"id": "c01", "title": "Céu é azul", "start": 0, "end": 5,
                                             "renders": {"9:16": "renders/clips/c01.mp4"}}]})
    (proj / "renders/clips").mkdir(parents=True)
    (proj / "renders/clips/c01.mp4").write_bytes(b"v")
    names = zipfile.ZipFile(io.BytesIO(clipper.build_bundle("p1", ["tiktok"]))).namelist()
    assert "TikTok/01-ceu-e-azul.mp4" in names


def test_save_kit_writes_the_kit_into_owlcutvideos(projects_root, tmp_path, monkeypatch):
    import os
    from backlot import exports

    out = tmp_path / "OwlCutVideos"
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(out))
    proj = _published_project(projects_root)
    folder = clipper.save_kit("p1", ["tiktok", "shorts"], "Clips — Demo", 1_700_000_000)
    assert folder.parent.parent == out / "Clips" and folder.name == "Publishing kit"
    assert folder.parent.name.startswith("Demo_")
    vid = folder / "TikTok/01-violet-sky.mp4"
    assert vid.read_bytes() == b"video"
    assert os.stat(vid).st_ino != os.stat(proj / "renders/clips/c01_9x16.mp4").st_ino   # own file (clone)
    assert (folder / "TikTok/01-violet-sky.txt").read_text().startswith("Draft")
    assert (folder / "YouTube Shorts/01-violet-sky.txt").read_text().startswith("TITLE: Violet sky")
    assert (folder / "CHECKLIST.txt").is_file()
    # same production folder as the auto export, and re-saving refreshes text / drops stale files
    assert exports.existing_folder("clips", "project:p1") == folder.parent
    clipper.save_edit("p1", "c01", "tiktok", {"caption": "Mine"})
    (folder / "TikTok/stale.txt").write_text("x")
    again = clipper.save_kit("p1", ["tiktok"], "Clips — Demo", 1_700_000_000)
    assert again == folder
    assert (folder / "TikTok/01-violet-sky.txt").read_text().startswith("Mine")
    assert not (folder / "TikTok/stale.txt").exists() and not (folder / "YouTube Shorts").exists()
