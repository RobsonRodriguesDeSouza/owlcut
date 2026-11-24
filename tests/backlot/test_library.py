"""Saved in OwlCutVideos: listing, post text per network, path safety."""

from __future__ import annotations

import pytest

from backlot import exports, library


@pytest.fixture
def saved(tmp_path, monkeypatch):
    monkeypatch.setenv("OWLCUT_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setattr(library, "POSTS_FILE", tmp_path / "posts.json")
    monkeypatch.setattr(library, "THUMBS_DIR", tmp_path / "thumbs")
    src = tmp_path / "c.mp4"
    src.write_bytes(b"clip")
    folder = exports.publish("clips", "quick:q", "Quick clips — Grand Theft Auto VI_20261121114025", 1_790_000_000,
                             [(src, "01 - is finally gonna happen.mp4"), (src, "02 - What.mp4")])
    (folder / "Publishing kit" / "TikTok").mkdir(parents=True)
    (folder / "Publishing kit" / "TikTok" / "01-x.mp4").write_bytes(b"dup")
    return folder


def test_lists_productions_and_cleans_titles(saved):
    prods = library.list_saved()
    assert len(prods) == 1
    p = prods[0]
    assert p["kind"] == "clips" and p["title"] == "Grand Theft Auto VI" and p["date"] and p["has_kit"]
    assert [v["label"] for v in p["videos"]] == ["is finally gonna happen", "What"]   # kit duplicates left out


def test_post_text_is_saved_per_network(saved):
    rel = library.list_saved()[0]["videos"][0]["path"]
    st = library.post_state(rel)
    assert st["posts"]["tiktok"]["caption"] == "is finally gonna happen" and not st["posts"]["tiktok"]["posted"]
    library.save_post(rel, "tiktok", {"caption": "GTA 6 is coming", "hashtags": "gta6, #gaming", "posted": True})
    st = library.post_state(rel)
    assert st["posts"]["tiktok"]["hashtags"] == ["#gta6", "#gaming"] and st["posts"]["tiktok"]["posted"]
    assert not st["posts"]["reels"]["posted"]
    assert library.list_saved()[0]["videos"][0]["posted"] == ["tiktok"]
    with pytest.raises(ValueError):
        library.save_post(rel, "tiktok", {"caption": "x" * 2201})
    with pytest.raises(ValueError):
        library.save_post(rel, "myspace", {})


@pytest.mark.parametrize("bad", ["../secret.mp4", "/etc/passwd", "~/x.mp4", "Clips/../../x.mp4", "Clips/a.txt", ""])
def test_paths_stay_inside_the_folder(saved, bad):
    with pytest.raises((ValueError, KeyError)):
        library.resolve(bad)


def test_api(saved):
    from fastapi.testclient import TestClient
    from backlot import server
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:4750")
    data = c.get("/api/saved").json()
    rel = data["productions"][0]["videos"][1]["path"]
    assert c.get("/api/saved/video", params={"path": rel}).content == b"clip"
    r = c.put("/api/saved/post", json={"path": rel, "platform": "shorts", "title": "What?!"},
              headers={"Origin": "http://127.0.0.1:4750"})
    assert r.status_code == 200 and r.json()["title"] == "What?!"
    assert c.get("/api/saved/video", params={"path": "../x.mp4"}).status_code == 400
    assert c.put("/api/saved/post", json={"path": rel, "platform": "shorts"}, headers={"Origin": "http://evil.example"}).status_code in (400, 403)
