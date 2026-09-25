"""Settings must keep knowing that automatic refresh has stopped.

Found in the 3.1.0 review. The Dashboard handed Settings
`status == reauth-needed` on EVERY sample, so an offline or API-error poll in
the middle of a dead-refresh-token outage cleared the flag, and the Connection
tab went back to "refresh now, or wait for auto-refresh" while no refresh was
coming. Only a sample that actually says something about the token may move it.

Runs a real Dashboard in mock mode headless with its timers stopped and
QSettings on a throwaway ini. Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app_settings  # noqa: E402
import dashboard  # noqa: E402
import poller  # noqa: E402
from poller import UsageSample  # noqa: E402

_app = QApplication.instance() or QApplication([])
app_settings.set_theme = lambda name: None


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    store = QSettings(str(tmp_path / "t.ini"), QSettings.IniFormat)
    monkeypatch.setattr(app_settings, "_settings", lambda: store)
    yield store


@pytest.fixture
def dash():
    app_settings.set_reset_notify(False)
    app_settings.set_auth_notify(False)
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


def _bad(status):
    return UsageSample(0, 0, 0, 0, status, False, "boom", time.time())


def _good():
    return UsageSample(10, 100, 40, 3000, "allowed", True, None, time.time())


@pytest.mark.parametrize("status", [poller.STATUS_OFFLINE, poller.STATUS_HTTP_ERROR])
def test_a_network_or_api_failure_does_not_forget_it(dash, status):
    dash._on_sample(_bad(poller.STATUS_REAUTH_NEEDED))
    assert dash.settings_panel._reauth_needed

    dash._on_sample(_bad(status))

    assert dash.settings_panel._reauth_needed


def test_a_working_poll_clears_it(dash):
    dash._on_sample(_bad(poller.STATUS_REAUTH_NEEDED))
    dash._on_sample(_bad(poller.STATUS_OFFLINE))

    dash._on_sample(_good())

    assert not dash.settings_panel._reauth_needed
