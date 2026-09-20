"""Refreshing early, backing off by cause, and the sign-in handoff.

The failure being designed out: the token expired at 02:42 and the app spent
the next 34 hours retrying a refresh every 15 minutes against an endpoint that
answers 429, never recovering and never saying so. Three separate faults —
refreshing only once it was already too late, treating "rate limited" and
"this token is dead" as the same thing, and having no way to sign in again.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import poller  # noqa: E402
import reauth  # noqa: E402
import token_refresh  # noqa: E402

Outcome = token_refresh.RefreshOutcome


def _creds(tmp_path, expires_at_ms):
    p = tmp_path / ".credentials.json"
    p.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": "a", "refreshToken": "r", "expiresAt": expires_at_ms,
    }}), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
# Refresh EARLY, not at the graveside
# --------------------------------------------------------------------------
# The clock is pinned on every one of these. Reading time.time() inside the
# function while the fixture was built a moment earlier is how a boundary test
# fails one run in twenty-five.

NOW = 1_800_000_000.0


@pytest.mark.parametrize("secs_left, expected", [
    (3600, False),     # an hour out: nothing to do yet
    (1801, False),     # one second before the lead window opens
    (1800, True),      # exactly on the lead boundary
    (600, True),       # well inside it
    (-60, True),       # already dead
])
def test_needs_refresh_opens_a_window_before_expiry(tmp_path, secs_left, expected):
    path = _creds(tmp_path, int((NOW + secs_left) * 1000))

    assert token_refresh.needs_refresh(path, now=NOW) is expected


@pytest.mark.parametrize("secs_left, expected", [
    (1800, False),     # needs_refresh says yes here; is_expired must NOT
    (121, False),
    (120, True),       # the old skew, unchanged
    (-1, True),
])
def test_is_expired_still_means_actually_expired(tmp_path, secs_left, expected):
    """Settings asks "is it broken NOW" and must not inherit the wider window."""
    path = _creds(tmp_path, int((NOW + secs_left) * 1000))

    assert token_refresh.is_expired(path, now=NOW) is expected


def test_unknown_expiry_triggers_nothing(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text("{}", encoding="utf-8")

    assert token_refresh.needs_refresh(path, now=NOW) is False
    assert token_refresh.is_expired(path, now=NOW) is False


# --------------------------------------------------------------------------
# WHY a refresh failed
# --------------------------------------------------------------------------

def _patch_refresh_transport(monkeypatch, handler):
    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    import httpx as real_httpx
    monkeypatch.setattr(real_httpx, "Client", _FakeClient)


@pytest.mark.parametrize("code, expected", [
    (429, Outcome.THROTTLED),
    (400, Outcome.REJECTED),
    (401, Outcome.REJECTED),
    (500, Outcome.ERROR),
])
def test_refresh_classifies_the_server_answer(tmp_path, monkeypatch, code, expected):
    path = _creds(tmp_path, int(NOW * 1000))
    _patch_refresh_transport(monkeypatch, lambda req: httpx.Response(code, json={}))

    result = token_refresh.refresh(path)

    assert result.ok is False
    assert result.outcome is expected


def test_refresh_reports_transport_failure_as_unreachable(tmp_path, monkeypatch):
    path = _creds(tmp_path, int(NOW * 1000))

    def handler(req):
        raise httpx.ConnectTimeout("no route")

    _patch_refresh_transport(monkeypatch, handler)

    assert token_refresh.refresh(path).outcome is Outcome.UNREACHABLE


def test_missing_refresh_token_is_a_dead_end_not_a_retry(tmp_path):
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}),
                    encoding="utf-8")

    assert token_refresh.refresh(path).outcome is Outcome.REJECTED


def test_a_good_refresh_says_so(tmp_path, monkeypatch):
    path = _creds(tmp_path, int(NOW * 1000))
    _patch_refresh_transport(monkeypatch, lambda req: httpx.Response(200, json={
        "access_token": "new", "refresh_token": "newr", "expires_in": 28800,
    }))

    result = token_refresh.refresh(path)

    assert result.ok is True
    assert result.outcome is Outcome.REFRESHED
    assert json.loads(path.read_text(encoding="utf-8"))["claudeAiOauth"]["accessToken"] == "new"


# --------------------------------------------------------------------------
# Backing off by cause
# --------------------------------------------------------------------------

def _poller_with(monkeypatch, outcome, expiry_ms=123):
    p = poller.UsagePoller()
    monkeypatch.setattr(token_refresh, "refresh",
                        lambda *a, **k: token_refresh.RefreshResult(
                            outcome is Outcome.REFRESHED, "x", outcome=outcome))
    monkeypatch.setattr(token_refresh, "token_expiry_ms", lambda *a, **k: expiry_ms)
    return p


def test_throttling_backs_off_far_past_the_ordinary_ceiling(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.THROTTLED)

    for _ in range(20):
        p._do_refresh(manual=False)

    assert p._cooldown == p.REFRESH_THROTTLED_MAX
    assert p._cooldown > p.REFRESH_COOLDOWN_MAX


def test_a_dead_refresh_token_stops_the_retry_loop(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.REJECTED, expiry_ms=999)
    p._do_refresh(manual=False)

    assert p._reauth_needed() is True

    # Nothing further is attempted while the same dead credentials are on disk.
    calls = []
    monkeypatch.setattr(p, "_do_refresh", lambda manual: calls.append(manual))
    p._maybe_auto_refresh()

    assert calls == []


def test_signing_in_again_clears_the_block_on_its_own(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.REJECTED, expiry_ms=999)
    p._do_refresh(manual=False)
    assert p._reauth_needed() is True

    # A new sign-in writes a different expiry; nothing has to notify the poller.
    monkeypatch.setattr(token_refresh, "token_expiry_ms", lambda *a, **k: 1000)

    assert p._reauth_needed() is False


def test_a_successful_refresh_unblocks_and_resets_the_cooldown(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.REJECTED, expiry_ms=999)
    p._do_refresh(manual=False)
    p._cooldown = 3600.0

    monkeypatch.setattr(token_refresh, "refresh",
                        lambda *a, **k: token_refresh.RefreshResult(
                            True, "ok", outcome=Outcome.REFRESHED))
    p._do_refresh(manual=False)

    assert p._blocked_expiry_ms is None
    assert p._cooldown == p.REFRESH_COOLDOWN_MIN


def test_a_manual_attempt_is_not_punished_with_backoff(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.THROTTLED)
    before = p._cooldown

    p._do_refresh(manual=True)

    assert p._cooldown == before


def test_network_trouble_keeps_the_short_ceiling(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.UNREACHABLE)

    for _ in range(20):
        p._do_refresh(manual=False)

    assert p._cooldown == p.REFRESH_COOLDOWN_MAX


# --------------------------------------------------------------------------
# The sign-in handoff
# --------------------------------------------------------------------------

def test_login_argv_uses_the_subscription_flow(monkeypatch):
    monkeypatch.setattr(reauth, "cli_path", lambda: r"C:\bin\claude.exe")

    argv = reauth.login_argv()

    assert argv == [r"C:\bin\claude.exe", "auth", "login", "--claudeai"]


def test_login_argv_can_prefill_an_email(monkeypatch):
    monkeypatch.setattr(reauth, "cli_path", lambda: "/usr/bin/claude")

    assert reauth.login_argv("a@b.c")[-2:] == ["--email", "a@b.c"]


def test_a_missing_cli_still_tells_you_what_to_run(monkeypatch):
    monkeypatch.setattr(reauth, "cli_path", lambda: None)

    started, message = reauth.start_login()

    assert started is False
    assert "claude auth login" in message


def test_a_terminal_that_will_not_open_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(reauth, "cli_path", lambda: "/usr/bin/claude")
    for name in ("_spawn_windows", "_spawn_macos", "_spawn_linux"):
        monkeypatch.setattr(reauth, name,
                            lambda argv: (_ for _ in ()).throw(OSError("nope")))

    started, message = reauth.start_login()

    assert started is False
    assert "claude auth login" in message


def test_a_started_login_reports_success(monkeypatch):
    monkeypatch.setattr(reauth, "cli_path", lambda: "/usr/bin/claude")
    seen = []
    for name in ("_spawn_windows", "_spawn_macos", "_spawn_linux"):
        monkeypatch.setattr(reauth, name, lambda argv: seen.append(argv))

    started, _message = reauth.start_login()

    assert started is True
    assert seen and seen[0][1:4] == ["auth", "login", "--claudeai"]


def test_reauth_status_is_distinct_from_a_plain_expiry():
    """They need different badges because they need different actions."""
    assert poller.STATUS_REAUTH_NEEDED != poller.STATUS_AUTH_EXPIRED


def _sample(status):
    return poller.UsageSample(0, 0, 0, 0, status, False, None, NOW)


def test_expiry_becomes_sign_in_again_once_the_refresh_is_dead(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.REJECTED, expiry_ms=999)
    p._do_refresh(manual=False)

    assert p._mark_reauth(_sample(poller.STATUS_AUTH_EXPIRED)).status == \
        poller.STATUS_REAUTH_NEEDED


def test_a_recoverable_expiry_is_left_alone():
    """No rejection seen, so the next refresh may well fix it by itself."""
    p = poller.UsagePoller()

    assert p._mark_reauth(_sample(poller.STATUS_AUTH_EXPIRED)).status == \
        poller.STATUS_AUTH_EXPIRED


def test_other_failures_are_never_relabelled_as_auth(monkeypatch):
    p = _poller_with(monkeypatch, Outcome.REJECTED, expiry_ms=999)
    p._do_refresh(manual=False)

    for status in (poller.STATUS_OFFLINE, poller.STATUS_HTTP_ERROR):
        assert p._mark_reauth(_sample(status)).status == status
