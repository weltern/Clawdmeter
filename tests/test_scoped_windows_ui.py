"""Scoped usage windows on screen: the shared row helper, the full / compact /
mini views, the Settings checkboxes, and the alert wording end to end.

Runs a real Dashboard in mock mode headless (QT_QPA_PLATFORM=offscreen) with
its mock timers stopped, so every sample is one the test feeds, and with
QSettings on a throwaway ini. Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QWidget  # noqa: E402

import app_settings  # noqa: E402
import dashboard  # noqa: E402
import session_shelf  # noqa: E402
from poller import UsageSample  # noqa: E402
from scoped_windows import ScopedWindow, windows_from_limits  # noqa: E402

_app = QApplication.instance() or QApplication([])
app_settings.set_theme = lambda name: None


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    store = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)
    monkeypatch.setattr(app_settings, "_settings", lambda: store)
    yield store


def _reset_iso(hours=30):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _sample(windows, *, weekly=40, ok=True):
    return UsageSample(
        session_pct=10, session_reset_minutes=100, weekly_pct=weekly,
        weekly_reset_minutes=3000, status="ok", ok=ok, timestamp=time.time(),
        scoped_windows=windows)


def _fable(pct, name="Fable", hours=30):
    return windows_from_limits([{"group": "weekly", "percent": pct,
                                 "resets_at": _reset_iso(hours),
                                 "scope": {"model": {"display_name": name}}}])


@pytest.fixture
def dash():
    # Samples here jump from the mock's own first reading, which the reset
    # notifier would (correctly) call a reset; keep its alert out of the way.
    app_settings.set_reset_notify(False)
    d = dashboard.Dashboard(mock=True)
    d._mock_sample_timer.stop()
    d._mock_shelf_timer.stop()
    d._countdown.stop()
    try:
        yield d
    finally:
        d.close()
        d.compact_view.hide()
        d.mini.hide()
        d.deleteLater()
        _app.processEvents()


def _texts(widget) -> list[str]:
    return [lbl.text() for lbl in widget.findChildren(QLabel)]


# --- ScopedRows -------------------------------------------------------------

def test_rows_reconcile_by_key_and_hide_when_empty():
    made = []

    def make():
        w = QWidget()
        made.append(w)
        return w, w

    drawn = []
    rows = session_shelf.ScopedRows(make, lambda p, w, m, t: drawn.append(w.name),
                                    spacing=4)
    a, b = (ScopedWindow(f"weekly:{n}", "weekly", n, 1) for n in ("A", "B"))
    assert rows.isHidden()
    assert rows.set_windows([(a, 0, 80), (b, 0, 80)]) is True
    assert rows.keys() == ["weekly:A", "weekly:B"] and not rows.isHidden()
    assert rows.set_windows([(a, 0, 80), (b, 0, 80)]) is False   # steady: no re-fit
    assert rows.set_windows([(b, 0, 80), (a, 0, 80)]) is True    # reorder
    assert len(made) == 2                                         # rows reused
    assert rows._col.count() == 2
    assert [rows._col.itemAt(i).widget() for i in range(2)] == [made[1], made[0]]
    assert rows.set_windows([]) is True and rows.isHidden()
    assert drawn[-2:] == ["B", "A"]


# --- full / mini / compact ---------------------------------------------------

def test_unticked_window_is_remembered_but_not_drawn(dash):
    dash._on_sample(_sample(_fable(93)))
    assert ("weekly:Fable", "Weekly · Fable") in app_settings.get_scoped_seen()
    assert dash.scoped_rows.keys() == []
    assert dash.mini.scoped_rows.keys() == []


def test_ticking_in_settings_draws_rows_without_waiting_for_a_poll(dash):
    dash._on_sample(_sample(_fable(93)))
    check = dash.settings_panel._scoped_checks["weekly:Fable"]
    assert check.text() == "Weekly · Fable" and not check.isChecked()

    check.setChecked(True)                     # no new sample
    assert app_settings.get_scoped_shown() == ["weekly:Fable"]
    assert dash.scoped_rows.keys() == ["weekly:Fable"]
    texts = _texts(dash.scoped_rows)
    assert "WEEKLY · FABLE" in texts and "93%" in texts
    assert any(t.startswith("resets in ") for t in texts)
    assert dash.mini.scoped_rows.keys() == ["weekly:Fable"]
    assert any(t.startswith("Fable · resets in ") for t in _texts(dash.mini.scoped_rows))
    assert "Weekly · Fable" in dash.mini.toolTip()

    check.setChecked(False)
    assert dash.scoped_rows.keys() == [] and dash.scoped_rows.isHidden()


def test_failed_usage_request_keeps_rows_and_settings(dash):
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    dash._on_sample(_sample(_fable(93)))
    dash._on_sample(_sample(None))             # usage endpoint failed this poll
    assert dash.scoped_rows.keys() == ["weekly:Fable"]
    assert "93%" in _texts(dash.scoped_rows)
    # Every poll redraws the mini too; its rows must survive that, not blink
    # out until the next 1s countdown tick puts them back.
    assert dash.mini.scoped_rows.keys() == ["weekly:Fable"]
    # The Stats page lists every reported window, ticked or not.
    assert dash.stat_windows._rows == [("Weekly · Fable", 93)]
    assert "— not reported" not in dash.settings_panel._scoped_checks["weekly:Fable"].text()

    dash._on_sample(_sample([]))               # genuinely no longer reported
    assert dash.scoped_rows.keys() == []
    assert dash.settings_panel._scoped_checks["weekly:Fable"].text().endswith(
        "not reported right now")


def test_shelf_mascots_fit_when_launched_with_a_row_ticked(dash):
    # Launch with a limit already ticked: the row is in place before the first
    # show, and the shelf sized its mascot for a viewport it never got —
    # measured 389px of tile in a 240px viewport, the mascot's name cut off.
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    dash._on_sample(_sample(_fable(93)))
    dash.resize(812, 572)
    dash.show()
    _settle()
    shelf = dash.shelf
    assert shelf.isVisible() and shelf._tiles, "mock mode should show a session"
    assert dash.scoped_rows.isVisible()
    tile = next(iter(shelf._tiles.values()))
    viewport = shelf._scroll.viewport().height()
    assert tile.height() <= viewport, (tile.height(), viewport)

    # And the row lines up with WEEKLY above it (a widget's layout brings
    # default margins that the bare SESSION/WEEKLY layouts don't have).
    title = dash.scoped_rows._rows["weekly:Fable"][1][0]
    page = dash.dashboard_page
    assert (title.mapTo(page, title.rect().topLeft()).x()
            == dash.weekly_title.mapTo(page, dash.weekly_title.rect().topLeft()).x())


def test_mini_width_follows_a_longer_reset_text(dash):
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    (w,) = _fable(50, hours=0.2)
    dash._on_sample(_sample([w]))
    dash._set_view_mode("mini")
    try:
        _settle()
        reset = dash.mini.scoped_rows._rows["weekly:Fable"][1][1]
        # Same rows, a much longer reset text: nothing but the text changes.
        dash.mini.set_resets(100, 3000, [(w, 3 * 24 * 60 + 22 * 60, 80)])
        assert reset.text() == "Fable · resets in 3d 22h"
        assert reset.width() >= reset.sizeHint().width(), "reset text clipped"
    finally:
        dash._set_view_mode("full")


def test_compact_view_keeps_rows_across_polls(dash):
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    dash._set_view_mode("compact")
    try:
        for pct in (93, 94):
            dash._on_sample(_sample(_fable(pct)))
            assert dash.compact_view.scoped_rows.keys() == ["weekly:Fable"]
        assert "94%" in _texts(dash.compact_view.scoped_rows)
    finally:
        dash._set_view_mode("full")


def test_scoped_countdown_matches_weekly_between_polls(dash):
    # A scoped window resetting with the weekly one must count down with it,
    # not freeze at the value from the last poll (which can be an hour old
    # under idle back-off).
    app_settings.set_show_token_usage(False)
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    polled_at = time.time() - 17 * 60
    (w,) = _fable(50, hours=3)   # under a day, so minutes are displayed
    s = _sample([w])
    s.timestamp = polled_at
    s.weekly_reset_minutes = w.reset_minutes(polled_at)
    dash._on_sample(s)
    dash._tick_countdown()
    reset = dash.scoped_rows._rows["weekly:Fable"][1][3]
    assert reset.text() == dash.weekly_reset.text()
    assert reset.text() != f"resets in {dashboard._format_minutes(w.reset_minutes(polled_at))}"


def test_overage_past_100_renders_red_like_the_main_bars(dash):
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()
    dash._on_sample(_sample(_fable(120)))
    label, pct, bar, _reset = dash.scoped_rows._rows["weekly:Fable"][1]
    # OVER LIMIT, not OVERAGE: OVERAGE means paid credits, unverified here.
    assert "OVER LIMIT" in label.text() and "OVERAGE" not in label.text()
    assert pct.text() == "120%"
    assert (bar._value, bar._overage) == (0, 20)


def _settle():
    for _ in range(5):
        _app.processEvents()


def test_compact_view_grows_and_shrinks_with_its_rows():
    # On a SHOWN window: a hidden top-level's size hint is stale regardless,
    # and the compact view is only ever updated while it is visible.
    cv = session_shelf.CompactView()
    try:
        s = _sample(None)
        cv.update_usage(s, 100, 3000, False)
        cv.show()
        _settle()
        bare = cv.height()
        ws = _fable(55) + _fable(20, name="Opus")
        cv.update_usage(s, 100, 3000, False, [(w, 1800, 80) for w in ws])
        assert cv.scoped_rows.keys() == ["weekly:Fable", "weekly:Opus"]
        assert cv.height() > bare + 60
        assert "WEEKLY · FABLE" in _texts(cv.scoped_rows)
        assert "resets in 1d 06h" in _texts(cv.scoped_rows)
        _settle()

        cv.update_usage(s, 100, 3000, False, [])
        # The regression: re-fitting on stale layout hints grew the window but
        # left it at full height when the rows went away.
        assert cv.height() == bare
    finally:
        cv.hide()
        cv.deleteLater()


def test_missing_reset_time_is_said_not_shown_as_zero():
    (w,) = windows_from_limits([{"group": "weekly", "percent": 5,
                                 "scope": {"model": {"display_name": "Opus"}}}])
    assert session_shelf.scoped_reset_text(w, 0) == "reset time not reported"


# --- Settings ---------------------------------------------------------------

def test_settings_empty_state_then_checkboxes():
    host = QWidget()
    p = dashboard.SettingsPanel(host, lambda *_a: None, lambda *_a: None)
    try:
        assert p._scoped_checks == {}
        assert p._SCOPED_EMPTY_TEXT in _texts(p._scoped_box)
        app_settings.set_scoped_shown(["weekly:Opus"])
        p.set_scoped_windows([("weekly:Fable", "Weekly · Fable"),
                              ("weekly:Opus", "Weekly · Opus")], ["weekly:Fable"])
        _app.processEvents()
        boxes = p._scoped_box.findChildren(QCheckBox)
        assert [b.text() for b in boxes] == [
            "Weekly · Fable", "Weekly · Opus — not reported right now"]
        assert [b.isChecked() for b in boxes] == [False, True]
    finally:
        p.deleteLater()
        host.deleteLater()


# --- alerts end to end -------------------------------------------------------

def test_scoped_alert_wording(dash, monkeypatch):
    sent = []
    monkeypatch.setattr(dash, "_deliver_alert", lambda title, body: sent.append((title, body)))
    app_settings.set_approaching_enabled(True)
    app_settings.set_approaching_weekly_pct(80)
    app_settings.set_overage_alert_enabled(True)
    app_settings.set_scoped_shown(["weekly:Fable"])
    dash._apply_scoped_view()

    dash._on_sample(_sample(_fable(70)))       # first sight: primes
    dash._on_sample(_sample(_fable(85)))
    dash._on_sample(_sample(_fable(100)))
    assert sent == [
        ("Approaching Claude limit", "Weekly · Fable is at 85% of your limit."),
        ("Claude limit reached", "Weekly · Fable reached 100% of its limit."),
    ]


def test_unticked_window_never_alerts(dash, monkeypatch):
    sent = []
    monkeypatch.setattr(dash, "_deliver_alert", lambda title, body: sent.append(title))
    app_settings.set_approaching_enabled(True)
    for pct in (70, 85, 100):
        dash._on_sample(_sample(_fable(pct)))
    assert sent == []
