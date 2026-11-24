"""Entry point for Clawdmeter."""

from __future__ import annotations

import logging
import os
import sys

from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtWidgets import QApplication

import app_settings
import pricing_refresh
import macos_launch
import netconfig
import run_at_startup
import single_instance
import theme
from dashboard import Dashboard, apply_theme
from sprite_player import assets_root


TRAY_GRACE_MS = 5000     # how long a sign-in launch waits for a tray host
TRAY_STEP_MS = 500       # re-poll interval within that grace period


def show_unless_tray_appears(win, *, grace_ms: int = TRAY_GRACE_MS,
                             step_ms: int = TRAY_STEP_MS, available=None) -> None:
    """Pop the window only if there is STILL no system tray after a short wait.

    A sign-in launch belongs in the tray. Where there is no tray to stay in --
    some Linux DEs have none -- staying hidden would leave the app invisible and
    unrecoverable, so the window has to show instead.

    The trap is that at LOGIN the app can beat the panel's tray host to the
    socket, so the snapshot taken in ``Dashboard.__init__`` reads "no tray" on a
    desktop that is about to have one, and acting on it pops a window at every
    single login. Re-poll instead of trusting that snapshot -- the same reason
    ``Dashboard.closeEvent`` re-queries rather than reusing it.

    Nothing has to be done when a host does turn up: the tray icon is already
    ``show()``n, so it docks by itself. All this has to get right is *not*
    showing the window.

    Only the no-tray answer is delayed. A DE that genuinely has none waits
    ``grace_ms`` before its window appears, which is nothing against a login.

    ``available`` is injectable so the wait can be tested without a real tray.
    """
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QSystemTrayIcon

    if available is None:
        available = QSystemTrayIcon.isSystemTrayAvailable

    if available():          # already up: the snapshot was simply stale
        win.tray_available = True
        return

    waited = {"ms": 0}
    timer = QTimer()         # unparented; the reference below keeps it alive
    timer.setInterval(step_ms)

    def _tick() -> None:
        if available():
            win.tray_available = True
            timer.stop()
            return
        waited["ms"] += step_ms
        if waited["ms"] >= grace_ms:
            timer.stop()
            win.show_initial()

    timer.timeout.connect(_tick)
    timer.start()
    win._tray_wait_timer = timer
    return


def main() -> int:
    mock = "--mock" in sys.argv
    startup = run_at_startup.STARTUP_FLAG in sys.argv  # launched at sign-in
    # Before anything makes a request: trust the system certificate store and
    # pick up the proxy Claude Code or the system uses, so work networks with
    # an inspecting proxy don't read as "offline". See netconfig.py.
    netconfig.apply()
    app = QApplication(sys.argv)
    # The OS colour-scheme hint (for native / un-QSS'd surfaces — notably macOS
    # native text) is set to match the active theme by apply_theme() below, so a
    # light theme renders correctly on every platform.
    app.setApplicationName("Clawdmeter")
    app.setOrganizationName(app_settings.ORG)
    # Ties the app to packaging/clawdmeter.desktop so Wayland uses its icon
    # (the app_id must match the .desktop basename). No-op on Windows.
    app.setDesktopFileName("clawdmeter")
    app.setQuitOnLastWindowClosed(False)  # tray keeps app alive

    # Single instance: if a copy is already running, surface its window and
    # exit instead of starting a duplicate process that lingers in the tray.
    if single_instance.activate_running_instance():
        return 0

    # macOS: fold any legacy LaunchAgent plist into an SMAppService registration
    # (no-op elsewhere, and on a Mac that never had one). After the
    # single-instance check so only the surviving process touches it. Guarded so
    # an unexpected SMAppService/bridge failure can never crash the launch before
    # the window is even built — worst case, the migration just doesn't run.
    try:
        run_at_startup.migrate_macos_login_item()
    except Exception:   # noqa: BLE001 - a startup migration must never crash launch
        logging.getLogger(__name__).exception(
            "Login-item migration failed; continuing without it")

    # Apply persisted credentials override before the poller starts.
    cred = app_settings.get_credentials_override()
    if cred:
        os.environ["CLAUDE_CREDENTIALS_PATH"] = cred

    # Prefer a prior session's live pricing refresh over the build-time bundled
    # map, before Dashboard (and Stats) ever reads pricing.
    pricing_refresh.apply_cached_override()

    icon_path = assets_root() / "icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    # Register bundled icon fonts (Font Awesome 6 Free Solid) before any window
    # builds, so glyph-as-text labels resolve. Family: "Font Awesome 6 Free".
    fa_path = assets_root() / "fonts" / "fa-solid-900.ttf"
    if fa_path.exists():
        QFontDatabase.addApplicationFont(str(fa_path))

    # Apply the saved appearance theme before building the window so it comes up
    # already themed (no flash). With no widgets yet, apply_theme just sets the
    # active palette and refreshes the module colour caches. Load the custom
    # theme's base colours first so a saved "Custom" selection resolves.
    saved_custom = app_settings.get_custom_base()
    if saved_custom:
        theme.set_custom_base(saved_custom)
    sys_dark, sys_light = app_settings.get_system_targets()
    if sys_dark or sys_light:
        theme.set_system_targets(sys_dark or theme.SYSTEM_DARK,
                                 sys_light or theme.SYSTEM_LIGHT)
    try:
        apply_theme(app_settings.get_theme())
    except Exception:   # noqa: BLE001 - a broken saved/custom theme must never
        # keep the app from launching; fall back to the built-in default.
        logging.getLogger(__name__).exception(
            "Saved theme failed to apply; falling back to the default theme")
        app_settings.set_theme(theme.DEFAULT_NAME)
        apply_theme(theme.DEFAULT_NAME)

    win = Dashboard(mock=mock)

    def _begin(startup: bool) -> None:
        """Show the window, or stay in the tray for a sign-in launch."""
        if startup:
            # Sign-in launch: stay in the tray (don't pop the window). The
            # poller, update checker and transcript watcher already start in
            # __init__.
            run_at_startup.sync_if_enabled()  # keep the entry pointed at this .exe
            # ...unless there's no system tray to stay in (some Linux DEs): a
            # hidden tray-less launch would be invisible and unrecoverable, so
            # show the window instead. Gated off Windows so the Windows sign-in
            # path is provably unchanged (it always has a tray regardless).
            if sys.platform != "win32" and not getattr(win, "tray_available", True):
                show_unless_tray_appears(win)
        else:
            win.show_initial()   # launch directly into the last-used view mode

    if sys.platform != "darwin":
        _begin(startup)
    else:
        # macOS: SMAppService launches us with no arguments, so --startup never
        # arrives and the sign-in launch is only identifiable from the Apple
        # Event delivered during exec(). Defer the decision until it lands
        # rather than showing the dashboard at every login. macos_launch always
        # answers — via the notification, or a fallback timer — so this cannot
        # strand the app windowless. `known` covers the LaunchAgent fallback,
        # which does still pass --startup.
        #
        # on_reopen is wired here too because it has to be installed from the
        # same launch notification, and it matters most in exactly the state
        # this change creates: running in the menu bar with no window.
        macos_launch.detect(_begin, on_reopen=win._show_window,
                            known=True if startup else None)

    # Listen for later launches so they surface this window instead of
    # spawning a duplicate. Kept on `app` so it isn't garbage-collected.
    app._instance_server = single_instance.InstanceServer(on_show=win._show_window)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
