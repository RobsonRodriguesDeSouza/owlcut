"""TikTok connection: Login Kit (OAuth 2 + PKCE) and the Content Posting API.

OwlCut sends a finished video to the creator's TikTok **inbox** (scope
``video.upload``): TikTok notifies the phone, and the creator opens the draft,
pastes the caption and posts it. That works before the app passes TikTok's
audit — direct public posting (``video.publish``) needs the audit.

Everything stays on this Mac, in ``.backlot/social/tiktok.json`` (mode 600):
the app's Client key/secret from developers.tiktok.com and the tokens of the
connected account. Nothing here is sent anywhere but TikTok's own endpoints.

Docs: developers.tiktok.com/doc/login-kit-desktop · /doc/content-posting-api-reference-upload-video
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API = "https://open.tiktokapis.com/v2"
SCOPES = ["user.info.basic", "video.upload"]
DEFAULT_REDIRECT = "http://localhost:4750/api/social/tiktok/callback"
CONFIG_FILE = Path(__file__).resolve().parents[1] / ".backlot" / "social" / "tiktok.json"
MIN_CHUNK, MAX_CHUNK = 5 * 1024 * 1024, 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024
_lock = threading.Lock()
_pending: dict[str, dict[str, Any]] = {}       # OAuth state -> {verifier, at}


class TikTokError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def _load() -> dict[str, Any]:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _store(data: dict[str, Any]) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def status() -> dict[str, Any]:
    """What the Settings page shows — never the secret or the tokens."""
    d = _load()
    acct = d.get("account") or {}
    connected = bool(d.get("access_token")) and (d.get("refresh_expires_at") or 0) > time.time()
    return {
        "client_key": d.get("client_key") or "",
        "has_secret": bool(d.get("client_secret")),
        "redirect_uri": d.get("redirect_uri") or DEFAULT_REDIRECT,
        "default_redirect": DEFAULT_REDIRECT,
        "scopes": SCOPES,
        "configured": bool(d.get("client_key") and d.get("client_secret")),
        "connected": connected,
        "account": {"display_name": acct.get("display_name") or "", "avatar_url": acct.get("avatar_url") or ""} if connected else None,
        "scope": d.get("scope") or "",
        "connected_at": d.get("connected_at") if connected else None,
    }


def save_keys(body: dict[str, Any]) -> dict[str, Any]:
    key = str(body.get("client_key") or "").strip()
    secret = body.get("client_secret")
    redirect = str(body.get("redirect_uri") or "").strip() or DEFAULT_REDIRECT
    if not key or len(key) > 64 or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError("Paste the Client key from your app on developers.tiktok.com.")
    if not redirect.startswith(("http://localhost", "http://127.0.0.1", "https://")):
        raise ValueError("The redirect URI must be http://localhost… or https://…")
    with _lock:
        d = _load()
        if key != d.get("client_key"):            # another app: the old login doesn't apply
            for k in ("access_token", "refresh_token", "expires_at", "refresh_expires_at", "open_id", "account", "scope"):
                d.pop(k, None)
        d["client_key"] = key
        if isinstance(secret, str) and secret.strip():
            s = secret.strip()
            if len(s) > 128 or not s.isascii() or any(c.isspace() for c in s):
                raise ValueError("That doesn't look like a Client secret.")
            d["client_secret"] = s
        if not d.get("client_secret"):
            raise ValueError("Paste the Client secret too.")
        d["redirect_uri"] = redirect
        _store(d)
    return status()


def forget_keys() -> dict[str, Any]:
    disconnect()
    with _lock:
        CONFIG_FILE.unlink(missing_ok=True)
    return status()


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def _http():
    import requests
    return requests


def auth_url() -> str:
    d = _load()
    if not (d.get("client_key") and d.get("client_secret")):
        raise ValueError("Save the Client key and secret first.")
    verifier = secrets.token_urlsafe(64)[:96]
    state = secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > 900]:
        _pending.pop(k, None)
    _pending[state] = {"verifier": verifier, "at": now}
    params = {
        "client_key": d["client_key"],
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": d.get("redirect_uri") or DEFAULT_REDIRECT,
        "state": state,
        # TikTok's desktop PKCE: the challenge is the *hex* SHA-256 of the verifier
        "code_challenge": hashlib.sha256(verifier.encode()).hexdigest(),
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    r = _http().post(f"{API}/oauth/token/", data=form, timeout=30,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        data = r.json()
    except ValueError:
        raise TikTokError(f"TikTok answered HTTP {r.status_code}.")
    if r.status_code != 200 or data.get("error") or not data.get("access_token"):
        raise TikTokError(data.get("error_description") or data.get("error") or f"HTTP {r.status_code}")
    return data


def _keep_tokens(d: dict[str, Any], tok: dict[str, Any]) -> None:
    now = time.time()
    d["access_token"] = tok["access_token"]
    d["refresh_token"] = tok.get("refresh_token") or d.get("refresh_token")
    d["expires_at"] = now + float(tok.get("expires_in") or 86400) - 120
    d["refresh_expires_at"] = now + float(tok.get("refresh_expires_in") or 365 * 86400) - 120
    d["open_id"] = tok.get("open_id") or d.get("open_id")
    d["scope"] = tok.get("scope") or d.get("scope")


def finish_login(code: str, state: str, error: str = "") -> dict[str, Any]:
    if error:
        raise TikTokError(error)
    pend = _pending.pop(state or "", None)
    if not pend or time.time() - pend["at"] > 900:
        raise TikTokError("The login expired or didn't start here. Click Connect again.")
    d = _load()
    tok = _token_request({
        "client_key": d["client_key"], "client_secret": d["client_secret"], "code": code,
        "grant_type": "authorization_code", "redirect_uri": d.get("redirect_uri") or DEFAULT_REDIRECT,
        "code_verifier": pend["verifier"],
    })
    with _lock:
        d = _load()
        _keep_tokens(d, tok)
        d["connected_at"] = time.time()
        _store(d)
    try:
        refresh_account()
    except TikTokError:
        pass
    return status()


def _access_token() -> str:
    with _lock:
        d = _load()
        if not d.get("access_token"):
            raise TikTokError("TikTok isn't connected. Connect it in Settings.")
        if (d.get("expires_at") or 0) > time.time():
            return d["access_token"]
        if (d.get("refresh_expires_at") or 0) <= time.time():
            raise TikTokError("The TikTok login expired. Connect it again in Settings.")
        tok = _token_request({"client_key": d["client_key"], "client_secret": d["client_secret"],
                              "grant_type": "refresh_token", "refresh_token": d["refresh_token"]})
        _keep_tokens(d, tok)
        _store(d)
        return d["access_token"]


def _api(method: str, path: str, **kw) -> dict[str, Any]:
    r = _http().request(method, f"{API}{path}", timeout=60,
                        headers={"Authorization": f"Bearer {_access_token()}",
                                 "Content-Type": "application/json; charset=UTF-8"}, **kw)
    try:
        data = r.json()
    except ValueError:
        raise TikTokError(f"TikTok answered HTTP {r.status_code}.")
    err = data.get("error") or {}
    if r.status_code != 200 or (err.get("code") and err.get("code") != "ok"):
        raise TikTokError(_explain(err.get("code"), err.get("message") or f"HTTP {r.status_code}"))
    return data.get("data") or {}


def _explain(code: Optional[str], msg: str) -> str:
    return {
        "scope_not_authorized": "The connected account didn't allow video uploads. Connect again and allow it.",
        "spam_risk_too_many_pending_share": "TikTok has too many drafts waiting in your inbox (max 5 in 24 h). Post or discard some in the app.",
        "spam_risk_too_many_posts": "TikTok's daily limit for this account was reached. Try again tomorrow.",
        "access_token_invalid": "The TikTok login is no longer valid. Connect again in Settings.",
        "rate_limit_exceeded": "Too many requests to TikTok — wait a minute and try again.",
        "url_ownership_unverified": "TikTok needs the domain verified for this kind of upload.",
    }.get(code or "", msg)


def refresh_account() -> dict[str, Any]:
    info = _api("GET", "/user/info/?fields=open_id,display_name,avatar_url").get("user") or {}
    with _lock:
        d = _load()
        d["account"] = {"display_name": info.get("display_name") or "", "avatar_url": info.get("avatar_url") or ""}
        _store(d)
    return d["account"]


def disconnect() -> None:
    with _lock:
        d = _load()
        token = d.get("access_token")
        for k in ("access_token", "refresh_token", "expires_at", "refresh_expires_at", "open_id", "account", "scope", "connected_at"):
            d.pop(k, None)
        if d:
            _store(d)
    if token and d.get("client_key") and d.get("client_secret"):
        try:   # tell TikTok too; the local logout stands even if this fails
            _http().post(f"{API}/oauth/revoke/", timeout=15,
                         data={"client_key": d["client_key"], "client_secret": d["client_secret"], "token": token})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# upload to the inbox
# ---------------------------------------------------------------------------

def chunk_plan(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) by TikTok's rules: one chunk up to 64 MB, otherwise
    10 MB chunks where the last one takes the remainder."""
    if size <= 0:
        raise ValueError("Empty video.")
    if size <= MAX_CHUNK:
        return size, 1
    return CHUNK, max(1, math.floor(size / CHUNK))


def upload_to_inbox(video: Path) -> dict[str, Any]:
    """Send a video to the connected account's TikTok inbox. Returns {publish_id}."""
    size = video.stat().st_size
    chunk, count = chunk_plan(size)
    data = _api("POST", "/post/publish/inbox/video/init/", json={"source_info": {
        "source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}})
    url, pid = data.get("upload_url"), data.get("publish_id")
    if not url or not pid:
        raise TikTokError("TikTok didn't return an upload address.")
    http = _http()
    with video.open("rb") as f:
        for i in range(count):
            start = i * chunk
            end = size - 1 if i == count - 1 else start + chunk - 1
            f.seek(start)
            body = f.read(end - start + 1)
            r = http.put(url, data=body, timeout=600, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                "Content-Range": f"bytes {start}-{end}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise TikTokError(f"Upload to TikTok failed (HTTP {r.status_code}).")
    return {"publish_id": pid}


def publish_status(publish_id: str) -> dict[str, Any]:
    data = _api("POST", "/post/publish/status/fetch/", json={"publish_id": publish_id})
    return {"status": data.get("status") or "", "fail_reason": data.get("fail_reason") or ""}
