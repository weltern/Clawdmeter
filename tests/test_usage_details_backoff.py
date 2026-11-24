"""The usage/profile requests are throttled, cached, and honour a 429.

Found on 2026-11-22: /api/oauth/usage answered 429 with Retry-After: 3600, and
Clawdmeter asked again every 30 s regardless -- the usage and profile requests
rode along on every poll, two thirds of the app's API traffic, and a failure
was swallowed silently. Now UsageDetails asks every few minutes at most, waits
out a 429, and re-applies the last values it did get in between.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
from email.utils import format_datetime
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import poller  # noqa: E402

LIMITS = [{"group": "weekly", "percent": 41,
           "scope": {"model": {"display_name": "Fable"}}}]


class _Server:
    """Counts requests per path and answers from a per-path script."""

    def __init__(self):
        self.calls = {"/v1/messages": 0, "/api/oauth/usage": 0,
                      "/api/oauth/profile": 0}
        self.usage = lambda: httpx.Response(200, json={"limits": LIMITS})
        self.profile = lambda: httpx.Response(200, json={
            "organization": {"rate_limit_tier": "default_claude_max_5x"}})

    def __call__(self, request):
        self.calls[request.url.path] += 1
        if request.url.path == "/v1/messages":
            return httpx.Response(200, headers={
                "anthropic-ratelimit-unified-5h-utilization": "0.25"}, json={})
        if request.url.path == "/api/oauth/usage":
            return self.usage()
        return self.profile()


@pytest.fixture
def server(monkeypatch):
    srv = _Server()

    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(srv)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(poller.httpx, "Client", _FakeClient)
    monkeypatch.setattr(poller.app_settings, "get_show_token_usage", lambda: False)
    return srv


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(poller.time, "time", lambda: now[0])
    return now


def test_details_are_not_fetched_on_every_poll(server, clock):
    d = poller.UsageDetails()
    for _ in range(10):                     # 10 polls, 30 s apart = 5 minutes
        s = poller._poll_once("t", d)
        clock[0] += 30
    assert server.calls["/v1/messages"] == 10
    assert server.calls["/api/oauth/usage"] == 1
    assert server.calls["/api/oauth/profile"] == 1
    # ...but every sample still carries the last-known details.
    assert [w.key for w in s.scoped_windows] == ["weekly:Fable"]
    assert s.plan_tier == "default_claude_max_5x"

    poller._poll_once("t", d)               # 300 s after the first: due again
    assert server.calls["/api/oauth/usage"] == 2
    assert server.calls["/api/oauth/profile"] == 1   # hourly


def test_a_429_is_honoured_for_its_retry_after(server, clock):
    d = poller.UsageDetails()
    poller._poll_once("t", d)               # primes the cache
    clock[0] += poller.USAGE_DETAILS_INTERVAL
    server.usage = lambda: httpx.Response(
        429, headers={"retry-after": "3600"}, json={"error": {"message": "Rate limited"}})
    s = poller._poll_once("t", d)
    assert s.ok and s.session_pct == 25     # the bars are unaffected
    assert s.details_paused_until == pytest.approx(clock[0] + 3600)
    assert [w.key for w in s.scoped_windows] == ["weekly:Fable"]   # held, not blanked
    asked = server.calls["/api/oauth/usage"]

    for _ in range(100):                    # just under an hour of 30 s polls
        clock[0] += 30
        if clock[0] >= s.details_paused_until:
            break
        poller._poll_once("t", d)
    assert server.calls["/api/oauth/usage"] == asked   # not asked once meanwhile

    server.usage = lambda: httpx.Response(200, json={"limits": LIMITS})
    clock[0] = s.details_paused_until + 1
    after = poller._poll_once("t", d)
    assert server.calls["/api/oauth/usage"] == asked + 1
    assert after.details_paused_until is None


def test_retry_after_as_an_http_date(server, clock):
    d = poller.UsageDetails()
    when = datetime.fromtimestamp(clock[0] + 1800, timezone.utc)
    server.usage = lambda: httpx.Response(
        429, headers={"retry-after": format_datetime(when, usegmt=True)}, json={})
    s = poller._poll_once("t", d)
    assert s.details_paused_until == pytest.approx(clock[0] + 1800, abs=1)


def test_a_429_without_retry_after_backs_off_and_doubles(server, clock):
    d = poller.UsageDetails()
    server.usage = lambda: httpx.Response(429, json={})
    first = poller._poll_once("t", d).details_paused_until - clock[0]
    clock[0] += first
    second = poller._poll_once("t", d).details_paused_until - clock[0]
    assert first == poller.DETAILS_429_DEFAULT and second == 2 * first


def test_an_absurd_retry_after_is_capped(server, clock):
    d = poller.UsageDetails()
    server.usage = lambda: httpx.Response(429, headers={"retry-after": "9999999"})
    s = poller._poll_once("t", d)
    assert s.details_paused_until - clock[0] == poller.DETAILS_429_MAX


def test_a_plain_failure_retries_soon_not_every_poll(server, clock):
    d = poller.UsageDetails()
    server.usage = lambda: httpx.Response(500, json={})
    s = poller._poll_once("t", d)
    assert s.scoped_windows is None and s.details_paused_until is None
    clock[0] += 30
    poller._poll_once("t", d)
    assert server.calls["/api/oauth/usage"] == 1
    clock[0] += poller.DETAILS_RETRY_AFTER_ERROR
    poller._poll_once("t", d)
    assert server.calls["/api/oauth/usage"] == 2


def test_a_new_token_asks_again_at_once(server, clock):
    d = poller.UsageDetails()
    poller._poll_once("old", d)
    clock[0] += 30
    poller._poll_once("new", d)             # a refresh or a fresh sign-in
    assert server.calls["/api/oauth/usage"] == 2


def test_retry_after_parsing():
    assert poller.retry_after_seconds("120", 0) == 120
    assert poller.retry_after_seconds(None, 0) is None
    assert poller.retry_after_seconds("soon", 0) is None
    assert poller.retry_after_seconds("-5", 0) == 0
