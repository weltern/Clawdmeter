"""Tests for refreshing the token alongside Claude Code.

Covers what changed once the Claude desktop app, rather than the `claude` CLI,
became the usual way in: the CLI no longer keeps ~/.claude/.credentials.json
fresh, so Clawdmeter has to refresh like the CLI does -- early, under the CLI's
refresh lock, standing down when someone else already rotated the tokens, and
never re-sending a refresh token the endpoint rejected.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import json
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import poller  # noqa: E402
import token_refresh as tr  # noqa: E402


def _now_ms() -> int:
    return int(time.time() * 1000)


@pytest.fixture
def cred(tmp_path):
    d = tmp_path / "claude"
    d.mkdir()
    path = d / ".credentials.json"

    def write(access="old", refresh="r-old", expires=None, scopes=None):
        blk = {"accessToken": access, "refreshToken": refresh,
               "expiresAt": _now_ms() - 1000 if expires is None else expires}
        if scopes is not None:
            blk["scopes"] = scopes
        path.write_text(json.dumps({"claudeAiOauth": blk}), encoding="utf-8")
        return path

    write()
    return path, write


@pytest.fixture(autouse=True)
def _clean_dead_tokens(monkeypatch):
    # token_refresh imports httpx lazily, and some other test modules leave a
    # stand-in httpx in sys.modules; make sure the lazy import sees the real one.
    monkeypatch.setitem(sys.modules, "httpx", httpx)
    tr._dead_refresh_tokens.clear()
    yield
    tr._dead_refresh_tokens.clear()


def _mock_http(monkeypatch, handler):
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(wrapped)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return calls


def _ok_token(request):
    return httpx.Response(200, json={
        "access_token": "new", "refresh_token": "r-new", "expires_in": 28800,
        "scope": "user:inference user:profile"})


def _blk(path):
    return json.loads(path.read_text())["claudeAiOauth"]


def test_refresh_uses_current_endpoint_and_stored_scopes(monkeypatch, cred):
    path, write = cred
    write(scopes=["user:inference", "user:profile"])
    calls = _mock_http(monkeypatch, _ok_token)

    res = tr.refresh(path)

    assert res.ok
    assert str(calls[0].url) == tr.OAUTH_TOKEN_URLS[0]
    body = json.loads(calls[0].content)
    assert body["scope"] == "user:inference user:profile"
    assert body["refresh_token"] == "r-old"
    blk = _blk(path)
    assert blk["accessToken"] == "new" and blk["refreshToken"] == "r-new"
    # Lock released afterwards.
    assert not (path.parent / tr.LOCK_NAME).exists()


def test_falls_back_to_legacy_endpoint_on_404(monkeypatch, cred):
    path, _ = cred

    def handler(request):
        if str(request.url) == tr.OAUTH_TOKEN_URLS[0]:
            return httpx.Response(404)
        return _ok_token(request)

    calls = _mock_http(monkeypatch, handler)
    assert tr.refresh(path).ok
    assert [str(c.url) for c in calls] == list(tr.OAUTH_TOKEN_URLS)


def test_refreshes_before_expiry(cred):
    path, write = cred
    write(expires=_now_ms() + 60_000)          # 1 min left -> inside the window
    assert tr.is_expired(path)
    write(expires=_now_ms() + 3_600_000)       # 1 h left -> leave it alone
    assert not tr.is_expired(path)


def test_busy_lock_skips_without_network(monkeypatch, cred):
    path, _ = cred
    calls = _mock_http(monkeypatch, _ok_token)
    (path.parent / tr.LOCK_NAME).mkdir()       # Claude Code is mid-refresh

    res = tr.refresh(path)

    assert not res.ok and res.busy
    assert calls == []
    assert _blk(path)["accessToken"] == "old"
    assert (path.parent / tr.LOCK_NAME).exists()   # not ours to remove


def test_stale_lock_is_taken_over(monkeypatch, cred):
    path, _ = cred
    _mock_http(monkeypatch, _ok_token)
    lock = path.parent / tr.LOCK_NAME
    lock.mkdir()
    old = time.time() - tr.LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))

    assert tr.refresh(path).ok
    assert not lock.exists()


def test_stands_down_when_token_already_rotated(monkeypatch, cred):
    path, write = cred
    calls = _mock_http(monkeypatch, _ok_token)
    write(access="rotated-by-cli", expires=_now_ms() + 3_600_000)

    res = tr.refresh(path, force=True, seen_access="old")

    assert res.ok
    assert calls == []


def test_not_expired_is_a_noop_unless_forced(monkeypatch, cred):
    path, write = cred
    write(expires=_now_ms() + 3_600_000)
    calls = _mock_http(monkeypatch, _ok_token)

    assert tr.refresh(path).ok and calls == []
    assert tr.refresh(path, force=True).ok and len(calls) == 1


def test_rejected_refresh_token_is_not_resent(monkeypatch, cred):
    path, _ = cred
    calls = _mock_http(monkeypatch, lambda r: httpx.Response(400, json={"error": "invalid_grant"}))

    first = tr.refresh(path)
    second = tr.refresh(path)

    assert not first.ok and first.http_status == 400
    assert not second.ok and "sign in again" in second.status
    assert len(calls) == 1


def test_poller_recovers_from_401_with_valid_looking_expiry(monkeypatch, cred):
    """A 401 with a far-future expiresAt forces a refresh and a re-poll."""
    path, write = cred
    write(expires=_now_ms() + 3_600_000)
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PATH", str(path))
    monkeypatch.setattr(poller.app_settings, "get_auto_refresh", lambda: True)
    monkeypatch.setattr(poller.app_settings, "get_show_token_usage", lambda: False)

    def handler(request):
        if request.url.path == "/v1/oauth/token":
            return _ok_token(request)
        if request.headers.get("authorization") != "Bearer new":
            return httpx.Response(401, json={})
        if request.url.path == "/v1/messages":
            return httpx.Response(200, headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.4"}, json={})
        return httpx.Response(200, json={})

    _mock_http(monkeypatch, handler)
    p = poller.UsagePoller()
    first = poller._poll_once("old")
    assert first.auth_failed

    recovered = p._recover_from_401("old")

    assert recovered is not None and recovered.ok and recovered.session_pct == 40
    assert _blk(path)["accessToken"] == "new"
