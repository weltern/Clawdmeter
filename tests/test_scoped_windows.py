"""Scoped usage windows (e.g. Weekly · Fable): parsing, selection, carry-forward,
persistence, the poller's "didn't find out" signal, and the approaching alerts.

Qt-free apart from the poller import. The fixture mirrors the live
/api/oauth/usage `limits[]` shape as of 2026-09-14 (numbers only, no account
data). Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import app_settings  # noqa: E402
import poller  # noqa: E402
from approaching_notify import ApproachingNotifier  # noqa: E402
from scoped_windows import (  # noqa: E402
    ScopedWindow,
    ScopedWindowTracker,
    merge_seen,
    shown,
    warn_threshold_for,
    windows_from_limits,
)

LIVE_LIMITS = [
    {"kind": "session", "group": "session", "percent": 37, "severity": "normal",
     "resets_at": "2026-09-15T01:30:00.542963+00:00", "scope": None, "is_active": False},
    {"kind": "weekly_all", "group": "weekly", "percent": 82, "severity": "warning",
     "resets_at": "2026-09-17T21:00:00.542983+00:00", "scope": None, "is_active": False},
    {"kind": "weekly_scoped", "group": "weekly", "percent": 93, "severity": "critical",
     "resets_at": "2026-09-17T21:00:00.543186+00:00",
     "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
     "is_active": True},
]
FABLE_RESET = datetime(2026, 9, 17, 21, 0, tzinfo=timezone.utc).timestamp()


def _w(name, pct, group="weekly", resets_at=None):
    return ScopedWindow(f"{group}:{name}", group, name, pct, resets_at)


# --- parsing ----------------------------------------------------------------

def test_live_shape_yields_only_the_scoped_window():
    (w,) = windows_from_limits(LIVE_LIMITS)
    assert (w.key, w.group, w.name, w.pct) == ("weekly:Fable", "weekly", "Fable", 93)
    assert w.label == "Weekly · Fable"
    assert w.resets_at == FABLE_RESET  # fractional seconds dropped


def test_surface_scope_is_kept_not_dropped():
    limits = [
        {"group": "weekly", "percent": 40,
         "scope": {"model": None, "surface": {"display_name": "Cowork"}}},
        {"group": "weekly", "percent": 5, "scope": {"surface": "Chats"}},
    ]
    assert [w.name for w in windows_from_limits(limits)] == ["Cowork", "Chats"]


def test_skips_unusable_entries_and_keeps_first_of_a_repeated_key():
    limits = [
        "not a dict",
        {"group": "weekly", "percent": True, "scope": {"model": {"display_name": "Opus"}}},
        {"group": "weekly", "percent": None, "scope": {"model": {"display_name": "Opus"}}},
        {"group": "weekly", "percent": 7, "scope": {"model": {"display_name": "  "}}},
        {"group": "weekly", "percent": 11, "scope": {"model": {"display_name": "Haiku"}}},
        {"group": "weekly", "percent": 99, "scope": {"model": {"display_name": "Haiku"}}},
    ]
    assert [(w.name, w.pct) for w in windows_from_limits(limits)] == [("Haiku", 11)]
    assert windows_from_limits(None) == []


def test_missing_group_is_not_labelled_weekly():
    (w,) = windows_from_limits([{"percent": 9, "scope": {"model": {"display_name": "Opus"}}}])
    assert (w.key, w.label) == ("limit:Opus", "Opus")
    assert warn_threshold_for(w, session=90, weekly=80) == 80


def test_unparseable_reset_is_unknown_not_zero_minutes_ago():
    (w,) = windows_from_limits([{"group": "weekly", "percent": 3, "resets_at": "soon",
                                 "scope": {"model": {"display_name": "Opus"}}}])
    assert w.resets_at is None
    assert w.reset_minutes(0.0) == 0


def test_reset_minutes_round_exactly_like_the_header_window():
    # 3000.4967 min to the whole-second reset -> 3000. Had the .543s fraction
    # been kept it would be 3000.5057 -> 3001, a minute off the WEEKLY bar that
    # resets at the same instant.
    now = FABLE_RESET - (3000 * 60 + 29.8)
    (w,) = windows_from_limits(LIVE_LIMITS)
    header = poller.sample_from_headers(
        {"anthropic-ratelimit-unified-7d-reset": str(int(FABLE_RESET))}, now)
    assert w.reset_minutes(now) == header.weekly_reset_minutes == 3000
    assert w.reset_minutes(FABLE_RESET + 5) == 0


# --- selection / memory -----------------------------------------------------

def test_tracker_keeps_last_known_through_a_failed_request():
    t = ScopedWindowTracker()
    fable = [_w("Fable", 93)]
    assert t.observe(fable) == fable
    assert t.observe(None) == fable     # usage request failed: keep
    assert t.observe([]) == []          # a real "none" clears


def test_shown_filters_to_ticked_keys_in_api_order():
    ws = [_w("Fable", 1), _w("Opus", 2), _w("Sonnet", 3)]
    assert [w.name for w in shown(ws, ["weekly:Sonnet", "weekly:Fable"])] == ["Fable", "Sonnet"]
    assert shown(ws, []) == []


def test_merge_seen_appends_updates_and_never_forgets():
    seen = [("weekly:Opus", "Weekly · Opus")]
    merged = merge_seen(seen, [_w("Fable", 93)])
    assert merged == [("weekly:Opus", "Weekly · Opus"), ("weekly:Fable", "Weekly · Fable")]
    assert merge_seen(merged, []) == merged
    assert merge_seen(merged, [_w("Fable", 1)]) == merged  # no duplicate


def test_threshold_follows_the_windows_group():
    assert warn_threshold_for(_w("Fable", 1), session=90, weekly=80) == 80
    assert warn_threshold_for(_w("Fable", 1, group="session"), session=90, weekly=80) == 90


# --- persistence -----------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    s = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    monkeypatch.setattr(app_settings, "_settings", lambda: s)
    return s


def test_settings_round_trip_and_default_off(store):
    assert app_settings.get_scoped_seen() == []
    assert app_settings.get_scoped_shown() == []   # opt-in
    app_settings.set_scoped_seen([("weekly:Fable", "Weekly · Fable")])
    app_settings.set_scoped_shown(["weekly:Fable", "weekly:Fable"])
    assert app_settings.get_scoped_seen() == [("weekly:Fable", "Weekly · Fable")]
    assert app_settings.get_scoped_shown() == ["weekly:Fable"]


@pytest.mark.parametrize("junk", ["{", "42", '{"a": 1}', '[1, ["x"], ["a", "b", "c"]]'])
def test_corrupt_settings_read_as_empty(store, junk):
    store.setValue(app_settings.KEY_SCOPED_SEEN, junk)
    store.setValue(app_settings.KEY_SCOPED_SHOWN, junk)
    assert app_settings.get_scoped_seen() == []
    assert app_settings.get_scoped_shown() == []


# --- poller: "no windows" vs "didn't find out" -----------------------------

def _patch_transport(monkeypatch, usage_status, profile_status=200):
    def handler(request):
        if request.url.path == "/v1/messages":
            return httpx.Response(200, headers={
                "anthropic-ratelimit-unified-7d-utilization": "0.82"}, json={})
        if request.url.path == "/api/oauth/usage":
            return httpx.Response(usage_status, json=(
                {"limits": LIVE_LIMITS} if usage_status == 200
                else {"error": {"message": "nope"}}))
        return httpx.Response(profile_status, json={})

    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(poller.httpx, "Client", _FakeClient)
    monkeypatch.setattr(poller.app_settings, "get_show_token_usage", lambda: False)


def test_poll_parses_scoped_windows(monkeypatch):
    _patch_transport(monkeypatch, 200)
    s = poller._poll_once("t")
    assert s.ok and [w.key for w in s.scoped_windows] == ["weekly:Fable"]


@pytest.mark.parametrize("usage_status", [500, 401])
def test_failed_usage_request_is_unknown_not_empty(monkeypatch, usage_status):
    # An error body is still JSON; parsed, it would read as "no scoped windows"
    # and blank the bars. It must come back as None so the last-known stay.
    _patch_transport(monkeypatch, usage_status)
    s = poller._poll_once("t")
    assert s.ok is True and s.weekly_pct == 82
    assert s.scoped_windows is None


def test_failed_profile_request_keeps_the_usage_fields(monkeypatch):
    # Only the plan tier comes from the profile; its failing must not throw
    # away the scoped windows (or the spend) that usage did return.
    _patch_transport(monkeypatch, 200, profile_status=503)
    s = poller._poll_once("t")
    assert [w.key for w in s.scoped_windows] == ["weekly:Fable"]
    assert s.plan_tier is None


# --- approaching alerts ----------------------------------------------------

def _sample(spct=10, wpct=10, ok=True):
    return SimpleNamespace(ok=ok, session_pct=spct, weekly_pct=wpct)


def _obs(n, scoped, *, spct=10, wpct=10, sthr=90, wthr=80, overage=True, enabled=True):
    return n.observe(_sample(spct, wpct), enabled=enabled, session_threshold=sthr,
                     weekly_threshold=wthr, overage_enabled=overage, scoped=scoped)


def test_scoped_window_warns_at_the_weekly_threshold():
    n = ApproachingNotifier()
    assert _obs(n, [_w("Fable", 70)]) == []                 # primes
    (e,) = _obs(n, [_w("Fable", 85)])
    assert (e.window, e.kind, e.pct, e.threshold) == ("Weekly · Fable", "approaching", 85, 80)
    assert _obs(n, [_w("Fable", 88)]) == []                 # once per cycle


def test_scoped_100_is_reached_while_main_windows_stay_overage():
    n = ApproachingNotifier()
    _obs(n, [_w("Fable", 95)], wpct=95)
    events = _obs(n, [_w("Fable", 101)], wpct=101)
    assert {(e.window, e.kind) for e in events} == {
        ("Weekly (7d)", "overage"), ("Weekly · Fable", "reached")}


def test_window_first_seen_above_threshold_primes_silently():
    n = ApproachingNotifier()
    _obs(n, [])                                  # launched with nothing ticked
    assert _obs(n, [_w("Fable", 93)]) == []      # user ticks Fable at 93%
    assert _obs(n, [_w("Fable", 94)]) == []


def test_rewatching_a_window_primes_again():
    n = ApproachingNotifier()
    _obs(n, [_w("Fable", 70)])
    _obs(n, [])                                  # unticked
    assert _obs(n, [_w("Fable", 90)]) == []      # re-ticked above threshold
    _obs(n, [_w("Fable", 60)])                   # a reset re-arms
    assert [e.kind for e in _obs(n, [_w("Fable", 85)])] == ["approaching"]


def test_session_group_window_uses_session_threshold():
    n = ApproachingNotifier()
    _obs(n, [_w("Fable", 50, group="session")])
    assert _obs(n, [_w("Fable", 85, group="session")]) == []   # under session's 90
    (e,) = _obs(n, [_w("Fable", 91, group="session")])
    assert e.threshold == 90


def test_scoped_windows_dont_disturb_the_main_windows():
    n = ApproachingNotifier()
    _obs(n, [_w("Fable", 10)], wpct=70)
    events = _obs(n, [_w("Fable", 10)], wpct=85)
    assert [(e.window, e.kind) for e in events] == [("Weekly (7d)", "approaching")]
