"""TikTok connection: keys, OAuth with PKCE, token refresh, inbox upload — TikTok faked."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs, urlparse

import pytest

from backlot import social_tiktok as tk


class FakeResp:
    def __init__(self, status=200, data=None):
        self.status_code = status
        self._data = data if data is not None else {}

    def json(self):
        return self._data


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.token_n = 0

    def post(self, url, data=None, json=None, timeout=None, headers=None):
        self.calls.append(("POST", url, data or json, headers))
        if url.endswith("/oauth/token/"):
            self.token_n += 1
            return FakeResp(200, {"access_token": f"at{self.token_n}", "refresh_token": "rt", "expires_in": 86400,
                                  "refresh_expires_in": 31536000, "open_id": "oid", "scope": "user.info.basic,video.upload"})
        if url.endswith("/oauth/revoke/"):
            return FakeResp(200, {})
        raise AssertionError(url)

    def request(self, method, url, timeout=None, headers=None, json=None):
        self.calls.append((method, url, json, headers))
        if "/user/info/" in url:
            return FakeResp(200, {"data": {"user": {"display_name": "GTA 6 Activities", "avatar_url": "https://x/a.jpg"}},
                                  "error": {"code": "ok"}})
        if url.endswith("/inbox/video/init/"):
            return FakeResp(200, {"data": {"publish_id": "v_inbox_1", "upload_url": "https://upload.example/u"}, "error": {"code": "ok"}})
        if url.endswith("/status/fetch/"):
            return FakeResp(200, {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}})
        raise AssertionError(url)

    def put(self, url, data=None, timeout=None, headers=None):
        self.calls.append(("PUT", url, len(data), headers))
        return FakeResp(201)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(tk, "CONFIG_FILE", tmp_path / "social" / "tiktok.json")
    http = FakeHTTP()
    monkeypatch.setattr(tk, "_http", lambda: http)
    tk._pending.clear()
    return http


def _connect(fake):
    tk.save_keys({"client_key": "awkey123", "client_secret": "s3cret"})
    url = tk.auth_url()
    q = parse_qs(urlparse(url).query)
    return tk.finish_login("CODE", q["state"][0]), q


def test_keys_are_private_and_validated(fake):
    with pytest.raises(ValueError):
        tk.save_keys({"client_key": "awkey123"})                      # no secret
    with pytest.raises(ValueError):
        tk.save_keys({"client_key": "has space", "client_secret": "x"})
    st = tk.save_keys({"client_key": "awkey123", "client_secret": "s3cret"})
    assert st["configured"] and st["has_secret"] and not st["connected"]
    assert "s3cret" not in json.dumps(st)
    assert stat.S_IMODE(os.stat(tk.CONFIG_FILE).st_mode) == 0o600
    st = tk.save_keys({"client_key": "awkey123", "client_secret": ""})  # empty keeps the saved secret
    assert st["has_secret"]


def test_oauth_uses_pkce_and_stores_tokens(fake):
    st, q = _connect(fake)
    assert q["client_key"] == ["awkey123"] and q["scope"] == ["user.info.basic,video.upload"]
    assert q["redirect_uri"] == [tk.DEFAULT_REDIRECT] and q["code_challenge_method"] == ["S256"]
    form = next(c[2] for c in fake.calls if c[1].endswith("/oauth/token/"))
    assert form["grant_type"] == "authorization_code" and form["code"] == "CODE"
    assert q["code_challenge"] == [hashlib.sha256(form["code_verifier"].encode()).hexdigest()]
    assert st["connected"] and st["account"]["display_name"] == "GTA 6 Activities"
    assert "at1" not in json.dumps(st)                                # tokens never leave the server


def test_login_state_must_match(fake):
    tk.save_keys({"client_key": "awkey123", "client_secret": "s3cret"})
    tk.auth_url()
    with pytest.raises(tk.TikTokError):
        tk.finish_login("CODE", "forged-state")
    with pytest.raises(tk.TikTokError):
        tk.finish_login("", "", "access_denied")


def test_expired_access_token_is_refreshed(fake):
    _connect(fake)
    d = json.loads(tk.CONFIG_FILE.read_text())
    d["expires_at"] = time.time() - 10
    tk.CONFIG_FILE.write_text(json.dumps(d))
    tk.publish_status("v_inbox_1")
    refresh = [c for c in fake.calls if c[1].endswith("/oauth/token/")][-1][2]
    assert refresh["grant_type"] == "refresh_token" and refresh["refresh_token"] == "rt"
    last = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/status/fetch/")][-1]
    assert last[3]["Authorization"] == "Bearer at2"


def test_chunk_plan_follows_tiktok_rules():
    assert tk.chunk_plan(3_000_000) == (3_000_000, 1)
    assert tk.chunk_plan(64 * 1024 * 1024) == (64 * 1024 * 1024, 1)
    size = 100 * 1024 * 1024
    chunk, n = tk.chunk_plan(size)
    assert chunk == 10 * 1024 * 1024 and n == 10                       # last chunk takes the remainder


def test_upload_to_inbox(fake, tmp_path):
    _connect(fake)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v" * 1234)
    assert tk.upload_to_inbox(video) == {"publish_id": "v_inbox_1"}
    init = next(c for c in fake.calls if c[1].endswith("/inbox/video/init/"))
    assert init[2]["source_info"] == {"source": "FILE_UPLOAD", "video_size": 1234, "chunk_size": 1234, "total_chunk_count": 1}
    put = next(c for c in fake.calls if c[0] == "PUT")
    assert put[2] == 1234 and put[3]["Content-Range"] == "bytes 0-1233/1234"
    assert tk.publish_status("v_inbox_1")["status"] == "SEND_TO_USER_INBOX"


def test_not_connected_is_a_clear_error(fake, tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"v")
    with pytest.raises(tk.TikTokError, match="isn't connected"):
        tk.upload_to_inbox(video)


def test_disconnect_and_api(fake):
    from fastapi.testclient import TestClient
    from backlot import server
    c = TestClient(server.create_app(), base_url="http://127.0.0.1:4750")
    o = {"Origin": "http://127.0.0.1:4750"}
    assert c.put("/api/social/tiktok/keys", json={"client_key": "k1", "client_secret": "s"}, headers={"Origin": "http://evil.example"}).status_code == 403
    r = c.put("/api/social/tiktok/keys", json={"client_key": "k1", "client_secret": "s"}, headers=o)
    assert r.status_code == 200 and r.json()["configured"]
    r = c.get("/api/social/tiktok/connect", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(tk.AUTH_URL)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    r = c.get("/api/social/tiktok/callback", params={"code": "C", "state": state}, follow_redirects=False)
    assert r.headers["location"].startswith("/settings?tiktok=connected")
    assert c.get("/api/social/tiktok").json()["connected"]
    r = c.get("/api/social/tiktok/callback", params={"code": "C", "state": "nope"}, follow_redirects=False)
    assert "tiktok_error=" in r.headers["location"]
    assert not c.post("/api/social/tiktok/disconnect", json={}, headers=o).json()["connected"]
    assert any(x[1].endswith("/oauth/revoke/") for x in fake.calls)
