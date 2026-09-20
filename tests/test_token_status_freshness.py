"""The token-validity line must reflect the token, not a snapshot of it.

Found by the second platform review. refresh_token_status() ran once during
SettingsPanel construction and then, on macOS, essentially never again: a
real token refresh re-renders it, and macOS cannot perform one (the refresh
button and the auto-refresh checkbox are both disabled there, deliberately).
An earlier fix filled the line once the poller had warmed the Keychain cache
and latched -- which cured a blank line but kept the staleness. The result was
a countdown that could read "Valid for ~7h 58m" hours after the token had in
fact expired and the bars had gone blank, which is precisely when a user opens
Settings to find out what is wrong.

The line is now recomputed when the Connection tab is opened, when Settings is
re-entered with that tab already current, and on each sample while it is the
page actually on screen. All three matter: the first two are the user asking,
and the third keeps it honest for someone sitting on the page watching.

Windows is not affected by the underlying staleness -- a real refresh
re-renders there -- but it gets the same freshening, so these tests cover it
too.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import app_settings  # noqa: E402
import dashboard  # noqa: E402
import token_refresh  # noqa: E402

_app = QApplication.instance() or QApplication([])
app_settings.set_theme = lambda name: None


class _Clock:
    """A token whose expiry the test can move."""

    def __init__(self, secs_left):
        self.secs_left = secs_left

    def expiry_ms(self, _path, **_kw):
        return (time.time() + self.secs_left) * 1000

    def expired(self, _path, **_kw):
        return self.secs_left <= 0


@pytest.fixture
def panel(monkeypatch, request):
    keychain = getattr(request, "param", True)
    clock = _Clock(4 * 3600)
    monkeypatch.setattr(token_refresh, "_macos_keychain_active", lambda: keychain)
    monkeypatch.setattr(token_refresh, "token_expiry_ms", clock.expiry_ms)
    monkeypatch.setattr(token_refresh, "is_expired", clock.expired)
    host = QWidget()
    p = dashboard.SettingsPanel(host, lambda *_a: None, lambda *_a: None)
    p._clock = clock
    try:
        yield p
    finally:
        p.deleteLater()
        host.deleteLater()


def _open_connection(panel):
    panel._stack.setCurrentIndex(panel._tab_index["Connection"])


def _open_some_other_tab(panel):
    panel._stack.setCurrentIndex(panel._tab_index["About"])


@pytest.mark.parametrize("panel", [True, False], indirect=True)
def test_reopening_connection_rerenders_an_expired_token(panel):
    """The regression itself, on both credential backends."""
    _open_connection(panel)
    first = panel.token_status.text()
    assert "Valid for" in first, first

    panel._clock.secs_left = -60          # the token dies while Settings sits open
    _open_some_other_tab(panel)
    _open_connection(panel)

    assert panel.token_status.text() != first, \
        "the line is still showing a countdown that expired"
    assert "expired" in panel.token_status.text().lower()


def test_re_entering_settings_on_connection_rerenders(panel):
    """Leaving Settings and returning does not change the sub-tab index, so
    _stack.currentChanged stays silent and only on_shown() can catch it."""
    _open_connection(panel)
    first = panel.token_status.text()

    panel._clock.secs_left = -60
    panel.on_shown()                      # what Dashboard._show_page(2) calls

    assert panel.token_status.text() != first
    assert "expired" in panel.token_status.text().lower()


def test_connection_tab_is_current_tracks_the_stack(panel):
    _open_some_other_tab(panel)
    assert panel.connection_tab_is_current() is False
    _open_connection(panel)
    assert panel.connection_tab_is_current() is True


def test_the_tab_index_is_recorded_by_name_not_position(panel):
    """Nothing may hard-code Connection's position; the order has changed
    before, and a stale integer would silently freshen the wrong page."""
    assert set(panel._tab_index) >= {
        "General", "Display", "Appearance", "Connection", "Notifications", "About"}
    for label, idx in panel._tab_index.items():
        panel._stack.setCurrentIndex(idx)
        assert panel.connection_tab_is_current() == (label == "Connection"), label


class _FakeDashboard:
    """Dashboard's freshening gate, without starting pollers or watchers."""

    _refresh_token_status_if_watched = dashboard.Dashboard._refresh_token_status_if_watched

    def __init__(self, *, page_idx, connection_current):
        self.calls = 0
        outer = self

        class _Pages:
            def currentIndex(self):
                return page_idx

        class _Panel:
            def connection_tab_is_current(self):
                return connection_current

            def showing_transient_token_status(self):
                return False      # no refresh failure on screen

            def refresh_token_status(self, *, preserve_message=False):
                outer.calls += 1
                outer.preserved = preserve_message

        self._pages, self.settings_panel = _Pages(), _Panel()


def test_the_freshening_is_wired_into_the_dashboard():
    """on_shown() and the sample hook are both called directly by the tests
    above, so all of them would still pass if Dashboard never invoked them.
    Source inspection, because building a real Dashboard starts pollers and
    file watchers and leaks global state into other test modules.
    """
    import inspect
    show = inspect.getsource(dashboard.Dashboard._show_page)
    assert "self.settings_panel.on_shown()" in show, \
        "re-entering Settings no longer refreshes the token line"
    sample = inspect.getsource(dashboard.Dashboard._on_sample)
    assert "self._refresh_token_status_if_watched()" in sample, \
        "a watched token line no longer updates as samples arrive"


def test_a_sample_freshens_the_line_while_it_is_on_screen():
    d = _FakeDashboard(page_idx=2, connection_current=True)
    d._refresh_token_status_if_watched()
    assert d.calls == 1


def test_a_refresh_failure_is_not_wiped_by_the_next_sample(panel):
    """The reason a refresh failed is the only diagnostic the app produces.

    UsagePoller emits `sample` immediately after `refresh_status` in the same
    cycle, so the freshening landed about a second after the error and replaced
    it with the generic "Token expired -- refresh now" -- telling the user to
    retry the thing that had just failed, with the reason already gone. This is
    a Windows regression: the old once-per-process latch returned early off
    macOS, so `_on_sample` never touched the label there.
    """
    _open_connection(panel)
    panel.set_token_status("⚠ Rate limited by token endpoint — backing off")
    assert panel.showing_transient_token_status() is True

    d = _FakeDashboard(page_idx=2, connection_current=True)
    d.settings_panel = panel                 # the real panel, not the stub
    d._refresh_token_status_if_watched()

    assert "Rate limited" in panel.token_status.text(), \
        "the failure reason was overwritten before the user could read it"


@pytest.mark.parametrize("panel", [False], indirect=True)   # file-backed, i.e. Windows
def test_a_refresh_failure_does_not_freeze_the_controls(panel):
    """Holding the MESSAGE must not hold the button and checkbox with it.

    Windows specifically: on macOS the refresh button is disabled permanently
    and by design ("Managed by the macOS Keychain"), so it cannot show this.

    The first attempt protected the message by skipping refresh_token_status()
    entirely -- which also froze refresh_token_btn and auto_refresh_check. A
    token expiring while the user sat on Connection then left "⚠ Rate limited"
    directly above a greyed-out button captioned "Token valid — refresh
    disabled", with the one in-app remedy unclickable.
    """
    _open_connection(panel)
    panel._clock.secs_left = 4 * 3600
    panel.refresh_token_status()
    assert panel.refresh_token_btn.isEnabled() is False, "precondition: token valid"

    panel._clock.secs_left = -60                 # token expires
    panel.set_token_status("⚠ Rate limited by token endpoint — backing off")
    panel.refresh_token_status(preserve_message=True)

    assert "Rate limited" in panel.token_status.text(), "the reason was lost"
    assert panel.refresh_token_btn.isEnabled() is True, \
        "the retry button is still greyed out while the token is expired"
    assert panel.refresh_token_btn.text() == "Refresh token now", \
        f"button still says {panel.refresh_token_btn.text()!r}"


def test_preserve_message_keeps_the_flag_set(panel):
    """Otherwise the next sample would clear it and wipe the message anyway."""
    _open_connection(panel)
    panel.set_token_status("⚠ something failed")
    panel.refresh_token_status(preserve_message=True)
    assert panel.showing_transient_token_status() is True


def test_the_user_reopening_connection_clears_a_stale_failure(panel):
    """It must not stick forever -- navigating back is the user asking again."""
    _open_connection(panel)
    panel.set_token_status("⚠ Rate limited by token endpoint — backing off")
    _open_some_other_tab(panel)
    _open_connection(panel)
    assert "Rate limited" not in panel.token_status.text()
    assert panel.showing_transient_token_status() is False


@pytest.mark.parametrize("page_idx,connection", [
    (0, True),      # on the Dashboard page
    (1, True),      # on Stats
    (2, False),     # in Settings, but reading a different tab
])
def test_a_sample_does_no_work_when_the_line_is_not_visible(page_idx, connection):
    """Cheap, but not free -- and it must not fire 60s after the user left."""
    d = _FakeDashboard(page_idx=page_idx, connection_current=connection)
    d._refresh_token_status_if_watched()
    assert d.calls == 0
