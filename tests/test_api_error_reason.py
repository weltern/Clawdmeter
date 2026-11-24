"""Claude's own reason for refusing the token reaches the user.

Found on 2026-11-22: the dashboard said "Sign in again" with nothing more, and
the actual cause -- 401 "OAuth access token has been revoked." -- took a
hand-written API probe to see, because the poller kept only "401
Unauthorized". Revoked and expired are both a 401; only this text tells them
apart.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import auth_notify  # noqa: E402
import poller  # noqa: E402

REVOKED = {"type": "error", "error": {
    "type": "authentication_error",
    "message": "OAuth access token has been revoked."}}


def _patch(monkeypatch, code, **kw):
    def handler(request):
        return httpx.Response(code, **kw)

    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(poller.httpx, "Client", _FakeClient)


@pytest.mark.parametrize("code", [401, 403])
def test_the_reason_is_kept(monkeypatch, code):
    _patch(monkeypatch, code, json=REVOKED)
    s = poller._poll_once("t")
    assert s.ok is False
    assert s.reason == "OAuth access token has been revoked."
    assert s.error == f"HTTP {code}: OAuth access token has been revoked."


@pytest.mark.parametrize("kw", [
    {"text": "<html>bad gateway</html>"},
    {"json": {"error": "just a string"}},
    {"json": ["not", "an", "object"]},
    {"json": {"error": {"message": "   "}}},
    {},
])
def test_odd_bodies_give_no_reason(monkeypatch, kw):
    _patch(monkeypatch, 401, **kw)
    s = poller._poll_once("t")
    assert s.reason is None and s.error   # still says *something*


def test_a_long_reason_is_trimmed(monkeypatch):
    _patch(monkeypatch, 403, json={"error": {"message": "x" * 1000}})
    s = poller._poll_once("t")
    assert len(s.reason) <= 200 and s.reason.endswith("…")


def test_the_alert_quotes_it():
    n = auth_notify.AuthNotifier()
    s = SimpleNamespace(ok=False, status="reauth-needed",
                        reason="OAuth access token has been revoked.")
    alert = n.observe(s)
    assert "OAuth access token has been revoked." in alert.body
    assert "sign in again" in alert.body.lower()


def test_the_alert_without_a_reason_is_unchanged():
    n = auth_notify.AuthNotifier()
    alert = n.observe(SimpleNamespace(ok=False, status="no-token", reason=None))
    assert alert.body == auth_notify._LOST_BODIES["no-token"]


def test_it_survives_the_poll_thread_upgrade(monkeypatch):
    # _mark_reauth rewrites the status of a 401 nothing can recover; the reason
    # must ride along, not be lost in the rewrite.
    monkeypatch.setattr(poller.token_refresh, "auto_refresh_supported", lambda: False)
    p = poller.UsagePoller()
    s = poller.UsageSample(0, 0, 0, 0, poller.STATUS_AUTH_EXPIRED, False,
                           "HTTP 401: revoked", time.time(), reason="revoked")
    out = p._mark_reauth(s)
    assert out.status == poller.STATUS_REAUTH_NEEDED and out.reason == "revoked"
