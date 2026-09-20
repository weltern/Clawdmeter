"""Offscreen tests for the tabbed settings panel.

Builds a SettingsPanel and asserts the full-width tab refactor: six tabs in
the expected order, every section routed onto the tab that owns its concern,
and the prominent close affordance present. Runs headless via
QT_QPA_PLATFORM=offscreen so it works in CI with no display.

Run with `python -m pytest tests/ -q`, or directly:
`python tests/test_settings_tabs.py`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from dashboard import SettingsPanel  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _panel() -> SettingsPanel:
    # Top-level (parent=None) so the returned panel owns itself and survives the
    # call. Settings is now a stacked page (no overlay/close), so the two
    # required callbacks are all it needs.
    noop = lambda *a, **k: None
    return SettingsPanel(None, noop, noop)


def test_six_tabs_in_order():
    sp = _panel()
    assert sp._stack.count() == 6
    assert len(sp._nav_group.buttons()) == 6
    labels = [sp._nav_group.button(i).text() for i in range(6)]
    assert [t.split()[-1] for t in labels] == [
        "General", "Display", "Appearance", "Connection", "Notifications", "About",
    ]
    # First tab is selected by default.
    assert sp._nav_group.button(0).isChecked()


def test_every_page_is_populated():
    sp = _panel()
    for i in range(sp._stack.count()):
        body = sp._stack.widget(i).widget()
        assert body.findChildren(QWidget), f"tab {i} has no widgets"


def test_sections_routed_to_expected_tabs():
    sp = _panel()
    page = {  # tab index by concern (Appearance inserted at index 2)
        "general": sp._stack.widget(0),
        "display": sp._stack.widget(1),
        "appearance": sp._stack.widget(2),
        "connection": sp._stack.widget(3),
        "notifications": sp._stack.widget(4),
    }
    expected = {
        "general": [sp.aot_check, sp.auto_hide_check, sp.quit_on_close_check,
                    sp.startup_check, sp.auto_check_updates_check, sp.start_btn],
        "display": [sp.multi_sessions_check, sp.subagents_check, sp.token_usage_check],
        "appearance": sp._theme_options,
        "connection": [sp.cred_btn, sp.auto_refresh_check, sp.refresh_token_btn,
                       sp.poll_interval_edit, sp.idle_backoff_check,
                       sp.idle_after_spin, sp.idle_interval_spin],
        "notifications": [sp.notify_check, sp.approaching_check,
                          sp.session_pct_slider, sp.session_pct_field,
                          sp.weekly_pct_slider, sp.weekly_pct_field, sp.overage_check,
                          sp.notify_toast_check, sp.notify_push_check,
                          sp.notify_push_add_btn],
    }
    for tab, widgets in expected.items():
        for w in widgets:
            assert page[tab].isAncestorOf(w), f"{w.objectName() or w} not on {tab} tab"


def _set_silently(checkbox, on):
    # Flip a checkbox WITHOUT firing its toggled handler, so the test never
    # persists a setting (e.g. enabling approaching alerts) into HKCU.
    checkbox.blockSignals(True)
    checkbox.setChecked(on)
    checkbox.blockSignals(False)


def test_approaching_threshold_box_follows_its_master():
    sp = _panel()
    _set_silently(sp.approaching_check, True)
    sp._sync_notify_subtoggles()
    assert not sp.approaching_box.isHidden()
    _set_silently(sp.approaching_check, False)
    sp._sync_notify_subtoggles()
    assert sp.approaching_box.isHidden()


def test_shared_channels_show_for_any_alert_and_hide_when_all_off():
    sp = _panel()
    # Reset on, the others off -> channels shown.
    _set_silently(sp.notify_check, True)
    _set_silently(sp.approaching_check, False)
    _set_silently(sp.auth_notify_check, False)
    sp._sync_notify_subtoggles()
    assert not sp.notify_how_box.isHidden()
    # Only approaching on -> still shown (channels are shared).
    _set_silently(sp.notify_check, False)
    _set_silently(sp.approaching_check, True)
    sp._sync_notify_subtoggles()
    assert not sp.notify_how_box.isHidden()
    # Only the auth alert on -> still shown. It delivers through these same
    # channels, so hiding them left it firing through controls nobody could see.
    _set_silently(sp.approaching_check, False)
    _set_silently(sp.auth_notify_check, True)
    sp._sync_notify_subtoggles()
    assert not sp.notify_how_box.isHidden()
    # Everything off -> channels hidden as a unit.
    _set_silently(sp.auth_notify_check, False)
    sp._sync_notify_subtoggles()
    assert sp.notify_how_box.isHidden()


def test_threshold_slider_and_field_stay_in_sync():
    import app_settings
    # Swap the setter to a no-op so driving the widgets never writes real settings.
    real = app_settings.set_approaching_session_pct
    app_settings.set_approaching_session_pct = lambda v: None
    try:
        sp = _panel()
        sp.session_pct_slider.setValue(72)
        assert sp.session_pct_field.value() == 72   # slider -> field
        sp.session_pct_field.setValue(64)
        assert sp.session_pct_slider.value() == 64   # field -> slider (no loop)
    finally:
        app_settings.set_approaching_session_pct = real


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


def test_ticking_the_auth_alert_reveals_the_delivery_channels(monkeypatch):
    """Drives the REAL toggled signal, not _set_silently.

    The handler is what has to call _sync_notify_subtoggles; a test that calls
    the sync itself would pass with the handler's call deleted. The setter is
    swapped for a no-op so driving the widget never writes into HKCU.
    """
    import app_settings
    monkeypatch.setattr(app_settings, "set_auth_notify", lambda *_a: None)
    monkeypatch.setattr(app_settings, "set_approaching_enabled", lambda *_a: None)
    monkeypatch.setattr(app_settings, "set_reset_notify", lambda *_a: None)

    sp = _panel()
    _set_silently(sp.notify_check, False)
    _set_silently(sp.approaching_check, False)
    _set_silently(sp.auth_notify_check, False)
    sp._sync_notify_subtoggles()
    assert sp.notify_how_box.isHidden(), "control: hidden with every alert off"

    sp.auth_notify_check.setChecked(True)      # real signal -> real handler

    assert not sp.notify_how_box.isHidden()


def test_the_panel_never_blocks_on_a_credential_read(monkeypatch):
    """Built on the UI thread, so the macOS Keychain read must not be waited on.

    This call sat eleven lines under a comment explaining exactly that, and
    blocked anyway.
    """
    import token_refresh
    seen = []
    monkeypatch.setattr(token_refresh, "token_expiry_ms", lambda _p, **kw: None)
    real = token_refresh.is_expired
    monkeypatch.setattr(token_refresh, "is_expired",
                        lambda p, *a, **kw: (seen.append(kw.get("blocking", True)),
                                             real(p, *a, **kw))[1])
    monkeypatch.setattr(token_refresh, "_macos_keychain_active", lambda: False)

    _panel()

    assert seen, "control: is_expired was never called, so this proves nothing"
    assert all(b is False for b in seen), f"blocking read on the UI thread: {seen}"
