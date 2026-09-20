"""Main dashboard window for Clawdmeter.

Frameless top-level window with a custom title bar (drag-to-move, in-app
min/max/close buttons), a sprite player driven by Claude usage rate, and a
slide-in settings panel on the right.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QParallelAnimationGroup,
    QPropertyAnimation,
    QPoint,
    QRect,
    QRegularExpression,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
    QRegularExpressionValidator,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import app_settings
import macos_keychain
import poll_cadence
import reauth
import run_at_startup
import start_menu
import token_refresh
import winutil
from mood import GROUP_ANIMS, GROUP_NAMES, RateGroupTracker
from poller import (
    UsagePoller, UsageSample, credentials_path, DEFAULT_CREDENTIALS_PATH,
    token_source_description,
    STATUS_AUTH_EXPIRED, STATUS_HTTP_ERROR, STATUS_NO_TOKEN, STATUS_OFFLINE,
    STATUS_REAUTH_NEEDED,
)
import macos_window
import remote_notify
import stats
import statviz
from statviz import CategoryBars, DailyBars, Heatmap, ModelBreakdown, PercentBars, WeekBars
import theme
from color_picker import ColorPicker
from usage_history import UsageHistory
from approaching_notify import ApproachingNotifier
from auth_notify import AuthNotifier
from reset_notify import ResetDecision, ResetNotifier
import update_check
from update_check import UpdateChecker
from pricing_refresh import PricingRefresher
import session_shelf
from session_shelf import (
    SCOPED_OVER_TAG, CompactView, ScopedRows, SessionShelf, UsageBar,
    apply_overage_bar, scoped_reset_text,
)
import scoped_windows
from sprite_player import SpritePlayer, assets_root
from transcript import (
    ACTIVITY_ANIMS,
    ACTIVITY_COLORS,
    ACTIVITY_LABELS,
    Activity as TranscriptActivity,
    AgentState as TranscriptAgentState,
    TokenUsage,
    TranscriptState,
    TranscriptWatcher,
    account_window_tokens,
    fmt_tokens,
)
from uiutil import (ThemedPopup, bar_warn_thresholds, is_wayland, make_popup,
                    format_minutes as _format_minutes, heat as _heat)


# What the bottom-left badge says when a poll FAILS: {status: (text, icon, level)}.
# `level` drives the QSS colour and only "warn" (amber) and "block" (red) are
# styled, so nothing here may invent a third one.
#
# The split matters more than the wording: "Token expired" points at a fix the
# user can carry out, "Offline" says to wait. Collapsing both into one message
# is what this table exists to prevent.
FAILURE_BADGES = {
    STATUS_AUTH_EXPIRED: ("Token expired", "🔑", "block"),
    STATUS_REAUTH_NEEDED: ("Sign in again", "🔑", "block"),
    STATUS_NO_TOKEN: ("No token found", "🔑", "block"),
    STATUS_HTTP_ERROR: ("API error", "⚠️", "warn"),
    STATUS_OFFLINE: ("Offline", "⚠️", "warn"),
}


# Stable tile id used in single-mascot mode (Settings: show multiple sessions
# off) so the one tile updates in place rather than animating a swap when the
# focused session changes.
_SINGLE_TILE_ID = "__single__"


def _view_states(raw, show_multiple, show_subagents, single_id=_SINGLE_TILE_ID):
    """Apply the Settings session-view toggles to the watcher's raw states.

    - show_subagents off -> strip each session's child agents.
    - show_multiple off  -> keep only the focused (newest) session, re-keyed to a
      stable id so its tile updates in place instead of animating a swap when the
      focused session changes.
    Pure (returns a new list) so it's testable without the Qt UI.
    """
    states = raw
    if not show_subagents:
        states = [replace(s, agents=[]) for s in states]
    if not show_multiple and states:
        states = [replace(states[0], session_id=single_id)]
    return states


def _should_persist_size(fit_armed: bool, fitting: bool) -> bool:
    """Whether a resize should schedule a save of the window size.

    `fit_armed` is False until the first show has settled, so construction and
    the startup content snap cannot overwrite the very size just restored.
    `fitting` is True during our own snap animation; that height IS worth
    saving, but resizeEvent is the wrong place to notice it -- the animation's
    final QResizeEvent arrives before _on_fit_anim_finished flips the flag, so
    the snap is persisted from there instead.

    A module-level predicate rather than an inline condition so it can be
    tested directly. Inlined, the whole save path had no coverage at all:
    deleting the _real_quit() flush, dropping the timer connection, or forcing
    this gate false each left the entire feature broken with the suite green.
    """
    return fit_armed and not fitting


# Valid view modes, largest -> smallest.
VIEW_ORDER = ("full", "compact", "mini")

# The empty-state (no sessions) mascot's size when the window has room for it.
HERO_MASCOT_MAX = 240


# The frameless windows draw a 1px #root border to define their edge -- good on
# Windows/Linux, but on macOS it reads as a stray dark line around the window
# (the OS already gives frameless windows a shadow). Make it transparent there.
_ROOT_BORDER_OVERRIDE = (
    "\nQWidget#root { border-color: transparent; }" if sys.platform == "darwin" else "")


STYLESHEET = theme.build_qss(theme.active()) + _ROOT_BORDER_OVERRIDE


_applying_theme = False


def apply_theme(selected: str) -> None:
    """Switch the whole app to ``selected`` and restyle every live widget in
    place — no restart. Persists the choice.

    ``selected`` is a preset name OR ``theme.SYSTEM``. For Follow System we clear
    any colour-scheme override so Qt reports the OS scheme, resolve to the
    matching dark/light preset, and keep tracking it (see the OS-scheme signal
    wired in Dashboard). For a fixed preset we pin the OS colour-scheme hint to
    the palette's light/dark nature so native / un-QSS'd surfaces (menus,
    tooltips, message boxes — notably on macOS) match.

    Safe to call at startup before any widgets exist: it sets the active palette
    and refreshes the module colour caches, so widgets built afterward come up
    themed. On a live switch it walks every existing widget, swaps the old
    stylesheet strings for the freshly-built ones, and repaints the
    custom-painted widgets (which read the module QColors at paint time).
    """
    global STYLESHEET, _applying_theme
    if _applying_theme:
        return
    _applying_theme = True
    try:
        app = QApplication.instance()
        sh = app.styleHints() if app is not None else None

        if selected == theme.SYSTEM:
            # Clear any override so colorScheme() reflects the OS, then resolve.
            if sh is not None:
                sh.setColorScheme(Qt.ColorScheme.Unknown)
            os_dark = sh.colorScheme() != Qt.ColorScheme.Light if sh else True
            concrete = theme.system_target(os_dark)
        elif selected == theme.CUSTOM:
            concrete = theme.CUSTOM
        else:
            concrete = selected if selected in theme.PRESETS else theme.DEFAULT_NAME

        old_base, old_shelf, old_compact = (
            STYLESHEET, session_shelf.SHELF_STYLESHEET, session_shelf.COMPACT_STYLESHEET)

        theme.apply_selection(selected, concrete)
        app_settings.set_theme(theme.selected())
        statviz.refresh_theme()
        session_shelf.refresh_theme()
        STYLESHEET = theme.build_qss(theme.active()) + _ROOT_BORDER_OVERRIDE

        # Fixed preset: pin the OS colour-scheme hint to match. Follow System
        # left its override cleared above so it keeps tracking the OS.
        if sh is not None and selected != theme.SYSTEM:
            sh.setColorScheme(Qt.ColorScheme.Light if theme.is_light(theme.active())
                              else Qt.ColorScheme.Dark)

        if app is None:
            return
        pairs = (
            (old_base, STYLESHEET),
            (old_shelf, session_shelf.SHELF_STYLESHEET),
            (old_compact, session_shelf.COMPACT_STYLESHEET),
        )
        for w in app.allWidgets():
            sheet = w.styleSheet()
            if sheet:
                for old, new in pairs:
                    if sheet == old:
                        w.setStyleSheet(new)
                        break
            # Windows whose stylesheet is the base sheet PLUS a platform append
            # (the macOS rounded-corner rule on mini/compact) never equal `old`,
            # so the exact-match swap above skips them — they rebuild themselves
            # from the freshly-built globals here. Broadcasting it from the walk
            # (rather than from a caller) means no theme entry point can miss it.
            if hasattr(w, "apply_theme_style"):
                w.apply_theme_style()
            # Custom-painted labels that cache a themed colour (session/compact
            # name + "working on" lines) re-read the palette here.
            if hasattr(w, "refresh_theme_color"):
                w.refresh_theme_color()
            w.update()  # repaint custom-painted widgets that read module QColors
    finally:
        _applying_theme = False


def tray_icon(source) -> QIcon:
    """Build the menu-bar / system-tray icon from a QPixmap or a file path.

    On macOS, status-bar icons are expected to be TEMPLATE images: a black
    silhouette plus alpha, which the OS recolours itself — dark on a light menu
    bar, light on a dark one, and inverted while the menu is open. A full-colour
    icon sitting among monochrome system items is the most immediately visible
    "ported from Windows" tell there is, and it does not invert on click.

    Nothing is lost by going monochrome here: the tray shows the Clawd mascot,
    which never encoded the usage percentage (that lives in the tooltip and the
    windows). The colour was decoration, not information.

    Off macOS the icon is returned unchanged — Windows and Linux trays are
    routinely full-colour and a black silhouette would look broken there.
    """
    pm = source if isinstance(source, QPixmap) else QPixmap(str(source))
    if sys.platform != "darwin" or pm.isNull():
        return QIcon(pm)
    # Keep the alpha, replace every colour with black: that IS a template image.
    mask = QPixmap(pm.size())
    mask.setDevicePixelRatio(pm.devicePixelRatio())
    mask.fill(Qt.transparent)
    p = QPainter(mask)
    p.drawPixmap(0, 0, pm)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(mask.rect(), QColor("#000000"))
    p.end()
    icon = QIcon(mask)
    icon.setIsMask(True)   # tells Qt/AppKit to treat it as a template
    return icon


def _tray_pixmap(pct: int) -> QPixmap:
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#1f2937"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    fill = {"cool": "#4f46e5", "warm": "#d97706", "hot": "#dc2626"}[_heat(pct)]
    p.setBrush(QColor(fill))
    span = int(360 * 16 * max(0, min(pct, 100)) / 100)
    p.drawPie(2, 2, 28, 28, 90 * 16, -span)
    p.setBrush(QColor("#0e1116"))
    p.drawEllipse(9, 9, 14, 14)
    p.end()
    return pm


def _tray_alert_pixmap() -> QPixmap:
    """High-contrast green "go / resume" variant used by the reset-flash."""
    pm = QPixmap(32, 32)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor("#22c55e"))
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 28, 28)
    p.setBrush(QColor("#0e1116"))
    p.drawEllipse(9, 9, 14, 14)
    p.end()
    return pm


PUSH_CHANNEL_NAMES = {
    "ntfy": "ntfy", "telegram": "Telegram", "discord": "Discord",
    "slack": "Slack", "pushover": "Pushover", "gotify": "Gotify",
    "webhook": "Webhook",
}


def _active_push_channels() -> list[str]:
    """Added channels that actually have the value(s) needed to send."""
    return [c for c in app_settings.get_reset_notify_push_channels()
            if app_settings.push_channel_configured(c)]


def _push_configured() -> bool:
    """True if at least one added push channel is ready to send."""
    return bool(_active_push_channels())


def _send_one_channel(provider: str, title: str, body: str) -> tuple[bool, str]:
    if provider == "ntfy":
        return remote_notify.send_ntfy(
            app_settings.get_reset_notify_push_topic(), title, body)
    if provider == "telegram":
        return remote_notify.send_telegram(
            app_settings.get_reset_notify_push_tg_token(),
            app_settings.get_reset_notify_push_tg_chat(), title, body)
    if provider == "discord":
        return remote_notify.send_discord(
            app_settings.get_reset_notify_push_discord(), title, body)
    if provider == "slack":
        return remote_notify.send_slack(
            app_settings.get_reset_notify_push_slack(), title, body)
    if provider == "webhook":
        return remote_notify.send_webhook(
            app_settings.get_reset_notify_push_webhook(), title, body)
    if provider == "pushover":
        return remote_notify.send_pushover(
            app_settings.get_reset_notify_push_po_token(),
            app_settings.get_reset_notify_push_po_user(), title, body)
    if provider == "gotify":
        return remote_notify.send_gotify(
            app_settings.get_reset_notify_push_gotify_url(),
            app_settings.get_reset_notify_push_gotify_token(), title, body)
    return False, f"unknown channel {provider}"


def _dispatch_push(title: str, body: str) -> tuple[bool, str]:
    """Send the push to EVERY added+configured channel. Shared by the reset
    notification and the Settings test button so both exercise one code path.
    Returns (all_ok, summary) — partial failures are reported, never raised."""
    channels = _active_push_channels()
    if not channels:
        return False, "no push channels configured"
    sent, failed = [], []
    for c in channels:
        ok, msg = _send_one_channel(c, title, body)
        (sent if ok else failed).append((c, msg))
    names = lambda items: ", ".join(PUSH_CHANNEL_NAMES.get(c, c) for c, _ in items)
    if not failed:
        return True, f"sent to {names(sent)}"
    detail = "; ".join(f"{PUSH_CHANNEL_NAMES.get(c, c)}: {m}" for c, m in failed)
    if sent:
        return False, f"sent to {names(sent)}; failed — {detail}"
    return False, f"failed — {detail}"


class MiniWidget(QWidget):
    """Tiny always-on-top floating readout: mini mascot + session/weekly bars.

    A frameless, draggable tool window with no taskbar entry. Double-click (or
    right-click -> Expand) returns to the full dashboard. The owning Dashboard
    feeds it usage values and sprite animations so it mirrors the main window.
    """

    expand_requested = Signal()
    quit_requested = Signal()

    SPRITE = 37

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("miniRoot")
        self.setWindowTitle("Clawdmeter")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(STYLESHEET)
        if sys.platform == "darwin":
            # macOS: stay OPAQUE (WA_TranslucentBackground makes the panel render
            # fully transparent on the frozen build). round_window() gives it native
            # rounded corners (via the content layer) + shadow on show; the QSS
            # radius rounds the salmon border to match. Drop Qt.Tool -- on macOS a
            # Tool window auto-hides when the app loses focus, bad for an always-on
            # mini.
            self.setStyleSheet(STYLESHEET + "\nQWidget#miniRoot{border-radius:13px}")
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint
                                | Qt.WindowStaysOnTopHint)
        else:
            # Opaque background (#0e1116). Intentionally NOT WA_TranslucentBackground
            # off macOS: translucent compositing needs Qt's bundled opengl32sw.dll
            # in the frozen build, which the spec prunes for size.
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.Tool
                                | Qt.WindowStaysOnTopHint)
        icon_path = assets_root() / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self._press_pos: QPoint | None = None

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 11, 5)
        row.setSpacing(9)

        self.sprite = SpritePlayer(size=self.SPRITE)
        row.addWidget(self.sprite)

        # Two stacked rows — session (bright) over weekly (dim). Each pairs a
        # right-aligned percentage with a small absolute reset time so the
        # rolling 5h / 7d windows are visible at a glance.
        stack = QVBoxLayout()
        stack.setSpacing(3)
        self.session_pct, self.session_reset, self.session_bar = self._row(stack, "miniPct")
        self.weekly_pct, self.weekly_reset, self.weekly_bar = self._row(stack, "miniPctSub")
        # Scoped windows the user ticked, each a dim row like WEEKLY's with the
        # limit's name leading its reset text (the mini has no titles).
        self.scoped_rows = ScopedRows(self._make_scoped_row, self._render_scoped_row,
                                      spacing=3)
        stack.addWidget(self.scoped_rows)
        row.addLayout(stack, 1)

        # ThemedPopup, not QMenu — a menu's panel cannot be painted opaque on
        # macOS (see ThemedPopup for the attempts that failed on hardware).
        menu = make_popup(self)
        menu.set_items([("Expand", self.expand_requested.emit),
                        ("Quit", self.quit_requested.emit)])
        self._menu = menu
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self._menu.popup_at(self.mapToGlobal(pos))
        )
        self._set_rows_tooltip([])

    _TOOLTIP_HINT = "Double-click to expand · drag to move"

    def _set_rows_tooltip(self, labels: list[str]) -> None:
        if not labels:
            rows = "Session (top) · Weekly (bottom)"
        else:
            rows = "Top to bottom: Session · Weekly · " + " · ".join(labels)
        self.setToolTip(f"{rows}\n{self._TOOLTIP_HINT}")

    def _make_scoped_row(self):
        row = QWidget()
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        return row, self._row(col, "miniPctSub")

    def _render_scoped_row(self, parts, window, minutes: int, warn_at: int) -> None:
        pct, reset, bar = parts
        self._set_bar(pct, bar, window.pct, warn_at)
        reset.setText(f"{window.name} · {scoped_reset_text(window, minutes)}")

    def _row(self, parent_layout: QVBoxLayout, pct_object: str):
        line = QHBoxLayout()
        line.setSpacing(7)
        pct = QLabel("-", objectName=pct_object)
        # Reserve room for three digits ("100%") so the mini's width doesn't jump
        # when the session meter crosses into triple digits; right-aligned so the
        # number stays put against the reset text.
        pct.setMinimumWidth(56)
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        reset = QLabel("", objectName="miniReset")
        reset.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        line.addWidget(pct)
        line.addWidget(reset)
        line.addStretch(1)
        parent_layout.addLayout(line)
        # A thin usage bar under each line so the mini mirrors the full view's
        # bars, including the red overage restart past 100%.
        bar = UsageBar(height=6)
        bar.setMinimumWidth(130)
        parent_layout.addWidget(bar)
        return pct, reset, bar

    def update_usage(self, session_pct: int, weekly_pct: int,
                     session_reset_minutes: int, weekly_reset_minutes: int,
                     scoped=()) -> None:
        s_warn, w_warn = bar_warn_thresholds()
        self._set_bar(self.session_pct, self.session_bar, session_pct, s_warn)
        self._set_bar(self.weekly_pct, self.weekly_bar, weekly_pct, w_warn)
        self.set_resets(session_reset_minutes, weekly_reset_minutes, scoped)

    @staticmethod
    def _set_bar(pct_label, bar, pct: int, warn_at: int) -> None:
        """Mirror the full-view bar: heat fill under 100%, red restart past it
        (the bar empties and fills red by the amount over 100)."""
        pct = max(0, int(pct))
        over = max(0, pct - 100)
        if over > 0:
            bar.set_values(0, over, "cool")
        else:
            bar.set_values(pct, 0, _heat(pct, warn_at))
        pct_label.setText(f"{pct}%")

    def set_resets(self, session_reset_minutes: int, weekly_reset_minutes: int,
                   scoped=()) -> None:
        """Reset labels in the same relative form as the main window.
        ``scoped``: ``(window, reset_minutes, warn_at)`` per scoped window shown —
        pass it on every call, since an empty value removes those rows."""
        self.session_reset.setText(f"resets in {_format_minutes(session_reset_minutes)}")
        self.weekly_reset.setText(f"resets in {_format_minutes(weekly_reset_minutes)}")
        if self.scoped_rows.set_windows(scoped):
            self._set_rows_tooltip([w.label for w, _m, _warn in scoped])
        # The width follows the reset texts, which change every minute, so the
        # rows' own layouts must be current before lock_size measures.
        self.scoped_rows.settle()
        self.lock_size()

    # Qt's QWIDGETSIZE_MAX — the "no constraint" sentinel for max size.
    _SIZE_MAX = 16777215
    # Horizontal breathing room so high-DPI rounding can't clip the reset text.
    _WIDTH_SLACK_PX = 8

    def lock_size(self) -> None:
        """Pin the window to its current content size as a hard cap.

        Belt-and-suspenders with the native-drag move: even if a mixed-DPI
        geometry glitch tries to resize the window, min == max blocks it, so it
        can't balloon across monitors. Recomputed on every content change so a
        longer "resets in ..." string is never clipped.

        Locks to sizeHint() (not size()): adjustSize() on a visible top-level
        window doesn't update size() synchronously, so reading it back pinned a
        stale, narrower width and clipped the trailing "m"/"h" of the reset text.
        A few px of horizontal slack is added on top: sizeHint() comes out exactly
        text-tight, so at fractional/200% scaling rounding would otherwise shave
        the last glyph. The trailing stretch in each row soaks up the slack.
        """
        self.setMinimumSize(0, 0)
        self.setMaximumSize(self._SIZE_MAX, self._SIZE_MAX)
        self.layout().activate()
        hint = self.sizeHint()
        self.setFixedSize(hint.width() + self._WIDTH_SLACK_PX, hint.height())

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if sys.platform == "darwin" and not getattr(self, "_macos_rounded", False):
            self._macos_rounded = True
            QTimer.singleShot(0, lambda: macos_window.round_window(self, 13))

    def apply_theme_style(self) -> None:
        """Re-apply the (theme-updated) stylesheet + repaint. apply_theme's generic
        swap misses us because the macOS radius append changes our stylesheet."""
        qss = STYLESHEET
        if sys.platform == "darwin":
            qss += "\nQWidget#miniRoot{border-radius:13px}"
        self.setStyleSheet(qss)
        self.update()

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        # Once the pointer passes the drag threshold, hand the move to
        # Windows' native move loop instead of repositioning the window
        # ourselves. The manual self.move() approach made Qt recompute the
        # window geometry on every step, which — when the frameless window
        # straddled a higher-DPI monitor — doubled the size each pass and
        # ballooned it across the desktop. The native loop is DPI-aware and
        # never triggers that recompute. Movement under the threshold is left
        # alone so double-click-to-expand still registers.
        if (e.buttons() & Qt.LeftButton) and self._press_pos is not None:
            moved = (e.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                winutil.start_move(self)
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._press_pos = None

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.expand_requested.emit()
            e.accept()


class ResetToast(QWidget):
    """Themed limit-reset toast: a frameless card with the mascot, a title and
    a one-line body. Fades in at the bottom-right of the primary screen, auto-
    dismisses after DURATION_MS, and emits `clicked` (then dismisses) on click
    so the dashboard can pop to the foreground.

    Replaces QSystemTrayIcon.showMessage so the reset alert matches the app's
    look. Pure QtWidgets/QtGui/QtCore — no QtMultimedia, no new dependency.
    """

    clicked = Signal()

    MARGIN = 18           # gap from the screen working-area edges
    DURATION_MS = 8000    # visible time before the auto fade-out
    FADE_MS = 220
    # A reset is good news, so the mascot does its DJ bounce rather than idling.
    ANIMS = ["dance bounce dj"]

    # Single source of truth: the native content-layer mask (round_window) and
    # the QSS border-radius must agree or the card's border is clipped at the
    # corners. macOS only — off macOS the window stays opaque and square, so a
    # rounded card would just expose dark square corners behind it.
    MACOS_RADIUS = 12

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("toastShell")
        self.setWindowTitle("Clawdmeter")
        self._show_seq = 0  # bumps each show so the sprite restarts its anim
        self._on_click = None  # optional per-message click action (else `clicked`)
        # Frameless, on-top, no taskbar entry, and — critically — never steal
        # focus/activation from whatever the user is doing when it pops.
        if sys.platform == "darwin":
            # Drop Qt.Tool on macOS: a Tool window auto-hides when the app loses
            # focus, and the app is BY DEFINITION not frontmost when an alert
            # matters — so the toast vanished exactly when it needed to be seen,
            # leaving "pop the window to front" as the only thing that actually
            # notified you. Same fix the mini and compact HUDs already carry.
            # WA_ShowWithoutActivating below still keeps it from stealing focus.
            self.setWindowFlags(
                Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            )
        else:
            self.setWindowFlags(
                Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
            )
        # Opaque, like the main/mini windows. NOT WA_TranslucentBackground:
        # the slim frozen build prunes opengl32sw.dll, without which translucent
        # compositing renders wrong. windowOpacity (the fade) is a separate OS
        # layered-window feature and keeps working.
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        card = QWidget(objectName="toastRoot")
        card.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(card)
        self._card = card
        self.apply_theme_style()

        row = QHBoxLayout(card)
        row.setContentsMargins(14, 12, 16, 12)
        row.setSpacing(12)

        self.sprite = SpritePlayer(size=44)
        row.addWidget(self.sprite, 0, Qt.AlignVCenter)

        text = QVBoxLayout()
        text.setSpacing(2)
        self.title = QLabel("", objectName="toastTitle")
        self.body = QLabel("", objectName="toastBody")
        self.body.setWordWrap(True)
        text.addWidget(self.title)
        text.addWidget(self.body)
        row.addLayout(text, 1)

        # Dismiss ✕. Clicking the toast body activates the app (the convention on
        # both platforms: a click is the user asking to see it), so there has to
        # be a separate way to say "not now" — otherwise the only options are
        # waiting out the timer or getting a window you didn't want. Parented to
        # the card and positioned by hand rather than added to `row`, so showing
        # and hiding it never reflows the toast's text. Revealed on hover, like
        # the macOS and Windows notification centres.
        self._close_btn = QToolButton(card, objectName="toastClose")
        self._close_btn.setText("✕")
        self._close_btn.setCursor(Qt.PointingHandCursor)
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setToolTip("Dismiss")
        self._close_btn.clicked.connect(self.dismiss)
        self._close_btn.hide()

        self.setFixedWidth(330)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(self.FADE_MS)
        self._fade.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_hooked = False  # is _on_faded_out currently connected?

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.dismiss)

    def apply_theme_style(self) -> None:
        """Re-apply the (theme-updated) stylesheet to the toast card.

        On macOS round_window() masks the window to MACOS_RADIUS, so the QSS has
        to round the card's 1px border to the same value or it is drawn square
        and clipped at each corner. That append also means apply_theme's
        exact-match swap can't see us — hence this method: apply_theme
        broadcasts it during its widget walk."""
        qss = STYLESHEET
        if sys.platform == "darwin":
            qss += f"\nQWidget#toastRoot{{border-radius:{self.MACOS_RADIUS}px}}"
        self._card.setStyleSheet(qss)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # Re-applied on every show, not latched: anything that makes Qt rebuild
        # the native window would otherwise silently lose these, and both calls
        # are idempotent. make_overlay: without it the toast cannot be drawn on
        # another app's fullscreen Space, so macOS switches Spaces to show it —
        # which looks like the fullscreen app being yanked away. round_window:
        # native rounded corners + shadow, so the toast isn't the one square
        # window left once the main/mini/compact/dialog are all rounded.
        macos_window.make_overlay(self)
        macos_window.round_window(self, self.MACOS_RADIUS)
        self._place_close_btn()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._place_close_btn()

    def _place_close_btn(self) -> None:
        """Pin the ✕ to the card's top-right corner."""
        m = 5
        self._close_btn.move(
            max(0, self._card.width() - self._close_btn.width() - m), m)
        self._close_btn.raise_()

    def enterEvent(self, e) -> None:
        super().enterEvent(e)
        self._close_btn.show()

    def leaveEvent(self, e) -> None:
        super().leaveEvent(e)
        self._close_btn.hide()

    def show_message(self, title: str, body: str, on_click=None) -> None:
        """Show (or re-show) the toast with new text and restart the timer.
        `on_click`, if given, runs when the toast is clicked (instead of emitting
        `clicked`) — e.g. the update toast opens the download page."""
        self._on_click = on_click
        self.title.setText(title)
        self.body.setText(body)
        # dismiss() stops the sprite, and set_anims() no-ops on an unchanged key —
        # so reuse of a fixed key would leave the mascot frozen on every toast
        # after the first. A fresh key each show forces a clean restart.
        self._show_seq += 1
        self.sprite.set_anims(f"toast{self._show_seq}", self.ANIMS)
        self.adjustSize()
        self._move_to_corner()

        # Cancel any in-flight fade-out so a fresh alert always lands at full
        # opacity, and drop the hide-on-finish hook from a prior dismiss().
        self._fade.stop()
        self._disconnect_fade_finished()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

        self._dismiss_timer.start(self.DURATION_MS)

    def dismiss(self) -> None:
        """Fade out and hide. Safe to call when already hidden."""
        self._dismiss_timer.stop()
        self._fade.stop()
        if not self._fade_hooked:
            self._fade.finished.connect(self._on_faded_out)
            self._fade_hooked = True
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _on_faded_out(self) -> None:
        # A new show_message() during the fade would have bumped opacity back
        # up; only actually hide if we really faded to zero.
        if self.windowOpacity() <= 0.01:
            self.sprite.stop()
            self.hide()

    def _disconnect_fade_finished(self) -> None:
        if self._fade_hooked:
            self._fade.finished.disconnect(self._on_faded_out)
            self._fade_hooked = False

    def _move_to_corner(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()  # excludes the taskbar
        x = geo.right() - self.width() - self.MARGIN
        y = geo.bottom() - self.height() - self.MARGIN
        self.move(x, y)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            if self._on_click is not None:
                self._on_click()
            else:
                self.clicked.emit()
            self.dismiss()
            e.accept()
        else:
            super().mousePressEvent(e)


# Auto-hide is unavailable on macOS. The traffic lights are drawn by AppKit in
# the NSWindow's titlebar region, NOT by our TitleBar widget, so collapsing the
# widget to height 0 doesn't remove them — it strands them over the content with
# the wordmark and view buttons gone. Apple's HIG is also explicit that the
# traffic lights must never be hidden or repositioned, and the small-footprint
# need auto-hide serves is already covered on macOS by the compact and mini HUD
# views. Gated in one place (_apply_auto_hide) so no caller can switch it on.
AUTO_HIDE_SUPPORTED = sys.platform != "darwin"


class TitleBar(QWidget):
    """Custom frameless title bar: icon, drag area, view + window buttons."""

    HEIGHT = 48
    ICON_SIZE = 36

    def __init__(self, window: QMainWindow, on_toggle, on_mini) -> None:
        super().__init__(window)
        self.setObjectName("titleBar")
        # Allow vertical animation: min=0, max=HEIGHT. Auto-hide animates
        # maximumHeight between these two values. When auto-hide is off,
        # _apply_auto_hide pins both ends back to HEIGHT.
        self.setMinimumHeight(0)
        self.setMaximumHeight(self.HEIGHT)
        self._win = window
        self._press_pos: QPoint | None = None

        row = QHBoxLayout(self)
        # On macOS the traffic lights occupy the top-left, spilling ~24px past the
        # nav rail into this bar, so pad the wordmark right to clear them.
        row.setContentsMargins(30 if sys.platform == "darwin" else 8, 0, 0, 0)
        row.setSpacing(8)

        # The app icon now lives at the top of the nav rail (so it survives the
        # auto-hide title bar), not here. The title bar starts with the wordmark.
        name = QLabel("CLAWDMETER", objectName="titleAppName")
        row.addWidget(name)
        row.addStretch(1)

        # Font Awesome glyphs for the view toggles + window controls. (Settings
        # now lives in the nav rail, so there's no gear here.)
        # Full<->Compact toggle: angles that point DOWN in full (click to collapse
        # to compact) and UP in compact (click to expand to full).
        self.caret_btn = self._tool_btn("", "Compact view")
        self.caret_btn.clicked.connect(on_toggle)
        row.addWidget(self.caret_btn)

        # Mini button (always available): Windows' restore-to-center glyph.
        self.mini_btn = self._tool_btn(chr(0xF422), "Mini view")  # compress-to-center
        self.mini_btn.clicked.connect(on_mini)
        row.addWidget(self.mini_btn)

        self.set_active_mode("full")

        # On macOS the native traffic-light buttons own minimize/close, so we
        # don't draw our own (they'd be redundant + non-native). Windows/Linux
        # keep them since those platforms have no window controls when frameless.
        if sys.platform != "darwin":
            self.min_btn = self._tool_btn(chr(0xF2D1), "Minimize")    # ChromeMinimize
            self.min_btn.clicked.connect(self._win.showMinimized)
            row.addWidget(self.min_btn)

            self.close_btn = self._tool_btn(chr(0xF00D), "Close")     # ChromeClose
            self.close_btn.setObjectName("closeBtn")
            self.close_btn.clicked.connect(self._win.close)
            row.addWidget(self.close_btn)

    def _tool_btn(self, glyph: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setObjectName("titleBtn")
        b.setText(glyph)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        return b

    def set_active_mode(self, mode: str) -> None:
        """Point the caret toward what the toggle does next: DOWN in full (click
        -> compact), UP in compact (click -> full)."""
        up = mode == "compact"
        self.caret_btn.setText(chr(0xF102) if up else chr(0xF103))  # angles up / down
        self.caret_btn.setToolTip("Full view" if up else "Compact view")

    def mousePressEvent(self, e) -> None:
        if e.button() != Qt.LeftButton:
            return
        # Off Windows, the title bar overlaps the window's top resize border (and
        # the upper part of the side/corner borders), so a press there must start
        # an OS resize, not a move — otherwise the top edge is unresizable because
        # this widget accepts the press before it can reach Dashboard's own
        # border handler. On Windows the WM_NCHITTEST path in Dashboard.nativeEvent
        # already handles the border before this widget sees the press.
        if not winutil.is_windows() and not self._win.isMaximized():
            wp = self._win.mapFromGlobal(e.globalPosition().toPoint())
            hit = winutil.hit_test(wp.x(), wp.y(), self._win.width(), self._win.height())
            if hit != winutil.HTCLIENT:
                winutil.start_resize(self._win, winutil.edges_for_hit(hit))
                e.accept()
                return
        self._press_pos = e.globalPosition().toPoint()
        e.accept()

    def mouseMoveEvent(self, e) -> None:
        # Once past the drag threshold, hand the move to Windows' native move
        # loop rather than repositioning the window ourselves every step. The
        # manual self._win.move() approach made Qt recompute the window geometry
        # on each step, which ballooned the frameless window when it was dragged
        # onto a higher-DPI monitor. The native loop is DPI-aware and avoids it.
        # Under the threshold we do nothing so the double-click-to-reset still
        # registers.
        if not (e.buttons() & Qt.LeftButton) or self._press_pos is None:
            return
        moved = (e.globalPosition().toPoint() - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return
        self._press_pos = None
        if self._win.isMaximized():
            # If the OS maximized us (e.g. Win+Up), restore before the handoff so
            # the window follows the cursor at its normal size.
            self._win.showNormal()
        winutil.start_move(self._win)
        e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._press_pos = None

    def mouseDoubleClickEvent(self, e) -> None:
        # Double-click snaps the window back to the snug auto-fit height (undoes
        # a manual height resize).
        if e.button() == Qt.LeftButton:
            self._win.reset_to_fit()
            e.accept()


# Per-channel editor fields: (getter, setter, placeholder, is_secret). Telegram
# needs two; ntfy/Discord one each.
_PUSH_FIELD_SPECS = {
    "ntfy": [(app_settings.get_reset_notify_push_topic,
              app_settings.set_reset_notify_push_topic,
              "ntfy topic (e.g. clawd-nick-7f3a) or full URL", False)],
    "telegram": [
        (app_settings.get_reset_notify_push_tg_token,
         app_settings.set_reset_notify_push_tg_token,
         "Telegram bot token (from @BotFather)", True),
        (app_settings.get_reset_notify_push_tg_chat,
         app_settings.set_reset_notify_push_tg_chat,
         "Telegram chat ID (e.g. 123456789)", False)],
    "discord": [(app_settings.get_reset_notify_push_discord,
                 app_settings.set_reset_notify_push_discord,
                 "Discord webhook URL", True)],
    "slack": [(app_settings.get_reset_notify_push_slack,
               app_settings.set_reset_notify_push_slack,
               "Slack incoming webhook URL", True)],
    "webhook": [(app_settings.get_reset_notify_push_webhook,
                 app_settings.set_reset_notify_push_webhook,
                 "Webhook URL (receives a JSON POST)", True)],
    "pushover": [
        (app_settings.get_reset_notify_push_po_token,
         app_settings.set_reset_notify_push_po_token,
         "Pushover application API token", True),
        (app_settings.get_reset_notify_push_po_user,
         app_settings.set_reset_notify_push_po_user,
         "Pushover user key", True)],
    "gotify": [
        (app_settings.get_reset_notify_push_gotify_url,
         app_settings.set_reset_notify_push_gotify_url,
         "Gotify server URL (e.g. https://gotify.example.com)", False),
        (app_settings.get_reset_notify_push_gotify_token,
         app_settings.set_reset_notify_push_gotify_token,
         "Gotify app token", True)],
}
_PUSH_HINTS = {
    "ntfy": "Subscribe to the same topic in the ntfy app (Android/iOS). Pick a "
            "long, hard-to-guess topic — anyone who knows it can read your alerts.",
    "telegram": "Message @BotFather to create a bot and copy its token, then DM "
                "the bot and read your chat ID from "
                "api.telegram.org/bot<token>/getUpdates. Keep the token private.",
    "discord": "In Discord: Channel Settings → Integrations → Webhooks → New "
               "Webhook → Copy URL. Anyone with the URL can post there, so keep "
               "it private.",
    "slack": "In Slack: create an Incoming Webhook for a channel "
             "(api.slack.com/messaging/webhooks) and paste its URL. Keep it private.",
    "webhook": "POSTs JSON {title, body, app} to any URL — wire it to Zapier, "
               "Make, IFTTT, n8n, Home Assistant, or your own endpoint.",
    "pushover": "Create an application at pushover.net for the API token; your "
                "user key is on your Pushover dashboard. Get the app on iOS/Android.",
    "gotify": "Self-hosted Gotify: your server URL plus an application token "
              "from the Gotify apps page.",
}


def _push_channel_summary(provider: str) -> str:
    """Short glanceable state for a channel row (the non-secret topic for ntfy,
    a 'set'/'not set' for the secret channels)."""
    def both(a: str, b: str, ok: str) -> str:
        if a and b:
            return ok
        return "incomplete" if (a or b) else "not set"

    if provider == "ntfy":
        return app_settings.get_reset_notify_push_topic() or "not set"
    if provider == "telegram":
        return both(app_settings.get_reset_notify_push_tg_token(),
                    app_settings.get_reset_notify_push_tg_chat(), "bot + chat set")
    if provider == "discord":
        return "webhook set" if app_settings.get_reset_notify_push_discord() else "not set"
    if provider == "slack":
        return "webhook set" if app_settings.get_reset_notify_push_slack() else "not set"
    if provider == "webhook":
        return "URL set" if app_settings.get_reset_notify_push_webhook() else "not set"
    if provider == "pushover":
        return both(app_settings.get_reset_notify_push_po_token(),
                    app_settings.get_reset_notify_push_po_user(), "token + user key set")
    if provider == "gotify":
        return both(app_settings.get_reset_notify_push_gotify_url(),
                    app_settings.get_reset_notify_push_gotify_token(), "server + token set")
    return ""


class _PushChannelRow(QWidget):
    """One added push channel as a small card: a glanceable status line (a
    coral/dim dot + name + muted summary) with flat Edit/✕ actions, and a
    collapsible editor (the channel's field(s) + hint) revealed by Edit."""

    removed = Signal()

    def __init__(self, provider: str, name: str, parent=None) -> None:
        super().__init__(parent)
        self._provider = provider
        self._name = name
        self.setObjectName("pushCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 7, 7, 7)
        col.setSpacing(5)

        head = QHBoxLayout()
        head.setSpacing(4)
        self._summary = QLabel(objectName="pushSummary")
        self._summary.setTextFormat(Qt.RichText)
        head.addWidget(self._summary)
        head.addStretch(1)
        self._edit_btn = QToolButton(objectName="pushEditBtn")
        self._edit_btn.setText("Edit")
        self._edit_btn.setCursor(Qt.PointingHandCursor)
        self._edit_btn.clicked.connect(self._toggle_edit)
        head.addWidget(self._edit_btn)
        rm = QToolButton(objectName="pushRemoveBtn")
        rm.setText("✕")
        rm.setToolTip(f"Remove {name}")
        rm.setCursor(Qt.PointingHandCursor)
        rm.clicked.connect(self.removed.emit)
        head.addWidget(rm)
        col.addLayout(head)

        self._editor = QWidget()
        ed = QVBoxLayout(self._editor)
        ed.setContentsMargins(2, 2, 0, 0)
        ed.setSpacing(4)
        for getter, setter, placeholder, secret in _PUSH_FIELD_SPECS[provider]:
            f = QLineEdit()
            f.setPlaceholderText(placeholder)
            if secret:
                f.setEchoMode(QLineEdit.Password)
            f.setText(getter())
            f.editingFinished.connect(
                lambda field=f, save=setter: self._on_field(field, save))
            ed.addWidget(f)
        hint = _PUSH_HINTS.get(provider)
        if hint:
            h = QLabel(hint, objectName="sectionHint")
            h.setWordWrap(True)
            ed.addWidget(h)
        self._first_field = self._editor.findChild(QLineEdit)
        col.addWidget(self._editor)
        self._editor.setVisible(False)
        self._refresh()

    def _on_field(self, field: QLineEdit, save) -> None:
        save(field.text())
        self._refresh()

    def _toggle_edit(self) -> None:
        self.set_editing(not self._editor.isVisible())

    def set_editing(self, on: bool) -> None:
        self._editor.setVisible(on)
        self._edit_btn.setText("Done" if on else "Edit")
        if on and self._first_field is not None:
            self._first_field.setFocus()

    def apply_theme_style(self) -> None:
        """Rebuild the summary against the current palette.

        Its colours are inline in rich text, so they are frozen at the palette
        in force when the row was last refreshed — nothing repaints them on a
        theme switch. Switching from a dark theme to a light one therefore left
        dark-theme greys on a light background, and the channel name came out
        nearly unreadable. `apply_theme`'s widget walk calls this.
        """
        self._refresh()

    def _refresh(self) -> None:
        configured = app_settings.push_channel_configured(self._provider)
        p = theme.active()
        dot = p.accent if configured else p.border_dim
        summary = _push_channel_summary(self._provider)
        summary = (summary.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))  # the ntfy topic is user-supplied
        self._summary.setText(
            f"<span style='color:{dot}'>●</span>&nbsp;&nbsp;"
            f"<span style='color:{p.text}'>{self._name}</span>&nbsp;&nbsp;"
            f"<span style='color:{p.text_muted}'>· {summary}</span>")


class _ThemeOption(QFrame):
    """One selectable theme row in the Appearance picker: a strip of colour
    swatches (the theme's identity), its name, and an ACTIVE tag. The Custom row
    (`editable`) also carries an Edit button and refreshes its swatches as the
    custom theme is edited."""

    selected = Signal(str)
    edit_requested = Signal()

    # Fixed per-theme chips — styled inline (their own colours), NOT via the
    # themed base stylesheet, so they show each theme's identity.
    _SWATCH_ROLES = ("bg", "surface", "accent", "text")

    def __init__(self, name: str, palette, parent=None, *,
                 editable: bool = False) -> None:
        super().__init__(parent)
        self.setObjectName("themeOption")
        self._name = name
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(11)
        swatches = QHBoxLayout()
        swatches.setSpacing(3)
        self._chips = []
        for _role in self._SWATCH_ROLES:
            chip = QFrame()
            chip.setFixedSize(11, 19)
            self._chips.append(chip)
            swatches.addWidget(chip)
        row.addLayout(swatches)
        self.refresh_swatches(palette)
        row.addWidget(QLabel(name, objectName="themeName"))
        row.addStretch(1)
        self._tag = QLabel("ACTIVE", objectName="themeTag")
        self._tag.setVisible(False)
        row.addWidget(self._tag)
        if editable:
            edit_btn = QPushButton("Edit", objectName="resetLink")
            edit_btn.setCursor(Qt.PointingHandCursor)
            edit_btn.setFocusPolicy(Qt.NoFocus)
            edit_btn.clicked.connect(lambda: self.edit_requested.emit())
            row.addWidget(edit_btn)

    def refresh_swatches(self, palette) -> None:
        for chip, role in zip(self._chips, self._SWATCH_ROLES):
            chip.setStyleSheet(
                f"background:{getattr(palette, role)}; border-radius:3px;"
                " border:1px solid rgba(0,0,0,0.35);")

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self._tag.setVisible(on)
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.selected.emit(self._name)
        super().mousePressEvent(e)


class _ThemedCombo(QComboBox):
    """A combo whose popup is themed to the LIVE palette each time it opens.

    The popup is a separate top-level window: the app stylesheet reaches the
    item view but not the container frame behind it, which otherwise shows a
    native (light) background. So on every open we restyle the view and paint
    the container's background to match — read from theme.active() so it always
    matches the current theme.

    Sizes to a fixed character budget (not the widest item), so a long preset
    name can't blow out the narrow settings column — it elides in the field and
    shows in full in the popup."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Small minimum so the combo can shrink (never widening the settings
        # column past its limit) — but with a stretch factor in its row it fills
        # the available space at render, showing the full name + swatches.
        self.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)

    def showPopup(self) -> None:
        p = theme.active()
        if sys.platform != "win32":
            # macOS draws a combo's popup container with the same vibrancy
            # material as a menu panel, so it renders translucent and neither a
            # stylesheet nor painting its NSWindow fixes it (both confirmed on
            # hardware).
            #
            # Linux has a different but equally unfixable-by-stylesheet problem:
            # the platform style draws its own item highlight, so the QSS hover
            # rule below never shows, and the popup's container frame keeps
            # square corners behind the rounded view — the same mismatched
            # corners macOS had. Windows renders both correctly and keeps the
            # native widget, along with its arrow-key navigation and
            # accessibility.
            #
            # The combo itself is untouched either way, so currentText /
            # currentIndexChanged and every caller keep working as before.
            # ThemedPopup explicitly, not make_popup: on Linux that returns the
            # QMenu wrapper, which is the thing being replaced here.
            if getattr(self, "_mac_popup", None) is None:
                self._mac_popup = ThemedPopup(self)
            # Carry the per-preset swatch icons through — they live on the
            # combo's items and a text-only list would drop them.
            self._mac_popup.set_items(
                [(self.itemText(i), lambda idx=i: self.setCurrentIndex(idx),
                  self.itemIcon(i)) for i in range(self.count())],
                icon_size=self.iconSize())
            self._mac_popup.popup_under(self)
            return
        self.view().setStyleSheet(
            f"QAbstractItemView{{background:{p.surface_dim};color:{p.text};"
            f"border:1px solid {p.border};border-radius:8px;padding:5px;outline:none;}}"
            f"QAbstractItemView::item{{padding:5px 8px;min-height:22px;"
            f"border-radius:5px;color:{p.text};}}"
            f"QAbstractItemView::item:selected{{background:{p.surface};color:{p.accent};}}"
            f"QAbstractItemView::item:hover{{background:{p.surface};}}")
        super().showPopup()
        win = self.view().window()
        if win is not None:   # the popup container — paint it the theme colour
            win.setStyleSheet(f"background:{p.surface_dim};")
            # macOS: the container is its own NSWindow and the stylesheet alone
            # leaves it translucent, exactly as it did for the menus. Paint the
            # native window and round it to match the rest of the chrome.
            macos_window.paint_window(win, p.surface_dim)
            macos_window.round_window(win, 8)


class _PresetRow(QFrame):
    """The 'Preset theme' Appearance option — a selectable row whose dropdown
    (with per-preset swatch previews) chooses which built-in preset to use."""

    selected = Signal(str)   # emits the chosen preset name

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("themeOption")
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 8, 12, 8)
        row.setSpacing(11)
        row.addWidget(QLabel("Preset", objectName="themeName"))
        self.combo = _ThemedCombo()
        self.combo.setFocusPolicy(Qt.StrongFocus)
        self.combo.setIconSize(QSize(38, 14))
        for name in theme.names():
            self.combo.addItem(self._swatch_icon(name), name)
        self.combo.currentTextChanged.connect(self._on_combo)
        row.addWidget(self.combo, 1)   # fill the row so long names show

    @staticmethod
    def _swatch_icon(name: str) -> QIcon:
        """A 4-chip (bg/surface/accent/text) preview icon for a preset, shown
        beside its name in the dropdown field and the popup list."""
        p = theme.get(name)
        w, h, gap = 8, 14, 2
        pm = QPixmap((w + gap) * 4 - gap, h)
        pm.fill(Qt.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.Antialiasing)
        for i, role in enumerate(("bg", "surface", "accent", "text")):
            painter.setPen(QColor(0, 0, 0, 90))
            painter.setBrush(QColor(getattr(p, role)))
            painter.drawRoundedRect(QRect(i * (w + gap), 0, w, h), 2, 2)
        painter.end()
        return QIcon(pm)

    def _on_combo(self, name: str) -> None:
        self.selected.emit(name)

    def set_current(self, name: str) -> None:
        self.combo.blockSignals(True)
        self.combo.setCurrentText(name)
        self.combo.blockSignals(False)

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        # Clicking the row (outside the combo) applies the current preset.
        if e.button() == Qt.LeftButton:
            self.selected.emit(self.combo.currentText())
        super().mousePressEvent(e)


class _SystemTargets(QWidget):
    """Sub-controls shown under the Follow System row: choose which preset it
    uses when the OS is dark vs. light. The dark dropdown lists dark presets;
    the light dropdown lists light presets."""

    changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 0, 12, 8)   # indent under the Follow System row
        lay.setSpacing(7)
        darks = [n for n in theme.names() if not theme.is_light(theme.get(n))]
        lights = [n for n in theme.names() if theme.is_light(theme.get(n))]
        self._dark = self._make_row(lay, "When dark", darks)
        self._light = self._make_row(lay, "When light", lights)

    def _make_row(self, lay, label, names) -> "_ThemedCombo":
        row = QHBoxLayout()
        row.setSpacing(9)
        lbl = QLabel(label, objectName="sectionHint")
        lbl.setFixedWidth(54)
        row.addWidget(lbl)
        combo = _ThemedCombo()
        combo.setFocusPolicy(Qt.StrongFocus)
        combo.setIconSize(QSize(38, 14))
        for n in names:
            combo.addItem(_PresetRow._swatch_icon(n), n)
        combo.currentTextChanged.connect(self._on_change)
        row.addWidget(combo, 1)
        lay.addLayout(row)
        return combo

    def _on_change(self, *_) -> None:
        dark, light = self._dark.currentText(), self._light.currentText()
        theme.set_system_targets(dark, light)
        app_settings.set_system_targets(dark, light)
        self.changed.emit()

    def sync(self) -> None:
        d, light = theme.system_targets()
        for combo, val in ((self._dark, d), (self._light, light)):
            combo.blockSignals(True)
            combo.setCurrentText(val)
            combo.blockSignals(False)


class _PvBar(QWidget):
    """A tiny fixed-fill usage bar for the editor's live preview."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._track = "#1f2937"
        self._fill = "#ce7d6b"
        self.setFixedHeight(9)

    def set_colors(self, track: str, fill: str) -> None:
        self._track, self._fill = track, fill
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._track))
        p.drawRoundedRect(r, 4, 4)
        p.setBrush(QColor(self._fill))
        p.drawRoundedRect(QRect(r.x(), r.y(), int(r.width() * 0.64), r.height()), 4, 4)


class _MiniPreview(QFrame):
    """A compact live preview of the custom palette — a usage bar, status chips
    and sample text — recoloured on every edit (reads the derived palette, so it
    updates instantly without a full app restyle)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("miniPrev")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 11, 12, 12)
        v.setSpacing(9)
        self._lbl = QLabel("LIVE PREVIEW")
        v.addWidget(self._lbl)
        top = QHBoxLayout()
        self._sess = QLabel("Session")
        self._pct = QLabel("64%")
        top.addWidget(self._sess)
        top.addStretch(1)
        top.addWidget(self._pct)
        v.addLayout(top)
        self._bar = _PvBar()
        v.addWidget(self._bar)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        self._chips = [QLabel("accent"), QLabel("good"), QLabel("over")]
        for c in self._chips:
            chips.addWidget(c)
        chips.addStretch(1)
        v.addLayout(chips)
        self._sample = QLabel("data-pipeline · reading transcript.py")
        v.addWidget(self._sample)

    def refresh(self, p) -> None:
        self.setStyleSheet(
            f"QFrame#miniPrev{{background:{p.bg};border:1px solid {p.border};"
            "border-radius:8px}")
        self._lbl.setStyleSheet(
            f"color:{p.text_muted};font-size:10px;letter-spacing:1.5px")
        self._sess.setStyleSheet(f"color:{p.text_dim};font-size:12px")
        self._pct.setStyleSheet(f"color:{p.text};font-size:15px;font-weight:700")
        self._bar.set_colors(p.surface, p.accent)
        for lbl, col in zip(self._chips, (p.accent, p.positive, p.danger)):
            lbl.setStyleSheet(
                f"color:{col};background:{p.surface};border-radius:9px;"
                "padding:2px 9px;font-size:10px")
        self._sample.setStyleSheet(f"color:{p.text_muted};font-size:11px")


class _EditorRow(QFrame):
    """One selectable role in the custom-theme editor's left list."""

    clicked = Signal(str)

    def __init__(self, role: str, label: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("editorRow")
        self._role = role
        self.setProperty("selected", "false")
        self.setCursor(Qt.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(9, 7, 10, 7)
        row.setSpacing(9)
        self._sw = QFrame()
        self._sw.setFixedSize(18, 18)
        row.addWidget(self._sw)
        row.addWidget(QLabel(label, objectName="roleName"))
        row.addStretch(1)
        self._aa = QLabel()
        row.addWidget(self._aa)

    def set_swatch(self, hexv: str) -> None:
        self._sw.setStyleSheet(
            f"background:{hexv};border-radius:4px;border:1px solid rgba(0,0,0,.4)")

    def set_aa(self, text: str, color: str) -> None:
        self._aa.setText(text)
        self._aa.setStyleSheet(
            f"color:{color};font-family:'Cascadia Code',monospace;font-size:11px")

    def set_selected(self, on: bool) -> None:
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self.clicked.emit(self._role)
        super().mousePressEvent(e)


class CustomThemeEditor(QDialog):
    """Studio-style custom theme editor: a role list on the left; a themed HSV
    colour picker (with a screen eyedropper) + a live preview on the right.

    Edits update a WORKING copy and the in-dialog preview only — the rest of the
    app does NOT change until you press Apply. Apply commits the theme to the
    whole app (and persists it); Close discards any uncommitted edits. Contrast
    warnings are soft (a 'Fix contrast' button nudges failing colours to AA)."""

    applied = Signal()   # committed to the app -> refresh the Appearance page

    _ROLE_LABELS = {
        "bg": "Background", "surface": "Surface / cards", "border": "Borders",
        "text": "Text", "accent": "Accent", "warn": "Warning",
        "danger": "Danger", "positive": "Positive",
    }
    _FG_ROLES = ("text", "accent", "warn", "danger", "positive")

    # Single source of truth: the native content-layer mask (round_window) and
    # the QSS border-radius must agree or the border gets clipped at the corners.
    MACOS_RADIUS = 12

    def __init__(self, seed_base: dict, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Custom theme")
        # Frameless to match Clawdmeter's main window (no native OS title bar,
        # which looked out of place — esp. on macOS). The #root objectName gives
        # it the themed panel background + 1px border, and mousePressEvent below
        # lets its empty/header areas drag the window (child widgets keep theirs).
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint)
        self.setObjectName("root")
        # The current app theme; only the preview shows the custom colours.
        self.apply_theme_style()
        self.setMinimumWidth(540)
        self._working = dict(seed_base)   # edited in place; committed on Apply
        self._active_role = "accent"

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)
        root.addWidget(QLabel("CUSTOM THEME", objectName="settingsTitle"))
        hint = QLabel("Pick a role, then set its colour — or grab one off the "
                      "screen with the eyedropper. Shades derive automatically. "
                      "The preview updates live; press Apply to use it.",
                      objectName="sectionHint")
        hint.setWordWrap(True)
        root.addWidget(hint)

        split = QHBoxLayout()
        split.setSpacing(16)
        left = QVBoxLayout()
        left.setSpacing(1)
        self._rows = {}
        for role in theme.CUSTOM_ROLES:
            r = _EditorRow(role, self._ROLE_LABELS[role])
            r.clicked.connect(self._select_role)
            left.addWidget(r)
            self._rows[role] = r
        left.addStretch(1)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(210)
        split.addWidget(lw)

        right = QVBoxLayout()
        right.setSpacing(12)
        self.picker = ColorPicker()
        self.picker.colorChanged.connect(self._on_color)
        right.addWidget(self.picker)
        self.preview = _MiniPreview()
        right.addWidget(self.preview)
        right.addStretch(1)
        split.addLayout(right, 1)
        root.addLayout(split, 1)

        foot = QHBoxLayout()
        imp = QPushButton("Import")
        imp.clicked.connect(self._import)
        foot.addWidget(imp)
        exp = QPushButton("Export")
        exp.clicked.connect(self._export)
        foot.addWidget(exp)
        fix = QPushButton("Fix contrast")
        fix.clicked.connect(self._fix_contrast)
        foot.addWidget(fix)
        foot.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        foot.addWidget(close_btn)
        apply_btn = QPushButton("Apply", objectName="applyBtn")
        apply_btn.clicked.connect(self._apply)
        foot.addWidget(apply_btn)
        root.addLayout(foot)

        self._refresh_all()
        self._select_role(self._active_role)

    def apply_theme_style(self) -> None:
        """Re-apply the (theme-updated) stylesheet + repaint.

        On macOS round_window() masks the dialog's content layer to MACOS_RADIUS,
        so the QSS has to round #root's 1px border to the same radius or the
        border is drawn square and chopped off at each corner. That append also
        means apply_theme's exact-match swap can't see us — which is why this is
        a method: apply_theme() broadcasts it during its widget walk."""
        qss = STYLESHEET
        if sys.platform == "darwin":
            qss += f"\nQWidget#root{{border-radius:{self.MACOS_RADIUS}px}}"
        self.setStyleSheet(qss)
        self.update()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # macOS: round the frameless dialog + give it a native shadow so it's not
        # a hard square (no traffic lights needed -- it has its own Close button).
        if sys.platform == "darwin" and not getattr(self, "_macos_rounded", False):
            self._macos_rounded = True
            QTimer.singleShot(
                0, lambda: macos_window.round_window(self, self.MACOS_RADIUS))

    def mousePressEvent(self, e) -> None:
        # Frameless-window drag: buttons, the picker and the role rows consume
        # their own clicks, so this only fires on the dialog's empty/header
        # background -> move the whole window. startSystemMove is cross-platform
        # (Windows/macOS/X11/Wayland), matching the main window's drag.
        if e.button() == Qt.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()
                return
        super().mousePressEvent(e)

    def _select_role(self, role: str) -> None:
        self._active_role = role
        for r, row in self._rows.items():
            row.set_selected(r == role)
        self.picker.set_color(self._working[role])

    def _on_color(self, hexv: str) -> None:
        # Working copy + in-dialog preview only; the app is untouched until Apply.
        self._working[self._active_role] = hexv
        self._refresh_all()

    def _fix_contrast(self) -> None:
        bg = self._working["bg"]
        for role in self._FG_ROLES:
            self._working[role] = theme.ensure_contrast(self._working[role], bg, 4.5)
        self.picker.set_color(self._working[self._active_role])
        self._refresh_all()

    def _apply(self) -> None:
        theme.set_custom_base(self._working)
        app_settings.set_custom_base(theme.custom_base())
        apply_theme(theme.CUSTOM)        # now the whole app changes
        self.setStyleSheet(STYLESHEET)   # re-theme the dialog to the committed look
        self.applied.emit()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export theme", "clawdmeter-theme.json", "Theme file (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(theme.serialize_custom(self._working))
        except OSError as e:
            QMessageBox.warning(self, "Export failed",
                                f"Couldn't write the file:\n{e}")

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import theme", "", "Theme file (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                # Read one byte past the cap: parse_custom rejects anything over
                # MAX_THEME_BYTES, so an over-size (or hostile) file is refused
                # cleanly instead of being slurped whole.
                text = f.read(theme.MAX_THEME_BYTES + 1)
        except (OSError, UnicodeDecodeError) as e:
            # A non-UTF-8 file (a UTF-16 export, a renamed binary) raises
            # UnicodeDecodeError from f.read(), which is a ValueError, not an
            # OSError — uncaught, it terminates the app on PySide6 6.11 instead
            # of showing this dialog.
            QMessageBox.warning(self, "Import failed",
                                f"Couldn't read the file:\n{e}")
            return
        base = theme.parse_custom(text)
        if base is None:
            QMessageBox.warning(self, "Import failed",
                                "That file isn't a valid Clawdmeter theme.")
            return
        # Load into the working copy — previews live, applies on Apply like any edit.
        self._working = base
        self.picker.set_color(self._working[self._active_role])
        self._refresh_all()

    def _refresh_all(self) -> None:
        bg = self._working["bg"]
        pal = theme.derive_palette(self._working)   # preview reflects the working copy
        for role, row in self._rows.items():
            col = self._working[role]
            row.set_swatch(col)
            if role in self._FG_ROLES:
                c = theme.contrast(col, bg)
                ok = c >= 4.5
                row.set_aa(f"{c:.1f}", pal.text_muted if ok else pal.warn)
            else:
                row.set_aa("", pal.text_muted)
        self.preview.refresh(pal)


class SettingsPanel(QWidget):
    """The Settings page — a destination in the app nav rail (not an overlay).
    Holds a left tab rail (General/Display/Connection/Notifications/About) plus
    the matching forms. Lives as a page in the content stack; you leave it by
    clicking another rail item."""

    # Emitted from the push-test worker thread; delivered on the UI thread.
    _push_test_result = Signal(bool, str)

    def __init__(self, parent: QWidget, on_always_on_top_changed, on_auto_hide_changed,
                 on_refresh_token=None, on_auto_refresh_changed=None,
                 on_poll_interval_changed=None, on_sessions_view_changed=None,
                 on_token_view_changed=None, on_check_updates=None,
                 on_idle_backoff_changed=None, on_scoped_view_changed=None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._on_aot_changed = on_always_on_top_changed
        self._on_auto_hide_changed = on_auto_hide_changed
        self._on_refresh_token = on_refresh_token
        self._on_auto_refresh_changed = on_auto_refresh_changed
        self._on_poll_interval_changed = on_poll_interval_changed
        self._on_sessions_view_changed = on_sessions_view_changed
        self._on_token_view_changed = on_token_view_changed
        self._on_check_updates = on_check_updates
        self._on_idle_backoff_changed = on_idle_backoff_changed
        self._on_scoped_view_changed = on_scoped_view_changed

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Page header — just the title now; you leave Settings via the nav rail.
        header_w = QWidget()
        header = QHBoxLayout(header_w)
        header.setContentsMargins(20, 18, 20, 8)
        header.addWidget(QLabel("SETTINGS", objectName="settingsTitle"))
        header.addStretch(1)
        outer.addWidget(header_w)

        # Body: a left tab rail + one stacked page per tab. Each page scrolls on
        # its own so a long tab (Notifications) never drags the shorter ones, and
        # new settings slot into the tab that owns their concern.
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(0)
        outer.addLayout(content_row, 1)

        nav_w = QWidget(objectName="settingsNav")
        nav = QVBoxLayout(nav_w)
        nav.setContentsMargins(8, 8, 8, 8)
        nav.setSpacing(4)
        content_row.addWidget(nav_w)

        self._stack = QStackedWidget()
        content_row.addWidget(self._stack, 1)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._tab_index: dict[str, int] = {}

        def _make_tab(glyph: str, label: str) -> QVBoxLayout:
            btn = QPushButton(f"{glyph}   {label}", objectName="navBtn")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            nav.addWidget(btn)
            page = QScrollArea()
            page.setObjectName("settingsScroll")
            page.setWidgetResizable(True)
            page.setFrameShape(QFrame.NoFrame)
            page.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            page.viewport().setStyleSheet("background: transparent;")
            page_body = QWidget(objectName="settingsBody")
            page.setWidget(page_body)
            lay = QVBoxLayout(page_body)
            lay.setContentsMargins(14, 8, 16, 18)
            lay.setSpacing(12)
            idx = self._stack.addWidget(page)
            self._nav_group.addButton(btn, idx)
            # Recorded by name so callers never hard-code a tab position; the
            # order of these has changed before.
            self._tab_index[label] = idx
            return lay

        # Font Awesome 6 Free (Solid) glyphs: gear, display, circle-nodes, bell,
        # info. Rendered as text so they recolor with the QSS accent on
        # hover/active, exactly as the Segoe glyphs did.
        gen_layout = _make_tab("\uF013", "General")
        disp_layout = _make_tab("\uE163", "Display")
        appearance_layout = _make_tab("\uF53F", "Appearance")  # fa-palette
        conn_layout = _make_tab("\uE4E2", "Connection")
        notif_layout = _make_tab("\uF0F3", "Notifications")
        about_layout = _make_tab("\uF129", "About")
        nav.addStretch(1)
        self._size_settings_nav(nav_w, nav)
        self._nav_group.idClicked.connect(self._stack.setCurrentIndex)
        self._nav_group.button(0).setChecked(True)
        # Recompute the token line whenever Connection is opened, rather than
        # holding a snapshot taken at construction. currentChanged (not
        # idClicked) so a programmatic switch counts too.
        self._stack.currentChanged.connect(self._on_settings_tab_changed)

        # ── Appearance: three options — Follow System / Custom / Preset ──
        appearance_layout.addWidget(QLabel("THEME", objectName="sectionLabel"))
        _theme_hint = QLabel(
            "Follow your system, build your own, or pick a preset.",
            objectName="sectionHint")
        _theme_hint.setWordWrap(True)
        appearance_layout.addWidget(_theme_hint)
        self._theme_options = []
        # 1. Follow System — resolves to a dark/light preset per the OS scheme.
        self._sys_option = _ThemeOption(theme.SYSTEM, theme.active())
        self._sys_option.selected.connect(self._on_theme_selected)
        appearance_layout.addWidget(self._sys_option)
        self._theme_options.append(self._sys_option)
        # Its selectable dark/light targets, revealed when Follow System is on.
        self._system_targets = _SystemTargets()
        self._system_targets.changed.connect(self._on_system_targets_changed)
        appearance_layout.addWidget(self._system_targets)
        # 2. Custom theme — user-editable; swatches track the custom palette.
        self._custom_option = _ThemeOption(theme.CUSTOM, theme.custom_palette(),
                                           editable=True)
        self._custom_option.selected.connect(self._on_theme_selected)
        self._custom_option.edit_requested.connect(self._open_custom_editor)
        appearance_layout.addWidget(self._custom_option)
        self._theme_options.append(self._custom_option)
        # 3. Preset theme — a dropdown of the built-in presets.
        self._preset_row = _PresetRow()
        self._preset_row.selected.connect(self._on_theme_selected)
        appearance_layout.addWidget(self._preset_row)
        self._theme_options.append(self._preset_row)
        self._sync_theme_selection()

        # `layout` is a moving cursor: each section appends to whichever tab page
        # it currently points at, reassigned at the section boundaries below.
        layout = conn_layout

        layout.addWidget(QLabel("CREDENTIALS", objectName="sectionLabel"))
        self.cred_btn = QPushButton("Use alternative credentials")
        self.cred_btn.clicked.connect(self._choose_credentials)
        layout.addWidget(self.cred_btn)

        status_row = QHBoxLayout()
        status_row.setSpacing(6)
        self.cred_status = QLabel(objectName="credStatus")
        self.cred_status.setWordWrap(True)
        self.cred_reset_btn = QPushButton("reset", objectName="resetLink")
        self.cred_reset_btn.setCursor(Qt.PointingHandCursor)
        self.cred_reset_btn.setFocusPolicy(Qt.NoFocus)
        self.cred_reset_btn.clicked.connect(self._reset_credentials)
        status_row.addWidget(self.cred_status, 1)
        status_row.addWidget(self.cred_reset_btn)
        layout.addLayout(status_row)
        self._refresh_cred_status()

        layout.addSpacing(10)
        layout.addWidget(QLabel("TOKEN", objectName="sectionLabel"))
        self.token_status = QLabel(objectName="sectionHint")
        self.token_status.setWordWrap(True)
        layout.addWidget(self.token_status)
        self.auto_refresh_check = QCheckBox("Auto-refresh when expired")
        self.auto_refresh_check.setChecked(app_settings.get_auto_refresh())
        self.auto_refresh_check.toggled.connect(self._on_auto_refresh_toggled)
        layout.addWidget(self.auto_refresh_check)
        self.refresh_token_btn = QPushButton("Refresh token now")
        self.refresh_token_btn.clicked.connect(self._on_refresh_token_clicked)
        layout.addWidget(self.refresh_token_btn)
        # The escape hatch for when refreshing cannot work at all: a dead
        # refresh token, or a macOS Keychain that Clawdmeter cannot write to.
        # Always present so it is findable before it is needed.
        self.reauth_btn = QPushButton("Sign in again")
        self.reauth_btn.clicked.connect(self._on_reauth_clicked)
        layout.addWidget(self.reauth_btn)
        self.refresh_token_status()

        layout = gen_layout
        layout.addWidget(QLabel("WINDOW", objectName="sectionLabel"))
        self.aot_check = QCheckBox("Always on top")
        # Wayland gives a client no way to raise itself above other windows --
        # it is a deliberate property of the protocol, not a Qt gap, and no
        # flag or plugin changes it. Confirmed on a real compositor: the hint
        # is simply ignored for every view. Disabled rather than hidden (unlike
        # auto-hide below) because unlike that one this IS a thing on Linux --
        # it works on X11 -- so the user deserves to know why it is unavailable
        # here rather than wonder where the setting went.
        if is_wayland():
            # Shown unticked, because a ticked-and-greyed box asserts a state
            # the window is not in and offers no way to clear it. Set before
            # the signal is connected, and the stored setting is left alone --
            # this is a display decision for this session only, so the user's
            # real preference survives for when they next log into X11.
            self.aot_check.setChecked(False)
            self.aot_check.setEnabled(False)
            self.aot_check.setToolTip(
                "Wayland doesn't let apps put themselves above other windows. "
                "Use your compositor's own always-on-top shortcut instead.")
        else:
            self.aot_check.setChecked(app_settings.get_always_on_top())
        self.aot_check.toggled.connect(self._on_aot_toggled)
        layout.addWidget(self.aot_check)

        self.auto_hide_check = QCheckBox("Auto-hide title bar")
        self.auto_hide_check.setChecked(
            app_settings.get_auto_hide_titlebar() and AUTO_HIDE_SUPPORTED)
        self.auto_hide_check.toggled.connect(self._on_auto_hide_toggled)
        # Hidden rather than disabled on macOS: a permanently greyed-out control
        # invites "why can't I turn this on?", and the answer is "this isn't a
        # thing on your platform" (see AUTO_HIDE_SUPPORTED).
        self.auto_hide_check.setVisible(AUTO_HIDE_SUPPORTED)
        layout.addWidget(self.auto_hide_check)

        self.quit_on_close_check = QCheckBox("Quit on close (don't minimize to tray)")
        self.quit_on_close_check.setChecked(app_settings.get_quit_on_close())
        self.quit_on_close_check.toggled.connect(self._on_quit_on_close_toggled)
        layout.addWidget(self.quit_on_close_check)

        layout.addSpacing(10)
        layout.addWidget(QLabel("STARTUP", objectName="sectionLabel"))
        # Two things differ by platform, and the hint and the checkbox must
        # agree on both: the verb ("sign in" is Microsoft's word, "log in" is
        # Apple's and the usual Linux one) and where the app sits while it has
        # no window. Derived once and shared, because the first attempt at this
        # branched the hint and not the checkbox, so Linux read "when you log
        # in" immediately above "Start when I sign in".
        _verb = "sign in" if winutil.is_windows() else "log in"
        _where = ("menu bar — click the menu bar icon"
                  if sys.platform == "darwin"
                  else "system tray — click the tray icon")
        startup_hint = QLabel(
            f"Launch Clawdmeter automatically when you {_verb}. It starts "
            f"quietly in the {_where} to open it.",
            objectName="sectionHint")
        startup_hint.setWordWrap(True)
        layout.addWidget(startup_hint)
        self.startup_check = QCheckBox(f"Start when I {_verb}")
        self.startup_check.setChecked(run_at_startup.is_enabled())
        self.startup_check.setEnabled(run_at_startup.is_supported())
        self.startup_check.toggled.connect(self._on_run_at_startup_toggled)
        layout.addWidget(self.startup_check)

        layout.addSpacing(10)
        layout.addWidget(QLabel("UPDATES", objectName="sectionLabel"))
        # The old wording said "ships as a single .exe", which is false on macOS
        # (.dmg) and Linux (.tar.gz). The point was never the file format -- it
        # was "nothing updates itself" -- so say that instead of branching three
        # ways on a detail that does not matter to the reader.
        _menu = "menu bar menu" if sys.platform == "darwin" else "tray menu"
        updates_hint = QLabel(
            "Clawdmeter doesn't update itself. When a newer release is "
            f"published on GitHub, the {_menu} shows an “Update available” "
            "item — click it to open the download page.",
            objectName="sectionHint",
        )
        updates_hint.setWordWrap(True)
        layout.addWidget(updates_hint)
        self.auto_check_updates_check = QCheckBox("Automatically check for updates")
        self.auto_check_updates_check.setChecked(app_settings.get_auto_check_updates())
        self.auto_check_updates_check.toggled.connect(self._on_auto_check_updates_toggled)
        layout.addWidget(self.auto_check_updates_check)
        self.check_updates_btn = QPushButton("Check for updates now")
        self.check_updates_btn.clicked.connect(self._on_check_updates_clicked)
        layout.addWidget(self.check_updates_btn)

        layout = disp_layout
        layout.addWidget(QLabel("SESSIONS", objectName="sectionLabel"))
        sessions_hint = QLabel(
            "Show every active Claude Code session as its own mascot, and the "
            "child agents a session spins up. Turn off for a single mascot.",
            objectName="sectionHint",
        )
        sessions_hint.setWordWrap(True)
        layout.addWidget(sessions_hint)
        self.multi_sessions_check = QCheckBox("Show multiple sessions")
        self.multi_sessions_check.setChecked(app_settings.get_show_multiple_sessions())
        self.multi_sessions_check.toggled.connect(self._on_multi_sessions_toggled)
        layout.addWidget(self.multi_sessions_check)

        self.subagents_check = QCheckBox("Show subagents")
        self.subagents_check.setChecked(app_settings.get_show_subagents())
        self.subagents_check.toggled.connect(self._on_subagents_toggled)
        layout.addWidget(self.subagents_check)

        layout.addSpacing(10)
        layout.addWidget(QLabel("TOKEN USAGE", objectName="sectionLabel"))
        tokens_hint = QLabel(
            "Show how many tokens you've used — input+output for the 5h/7d "
            "windows beside the bars, with a full per-session breakdown when you "
            "hover a mascot. Read from your local transcripts, not the API.",
            objectName="sectionHint",
        )
        tokens_hint.setWordWrap(True)
        layout.addWidget(tokens_hint)
        self.token_usage_check = QCheckBox("Show token usage")
        self.token_usage_check.setChecked(app_settings.get_show_token_usage())
        self.token_usage_check.toggled.connect(self._on_token_usage_toggled)
        layout.addWidget(self.token_usage_check)

        layout.addSpacing(10)
        layout.addWidget(QLabel("ADDITIONAL LIMITS", objectName="sectionLabel"))
        scoped_hint = QLabel(
            "Some plans also have limits beyond the 5-hour and weekly windows, "
            "like a weekly limit on one model. Tick one to add its bar to the "
            "dashboard, compact and mini views. Approaching-limit alerts, when "
            "on, cover it too.",
            objectName="sectionHint",
        )
        scoped_hint.setWordWrap(True)
        layout.addWidget(scoped_hint)
        # One checkbox per window ever reported, rebuilt when that list changes
        # (see set_scoped_windows).
        self._scoped_box = QWidget()
        self._scoped_col = QVBoxLayout(self._scoped_box)
        self._scoped_col.setContentsMargins(0, 0, 0, 0)
        self._scoped_col.setSpacing(6)
        layout.addWidget(self._scoped_box)
        self._scoped_checks: dict[str, QCheckBox] = {}
        self._scoped_state = None
        self.set_scoped_windows(app_settings.get_scoped_seen(), None)

        layout = conn_layout
        layout.addSpacing(10)
        layout.addWidget(QLabel("USAGE POLLING", objectName="sectionLabel"))
        poll_hint = QLabel(
            f"How often to check your usage ({app_settings.POLL_INTERVAL_MIN}"
            f"–{app_settings.POLL_INTERVAL_MAX}s). Each check is a tiny API "
            "request, so lower = fresher but more requests. Takes effect on the "
            "next check.",
            objectName="sectionHint",
        )
        poll_hint.setWordWrap(True)
        layout.addWidget(poll_hint)
        poll_row = QHBoxLayout()
        poll_row.setSpacing(6)
        self.poll_interval_edit = QLineEdit()
        # Digits-only — NOT QIntValidator(0, MAX). An int validator marks
        # out-of-range-but-same-digit-count values like "999" as Intermediate,
        # and editingFinished is suppressed for non-Acceptable input, so the
        # commit/clamp would silently never run. A plain digit validator keeps
        # every commit firing; the handler does the clamping.
        self.poll_interval_edit.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"\d{0,6}"), self)
        )
        self.poll_interval_edit.setText(str(app_settings.get_poll_interval()))
        self.poll_interval_edit.setFixedWidth(64)
        self.poll_interval_edit.setToolTip(
            f"Whole seconds, {app_settings.POLL_INTERVAL_MIN}–"
            f"{app_settings.POLL_INTERVAL_MAX}. Out-of-range values are clamped."
        )
        self.poll_interval_edit.editingFinished.connect(self._on_poll_interval_committed)
        self.poll_interval_edit.textEdited.connect(self._clear_poll_note)
        poll_row.addWidget(self.poll_interval_edit)
        poll_row.addWidget(QLabel("seconds"))
        poll_row.addStretch(1)
        layout.addLayout(poll_row)

        # Transient amber note shown when a committed value gets clamped, so the
        # correction is obvious without a modal. Holds briefly, fades out, and
        # clears the moment the user edits the field again.
        self.poll_interval_note = QLabel("", objectName="pollNote")
        self.poll_interval_note.setWordWrap(True)
        self._poll_note_effect = QGraphicsOpacityEffect(self.poll_interval_note)
        self.poll_interval_note.setGraphicsEffect(self._poll_note_effect)
        self._poll_note_effect.setOpacity(0.0)
        self.poll_interval_note.hide()
        self._poll_note_fade = QPropertyAnimation(self._poll_note_effect, b"opacity", self)
        self._poll_note_fade.setDuration(500)
        self._poll_note_fade.setEndValue(0.0)
        self._poll_note_fade.finished.connect(self._clear_poll_note)
        self._poll_note_hold = QTimer(self)
        self._poll_note_hold.setSingleShot(True)
        self._poll_note_hold.setInterval(3200)
        self._poll_note_hold.timeout.connect(self._fade_poll_note)
        layout.addWidget(self.poll_interval_note)

        # Idle back-off: slow polling when no local session has been active for a
        # while. Opt-in; the sub-box (after / interval spinners) follows the toggle.
        self.idle_backoff_check = QCheckBox("Slow polling when idle")
        self.idle_backoff_check.setChecked(app_settings.get_idle_backoff_enabled())
        self.idle_backoff_check.toggled.connect(self._on_idle_backoff_toggled)
        layout.addWidget(self.idle_backoff_check)

        self.idle_box = QWidget()
        idle_box = QVBoxLayout(self.idle_box)
        idle_box.setContentsMargins(22, 0, 0, 0)
        idle_box.setSpacing(6)
        idle_hint = QLabel(
            "Poll less often when no Claude Code session has been active for a "
            "while. Usage is account-wide, so usage from another machine shows "
            "up more slowly while idle — it never stops, just slows.",
            objectName="sectionHint",
        )
        idle_hint.setWordWrap(True)
        idle_box.addWidget(idle_hint)
        self.idle_after_spin = QSpinBox()
        self.idle_after_spin.setRange(app_settings.IDLE_AFTER_MIN, app_settings.IDLE_AFTER_MAX)
        self.idle_after_spin.setSuffix(" min")
        self.idle_after_spin.setValue(app_settings.get_idle_after_minutes())
        self.idle_after_spin.setFixedWidth(80)
        self.idle_after_spin.valueChanged.connect(self._on_idle_after_changed)
        self.idle_interval_spin = QSpinBox()
        self.idle_interval_spin.setRange(app_settings.IDLE_INTERVAL_MIN, app_settings.IDLE_INTERVAL_MAX)
        self.idle_interval_spin.setSuffix(" s")
        self.idle_interval_spin.setSingleStep(30)
        self.idle_interval_spin.setValue(app_settings.get_idle_interval())
        self.idle_interval_spin.setFixedWidth(88)
        self.idle_interval_spin.valueChanged.connect(self._on_idle_interval_changed)
        idle_box.addLayout(self._threshold_row("Back off after", self.idle_after_spin,
                                               "of inactivity"))
        idle_box.addLayout(self._threshold_row("then poll every", self.idle_interval_spin,
                                               ""))
        layout.addWidget(self.idle_box)
        self.idle_box.setVisible(self.idle_backoff_check.isChecked())

        layout = notif_layout
        layout.addWidget(QLabel("WHAT TO NOTIFY", objectName="sectionLabel"))
        notify_hint = QLabel(
            "Pick what you want to be alerted about. Choose how you're reached "
            "below — it applies to every alert here.",
            objectName="sectionHint",
        )
        notify_hint.setWordWrap(True)
        layout.addWidget(notify_hint)
        self.notify_check = QCheckBox("On a limit reset (when you can resume)")
        self.notify_check.setChecked(app_settings.get_reset_notify())
        self.notify_check.toggled.connect(self._on_notify_toggled)
        layout.addWidget(self.notify_check)

        self.auth_notify_check = QCheckBox("When usage can't be read (sign-in problem)")
        self.auth_notify_check.setChecked(app_settings.get_auth_notify())
        self.auth_notify_check.toggled.connect(self._on_auth_notify_toggled)
        self.auth_notify_check.setToolTip(
            "The session shelf keeps working without a token, so a sign-in "
            "problem is easy to miss. Fires once when it happens and once when "
            "it recovers."
        )
        layout.addWidget(self.auth_notify_check)

        # Approaching-limit master + its threshold sub-box.
        self.approaching_check = QCheckBox("When approaching a limit")
        self.approaching_check.setChecked(app_settings.get_approaching_enabled())
        self.approaching_check.toggled.connect(self._on_approaching_toggled)
        layout.addWidget(self.approaching_check)

        self.approaching_box = QWidget()
        appr_box = QVBoxLayout(self.approaching_box)
        appr_box.setContentsMargins(44, 0, 0, 0)
        appr_box.setSpacing(6)
        sess_row, self.session_pct_slider, self.session_pct_field = self._make_threshold_slider(
            "5h session", app_settings.get_approaching_session_pct(),
            self._on_session_pct_changed)
        week_row, self.weekly_pct_slider, self.weekly_pct_field = self._make_threshold_slider(
            "7d week", app_settings.get_approaching_weekly_pct(),
            self._on_weekly_pct_changed)
        appr_box.addLayout(sess_row)
        appr_box.addLayout(week_row)
        self.overage_check = QCheckBox("Also alert when I cross 100% into overage")
        self.overage_check.setChecked(app_settings.get_overage_alert_enabled())
        self.overage_check.toggled.connect(self._on_overage_alert_toggled)
        appr_box.addWidget(self.overage_check)
        layout.addWidget(self.approaching_box)

        # Shared delivery channels, wrapped so the whole block hides as a unit
        # when no alert type is enabled. Subsequent channel widgets go into it.
        layout.addSpacing(10)
        self.notify_how_box = QWidget()
        how_layout = QVBoxLayout(self.notify_how_box)
        # The bottom margin is load-bearing, not cosmetic. This box is sized
        # from its layout's minimum, and the word-wrapped hint below reports a
        # single line there while rendering two — so the box comes out ~4px
        # shorter than the content it holds, and its LAST child hangs over the
        # bottom edge and is clipped.
        #
        # Measured on macOS: the trailing "Send a push notification" checkbox
        # sat at y=109..132 inside a 129px box, so the one row that fell outside
        # was its indicator's bottom border — 17 rendered rows instead of 18,
        # reading as a square-cornered box, while the identical checkbox above
        # it was fine. Giving the box room for its content puts the border back.
        how_layout.setContentsMargins(0, 0, 0, 8)
        how_layout.setSpacing(6)
        how_layout.addWidget(QLabel("HOW YOU'RE NOTIFIED", objectName="sectionLabel"))
        how_hint = QLabel(
            "Where alerts reach you — shared by every alert above.",
            objectName="sectionHint",
        )
        how_hint.setWordWrap(True)
        how_layout.addWidget(how_hint)
        layout.addWidget(self.notify_how_box)
        layout = how_layout

        # Windows channel: the desktop toast + tray flash, with Play-a-sound /
        # Pop-to-front nested in an indented box so they hide together when the
        # channel — or all alerts — are off.
        self.notify_toast_check = QCheckBox("Show a toast notification")
        self.notify_toast_check.setChecked(app_settings.get_reset_notify_toast())
        self.notify_toast_check.toggled.connect(self._on_notify_toast_toggled)
        layout.addWidget(self.notify_toast_check)

        self.notify_toast_box = QWidget()
        toast_box = QVBoxLayout(self.notify_toast_box)
        toast_box.setContentsMargins(44, 0, 0, 0)
        toast_box.setSpacing(6)
        self.notify_sound_check = QCheckBox("Play a sound")
        self.notify_sound_check.setChecked(app_settings.get_reset_notify_sound())
        self.notify_sound_check.toggled.connect(self._on_notify_sound_toggled)
        toast_box.addWidget(self.notify_sound_check)
        self.notify_popup_check = QCheckBox(
            "Bring the Clawdmeter dashboard to the front")
        self.notify_popup_check.setToolTip(
            "Raises the main Clawdmeter window on an alert. The toast already "
            "appears on top of other apps, so turn this on only if you want "
            "the full dashboard in front of you.")
        self.notify_popup_check.setChecked(app_settings.get_reset_notify_popup())
        self.notify_popup_check.toggled.connect(self._on_notify_popup_toggled)
        toast_box.addWidget(self.notify_popup_check)
        layout.addWidget(self.notify_toast_box)

        self.notify_push_check = QCheckBox("Send a push notification")
        self.notify_push_check.setChecked(app_settings.get_reset_notify_push())
        self.notify_push_check.toggled.connect(self._on_notify_push_toggled)
        layout.addWidget(self.notify_push_check)

        # Push sub-box: a list of the channels you've ADDED (each a summary row
        # with edit/remove), an "Add a channel" menu, and one test button that
        # fires every configured channel. Hides as a unit when push is off.
        self.notify_push_box = QWidget()
        push_box = QVBoxLayout(self.notify_push_box)
        push_box.setContentsMargins(44, 0, 0, 0)
        push_box.setSpacing(6)

        self.notify_push_list = QVBoxLayout()
        self.notify_push_list.setSpacing(6)
        push_box.addLayout(self.notify_push_list)

        self.notify_push_add_btn = QToolButton(objectName="addChannelBtn")
        self.notify_push_add_btn.setText("+ Add a channel  ▾")
        self.notify_push_add_btn.setCursor(Qt.PointingHandCursor)
        self.notify_push_add_btn.setPopupMode(QToolButton.InstantPopup)
        # A ThemedPopup, not a QMenu: a menu's panel cannot be made opaque on
        # macOS (see ThemedPopup for the four attempts that failed on hardware).
        self.notify_push_add_menu = make_popup(self.notify_push_add_btn)
        self.notify_push_add_btn.setPopupMode(QToolButton.DelayedPopup)
        self.notify_push_add_btn.clicked.connect(
            lambda: self.notify_push_add_menu.popup_under(self.notify_push_add_btn))
        push_box.addWidget(self.notify_push_add_btn, alignment=Qt.AlignLeft)

        self.notify_push_test_btn = QPushButton("Send test notification")
        self.notify_push_test_btn.clicked.connect(self._on_test_push_clicked)
        push_box.addWidget(self.notify_push_test_btn)
        self.notify_push_test_status = QLabel("", objectName="sectionHint")
        self.notify_push_test_status.setWordWrap(True)
        push_box.addWidget(self.notify_push_test_status)
        layout.addWidget(self.notify_push_box)
        self._push_test_result.connect(self._on_test_push_result)
        self._push_rows: dict[str, _PushChannelRow] = {}
        self._rebuild_push_channels()

        self._sync_notify_subtoggles()

        layout = gen_layout
        layout.addSpacing(10)
        self._start_menu_label = QLabel("START MENU", objectName="sectionLabel")
        layout.addWidget(self._start_menu_label)
        self._start_menu_hint = QLabel(
            "Adds a Start menu shortcut. Right-click it in Start to Pin to Start.",
            objectName="sectionHint",
        )
        self._start_menu_hint.setWordWrap(True)
        layout.addWidget(self._start_menu_hint)
        self.start_btn = QPushButton()
        self.start_btn.clicked.connect(self._on_start_menu_toggled)
        layout.addWidget(self.start_btn)
        # A Start Menu shortcut is a Windows-only concept; hide the whole section
        # off Windows rather than showing a disabled, Windows-worded control.
        if start_menu.is_supported():
            self._refresh_start_menu_btn()
        else:
            self._start_menu_label.setVisible(False)
            self._start_menu_hint.setVisible(False)
            self.start_btn.setVisible(False)

        layout = about_layout
        layout.addWidget(QLabel("ABOUT", objectName="sectionLabel"))
        about = QLabel(
            # The product is "Clawdmeter" on every platform; the repository is
            # a separate thing, so the link below keeps the real repo name and
            # changes only when the repo is actually renamed.
            #
            # It is only a link -- nothing reads this string. The constant that
            # DOES matter is update_check.REPO, which builds the releases API
            # URL and gates which release URLs are trusted; that one has to move
            # in the same commit as the rename or update checking breaks.
            f"Clawdmeter  v{app_settings.APP_VERSION}\n"
            "by Nick Welter (@weltern) & Claude\n"
            "github.com/weltern/Clawdmeter\n\n"
            "MIT licensed · the Clawd mascot is © Anthropic PBC and is "
            "not covered by the MIT license · unofficial, not affiliated "
            "with Anthropic.\n\n"
            "Icons by Font Awesome Free (fontawesome.com) · SIL OFL 1.1.",
            objectName="sectionHint",
        )
        about.setWordWrap(True)
        about.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(about)

        for _page_layout in (gen_layout, disp_layout, appearance_layout,
                             conn_layout, notif_layout, about_layout):
            _page_layout.addStretch(1)

    # Width of the Settings tab rail on Windows before this was made adaptive.
    # Kept as a floor so the Windows layout is byte-identical to what shipped.
    NAV_MIN_WIDTH = 148
    # Horizontal padding from the QPushButton#navBtn QSS rule ("padding: 9px
    # 14px"). Duplicated here because Qt gives no way to read a stylesheet's
    # box model back off a widget; test_settings_nav_width guards the pairing.
    NAV_BTN_H_PADDING = 14 * 2

    def _size_settings_nav(self, nav_w: QWidget, nav: QVBoxLayout) -> None:
        """Widen the tab rail if the current font needs more than the floor.

        A hard 148px fits Segoe UI but clips "Appearance", "Connection" and
        "Notifications" in the wider fonts Linux desktops ship -- confirmed on
        Ubuntu with Cantarell 11, where those three rendered as "Appearanc",
        "Connectior" and "Notificatior".

        Measured from fontMetrics rather than sizeHint deliberately.
        QPushButton.sizeHint() reports ~250px for these buttons even where 148
        renders perfectly, so sizing from it would balloon the rail on every
        platform. ensurePolished() first: a QSS font-family/font-size is not on
        the widget's own QFont until it has been polished, and measuring before
        that silently uses the default application font.

        Widening only, never narrowing -- if a font measures small we keep the
        original width so the Windows build looks exactly as it always has.
        """
        need = 0
        for btn in self._nav_group.buttons():
            btn.ensurePolished()
            need = max(need, btn.fontMetrics().horizontalAdvance(btn.text()))
        margins = nav.contentsMargins()
        nav_w.setFixedWidth(max(
            self.NAV_MIN_WIDTH,
            need + self.NAV_BTN_H_PADDING + margins.left() + margins.right()))

    def _refresh_dynamic_theme_colors(self) -> None:
        """Ask the owning Dashboard to re-render its dynamic (non-QSS) colours.

        The state this needs (`_last_raw_states`, the shelf render path) lives on
        the Dashboard, not on this panel — an earlier version of this method read
        those off ``self`` behind ``hasattr`` guards, which made it a silent
        no-op. Stylesheet re-application is handled by ``apply_theme`` itself."""
        win = self.window()
        if hasattr(win, "refresh_dynamic_theme_colors"):
            win.refresh_dynamic_theme_colors()

    def _on_theme_selected(self, name: str) -> None:
        if name == theme.CUSTOM:
            self._ensure_custom_seeded()   # copy current theme on first use
        apply_theme(name)
        self._sync_theme_selection()
        self._refresh_dynamic_theme_colors()

    def _ensure_custom_seeded(self) -> None:
        # First time the custom theme is used, seed it from whatever theme is
        # active right now (Nick's choice: copy the current theme).
        if app_settings.get_custom_base() is None:
            theme.set_custom_base(theme.custom_base_from(theme.active()))
            app_settings.set_custom_base(theme.custom_base())

    def _open_custom_editor(self) -> None:
        # Seed from the saved custom theme, or from the current theme the first
        # time ("copy the current theme"). Editing previews in the dialog only;
        # the app changes when the user presses Apply.
        self._ensure_custom_seeded()
        # theme.custom_base() is always the complete, sanitized 8-role base
        # (set_custom_base fills missing/malformed roles), so the editor can't
        # KeyError on a partial or corrupt persisted dict.
        seed = theme.custom_base()
        dlg = CustomThemeEditor(seed, self.window())
        dlg.applied.connect(self._sync_theme_selection)
        dlg.applied.connect(self._refresh_dynamic_theme_colors)
        dlg.exec()
        self._sync_theme_selection()
        self._refresh_dynamic_theme_colors()

    def _on_system_targets_changed(self) -> None:
        # If Follow System is active, re-resolve to the new target right away.
        if theme.selected() == theme.SYSTEM:
            apply_theme(theme.SYSTEM)
        self._sync_theme_selection()
        self._refresh_dynamic_theme_colors()

    def _sync_theme_selection(self) -> None:
        sel = theme.selected()
        self._custom_option.refresh_swatches(theme.custom_palette())
        self._sys_option.refresh_swatches(theme.active())
        self._sys_option.set_selected(sel == theme.SYSTEM)
        self._system_targets.setVisible(sel == theme.SYSTEM)
        self._system_targets.sync()
        self._custom_option.set_selected(sel == theme.CUSTOM)
        is_preset = sel in theme.PRESETS
        self._preset_row.set_selected(is_preset)
        if is_preset:
            self._preset_row.set_current(sel)

    def _refresh_cred_status(self) -> None:
        override = app_settings.get_credentials_override()
        if override:
            self.cred_status.setText(f"Using: {override}")
            self.cred_reset_btn.show()
        else:
            # token_source_description() names the login Keychain on macOS and
            # the default credentials file on Windows/Linux.
            self.cred_status.setText(f"Default: {token_source_description()}")
            self.cred_reset_btn.hide()

    def refresh_token_status(self, *, preserve_message: bool = False) -> None:
        """Show access-token validity; enable manual refresh only when it's
        actually needed (expired / near expiry) so a valid token can't be
        needlessly refreshed into a rate-limit error.

        ``preserve_message`` re-evaluates the CONTROLS but leaves the status
        line alone, for when a refresh failure is on screen and should still be
        readable. Protecting that message by skipping this method wholesale --
        the first attempt -- also froze the button and the checkbox, so the
        warning ended up sitting above a greyed-out button captioned "Token
        valid — refresh disabled", with the one in-app remedy unclickable.
        """
        path = credentials_path()
        # blocking=False because this runs on the UI thread, and SettingsPanel
        # is built during Dashboard construction -- a blocking macOS Keychain
        # read here hung the app before it drew anything, showing the user only
        # a bare password dialog from an app with no Dock icon. The expiry line
        # fills in once the poller has read credentials on its worker.
        exp = token_refresh.token_expiry_ms(path, blocking=False)
        if token_refresh._macos_keychain_active():
            # On macOS the token lives in the login Keychain and write-back isn't
            # implemented, so a manual refresh could only ever return the "not
            # supported" message. Disable the button rather than enable a dead one.
            needs_refresh = False
            keychain_note = (
                "On macOS the token is stored in the login Keychain — run "
                "`claude` to refresh it; Clawdmeter re-reads it automatically."
            )
            self.refresh_token_btn.setEnabled(False)
            self.refresh_token_btn.setText("Managed by the macOS Keychain")
            self.refresh_token_btn.setToolTip(keychain_note)
            # The checkbox has to be disabled for the same reason as the button:
            # auto-refresh cannot run on macOS, so leaving it clickable — and
            # ticked, since it defaults on — tells the user their token is being
            # refreshed automatically when nothing of the sort is happening.
            # Shown unchecked to match reality, WITHOUT writing the setting:
            # blockSignals keeps the stored (Windows/Linux) preference intact.
            self.auto_refresh_check.setEnabled(False)
            self.auto_refresh_check.setToolTip(keychain_note)
            self.auto_refresh_check.blockSignals(True)
            self.auto_refresh_check.setChecked(False)
            self.auto_refresh_check.blockSignals(False)
            # Signing in again is the ONE thing that does work on macOS, and it
            # is exactly what the note above tells the user to do, so this stays
            # live beside two greyed-out controls rather than joining them.
            self._set_reauth_enabled(True, "")
        else:
            self.auto_refresh_check.setEnabled(True)
            self.auto_refresh_check.setToolTip("")
            needs_refresh = token_refresh.is_expired(path)
            self.refresh_token_btn.setEnabled(needs_refresh)
            if needs_refresh:
                self.refresh_token_btn.setText("Refresh token now")
                self.refresh_token_btn.setToolTip("")
            else:
                self.refresh_token_btn.setText("Token valid — refresh disabled")
                self.refresh_token_btn.setToolTip(
                    "Disabled because your token is still valid — it refreshes "
                    "automatically when it expires."
                )
            # Same gate as the refresh button: offered whenever the token is in
            # trouble, disabled WITH A REASON rather than hidden the rest of the
            # time, so it can be found before it is needed.
            self._set_reauth_enabled(needs_refresh, "" if needs_refresh else (
                "Disabled because your token is still valid. Signing in again "
                "replaces your Claude Code credentials."
            ))
        if preserve_message:
            return          # controls are up to date; the message stays put
        # A full re-render replaces whatever set_token_status() put there, so
        # the message is no longer transient. Reached by the user opening or
        # re-entering the Connection tab, or by a refresh actually succeeding --
        # all cases where the stale failure text should go.
        self._token_status_is_transient = False
        if exp is None:
            self.token_status.setText("Token expiry unknown.")
            return
        secs = exp / 1000 - time.time()
        if token_refresh._macos_keychain_active():
            # macOS needs its own wording, not the shared tail below. That tail
            # promises an auto-refresh, and this method has just disabled every
            # mechanism that could deliver one -- the refresh button, and the
            # auto-refresh checkbox. Telling a macOS user to "wait for
            # auto-refresh" beside two greyed-out controls sends them waiting
            # for something that is never coming.
            #
            # Says only what is established. It states where the token came
            # from (Clawdmeter demonstrably reads it from there) and, when
            # expired, the one action known to fix it -- the same guidance
            # token_refresh.refresh() already returns on macOS. It deliberately
            # does NOT claim Claude Code renews the Keychain entry on its own:
            # plausible, but unverified here, and the write-back experiment
            # that would have settled it was inconclusive (three runs, all
            # HTTP 429). Replacing a false promise with an unproven one would
            # not be a fix.
            if secs <= 0:
                self.token_status.setText(
                    "Token expired — run `claude` to re-authenticate.")
            else:
                h, m = int(secs // 3600), int((secs % 3600) // 60)
                self.token_status.setText(
                    f"Valid for ~{h}h {m}m — read from the login Keychain.")
            return
        if secs <= 0:
            self.token_status.setText("Token expired — refresh now, or wait for auto-refresh.")
        elif needs_refresh:
            self.token_status.setText("Token expiring — refresh now, or wait for auto-refresh.")
        else:
            h, m = int(secs // 3600), int((secs % 3600) // 60)
            self.token_status.setText(f"Valid for ~{h}h {m}m — refreshes automatically.")

    def connection_tab_is_current(self) -> bool:
        """True when the Connection tab is the one on screen."""
        idx = self._tab_index.get("Connection")
        return idx is not None and self._stack.currentIndex() == idx

    def _on_settings_tab_changed(self, idx: int) -> None:
        if idx == self._tab_index.get("Connection"):
            self.refresh_token_status()

    def on_shown(self) -> None:
        """The Settings page itself became visible again.

        Needed on top of the tab signal: leaving Settings and coming back does
        not change the sub-tab index, so currentChanged stays silent and the
        line would still be whatever it was minutes ago.
        """
        if self.connection_tab_is_current():
            self.refresh_token_status()

    def set_token_status(self, text: str) -> None:
        """Show a message that outranks the computed status line.

        Callers: a refresh failure ("⚠ ..."), the in-flight "Refreshing…", and
        mock mode. Flagged so the per-sample freshening cannot wipe it -- the
        failure case is the only place the app ever states WHY a refresh
        failed, and the sample after a failed refresh arrives about a second
        later in the same poll cycle, so the reason used to be gone before it
        could be read, leaving the user told to retry what had just failed.

        "Held" rather than "transient": nothing expires it on a timer. It is
        cleared by a full refresh_token_status() -- opening or re-entering the
        Connection tab, or a refresh succeeding. Anything added here that is
        NOT followed by one of those will pin the status line indefinitely.
        """
        self._token_status_is_transient = True
        self.token_status.setText(text)

    def showing_transient_token_status(self) -> bool:
        return getattr(self, "_token_status_is_transient", False)

    def _on_auto_refresh_toggled(self, checked: bool) -> None:
        app_settings.set_auto_refresh(checked)
        if self._on_auto_refresh_changed:
            self._on_auto_refresh_changed(checked)

    def _on_poll_interval_committed(self) -> None:
        """Parse the box, clamp+persist, reflect the applied value back into the
        field, surface an amber note if it was corrected, and push it live."""
        raw = self.poll_interval_edit.text().strip()
        try:
            requested = int(raw)
            parsed = True
        except ValueError:
            requested = app_settings.get_poll_interval()
            parsed = False
        clamped = app_settings.set_poll_interval(requested)
        self.poll_interval_edit.setText(str(clamped))  # reflect what applied

        lo, hi = app_settings.POLL_INTERVAL_MIN, app_settings.POLL_INTERVAL_MAX
        if not parsed:
            self._show_poll_note(f"Enter a whole number {lo}–{hi}s — kept {clamped}.")
        elif clamped != requested:
            self._show_poll_note(f"Adjusted to {clamped}s — allowed range {lo}–{hi}.")
        else:
            self._clear_poll_note()

        if self._on_poll_interval_changed:
            self._on_poll_interval_changed(clamped)

    def _show_poll_note(self, text: str) -> None:
        """Show the amber clamp note at full opacity, then schedule a fade."""
        self._poll_note_hold.stop()
        self._poll_note_fade.stop()
        self.poll_interval_note.setText(text)
        self._poll_note_effect.setOpacity(1.0)
        self.poll_interval_note.show()
        self._poll_note_hold.start()

    def _fade_poll_note(self) -> None:
        self._poll_note_fade.stop()
        self._poll_note_fade.setStartValue(self._poll_note_effect.opacity())
        self._poll_note_fade.start()

    def _clear_poll_note(self, *args) -> None:
        """Hide the note immediately (called on next edit and on fade finish)."""
        self._poll_note_hold.stop()
        self._poll_note_fade.stop()
        self.poll_interval_note.clear()
        self.poll_interval_note.hide()
        self._poll_note_effect.setOpacity(0.0)

    def _on_refresh_token_clicked(self) -> None:
        if self._on_refresh_token:
            self.set_token_status("Refreshing…")
            self._on_refresh_token()

    def _set_reauth_enabled(self, enabled: bool, tooltip: str) -> None:
        """Enable/disable the sign-in button, saying why when it is off.

        A missing CLI does NOT disable it: clicking then reports the command to
        run, which is more use than a dead control with no explanation.
        """
        self.reauth_btn.setEnabled(enabled)
        self.reauth_btn.setToolTip(tooltip)

    def _on_reauth_clicked(self) -> None:
        # start_login never raises; a False comes back as a message that always
        # ends in the command to run, so this cannot dead-end.
        _started, message = reauth.start_login()
        self.set_token_status(message)

    def _on_aot_toggled(self, checked: bool) -> None:
        app_settings.set_always_on_top(checked)
        if self._on_aot_changed:
            self._on_aot_changed(checked)

    def _on_auto_hide_toggled(self, checked: bool) -> None:
        app_settings.set_auto_hide_titlebar(checked)
        if self._on_auto_hide_changed:
            self._on_auto_hide_changed(checked)

    def _on_quit_on_close_toggled(self, checked: bool) -> None:
        app_settings.set_quit_on_close(checked)

    def _on_auto_check_updates_toggled(self, checked: bool) -> None:
        # The running checker reads this each cycle, so no live callback needed.
        app_settings.set_auto_check_updates(checked)

    def _on_check_updates_clicked(self) -> None:
        if self._on_check_updates:
            self._on_check_updates()

    def _on_multi_sessions_toggled(self, checked: bool) -> None:
        app_settings.set_show_multiple_sessions(checked)
        if self._on_sessions_view_changed:
            self._on_sessions_view_changed()

    def _on_subagents_toggled(self, checked: bool) -> None:
        app_settings.set_show_subagents(checked)
        if self._on_sessions_view_changed:
            self._on_sessions_view_changed()

    def _on_token_usage_toggled(self, checked: bool) -> None:
        app_settings.set_show_token_usage(checked)
        if self._on_token_view_changed:
            self._on_token_view_changed()

    _SCOPED_EMPTY_TEXT = "None reported for your account yet."
    _SCOPED_ABSENT_SUFFIX = " — not reported right now"

    def set_scoped_windows(self, seen, current_keys) -> None:
        """Rebuild the ADDITIONAL LIMITS checkboxes: one per remembered
        ``(key, label)``, ticked from the saved choice. ``current_keys`` — the
        keys in the latest usage response, or None before any — marks a
        remembered window that isn't being reported, so ticking it and seeing
        no bar isn't a mystery. Called on every poll; a no-op unless something
        it shows changed."""
        seen = list(seen)
        current = None if current_keys is None else frozenset(current_keys)
        if (seen, current) == self._scoped_state:
            return
        self._scoped_state = (seen, current)
        while self._scoped_col.count():
            item = self._scoped_col.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._scoped_checks = {}
        if not seen:
            empty = QLabel(self._SCOPED_EMPTY_TEXT, objectName="sectionHint")
            empty.setWordWrap(True)
            self._scoped_col.addWidget(empty)
            return
        shown = set(app_settings.get_scoped_shown())
        for key, label in seen:
            text = label
            if current is not None and key not in current:
                text += self._SCOPED_ABSENT_SUFFIX
            check = QCheckBox(text)
            check.setChecked(key in shown)
            check.toggled.connect(self._on_scoped_toggled)
            self._scoped_col.addWidget(check)
            self._scoped_checks[key] = check

    def _on_scoped_toggled(self, _checked: bool) -> None:
        # Save every box's state (not a toggle of one key) so the stored list
        # always matches what's on screen, in Settings order.
        app_settings.set_scoped_shown(
            [k for k, c in self._scoped_checks.items() if c.isChecked()])
        if self._on_scoped_view_changed:
            self._on_scoped_view_changed()

    def _make_threshold_slider(self, label: str, value: int, on_change):
        """A row for an approaching-limit % threshold: window label, a slider over
        the allowed range, and an editable value field. Drag the slider or type a
        number — the two stay in sync. Returns (row, slider, field)."""
        lo, hi = app_settings.APPROACHING_PCT_MIN, app_settings.APPROACHING_PCT_MAX
        row = QHBoxLayout()
        row.setSpacing(10)
        win = QLabel(label)
        win.setMinimumWidth(72)
        slider = QSlider(Qt.Horizontal, objectName="threshold")
        slider.setRange(lo, hi)
        slider.setValue(value)
        # Buttonless spinbox: looks like the value pill but you can click + type.
        field = QSpinBox(objectName="thresholdField")
        field.setButtonSymbols(QAbstractSpinBox.NoButtons)
        field.setRange(lo, hi)
        field.setSuffix("%")
        field.setValue(value)
        field.setAlignment(Qt.AlignCenter)
        field.setFixedWidth(48)

        def from_slider(v: int) -> None:
            field.blockSignals(True); field.setValue(v); field.blockSignals(False)
            on_change(v)

        def from_field(v: int) -> None:
            slider.blockSignals(True); slider.setValue(v); slider.blockSignals(False)
            on_change(v)

        def set_sliding(on: bool) -> None:
            # Show the field's salmon focus border while the slider is dragged,
            # so moving the slider gives the same "active" cue as typing in it.
            field.setProperty("sliding", "true" if on else "false")
            field.style().unpolish(field)
            field.style().polish(field)

        slider.valueChanged.connect(from_slider)
        field.valueChanged.connect(from_field)
        slider.sliderPressed.connect(lambda: set_sliding(True))
        slider.sliderReleased.connect(lambda: set_sliding(False))
        row.addWidget(win)
        row.addWidget(slider, 1)
        row.addWidget(field)
        return row, slider, field

    def _threshold_row(self, lead: str, spin: QSpinBox, trail: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel(lead))
        row.addWidget(spin)
        row.addWidget(QLabel(trail))
        row.addStretch(1)
        return row

    # These thresholds now also drive where the usage bars turn yellow
    # (uiutil.bar_warn_thresholds), so re-render from the last sample instead of
    # leaving the bars on the old colour until the next poll — up to a minute of
    # the settings screen disagreeing with the bar right behind it.
    def _refresh_usage_bar_colors(self) -> None:
        win = self.window()
        if hasattr(win, "refresh_usage_bar_colors"):
            win.refresh_usage_bar_colors()

    def _on_auth_notify_toggled(self, checked: bool) -> None:
        app_settings.set_auth_notify(checked)

    def _on_approaching_toggled(self, checked: bool) -> None:
        app_settings.set_approaching_enabled(checked)
        self._sync_notify_subtoggles()
        self._refresh_usage_bar_colors()

    def _on_session_pct_changed(self, value: int) -> None:
        app_settings.set_approaching_session_pct(value)
        self._refresh_usage_bar_colors()

    def _on_weekly_pct_changed(self, value: int) -> None:
        app_settings.set_approaching_weekly_pct(value)
        self._refresh_usage_bar_colors()

    def _on_overage_alert_toggled(self, checked: bool) -> None:
        app_settings.set_overage_alert_enabled(checked)

    def _on_idle_backoff_toggled(self, checked: bool) -> None:
        app_settings.set_idle_backoff_enabled(checked)
        self.idle_box.setVisible(checked)
        if self._on_idle_backoff_changed:
            self._on_idle_backoff_changed()

    def _on_idle_after_changed(self, value: int) -> None:
        app_settings.set_idle_after_minutes(value)
        if self._on_idle_backoff_changed:
            self._on_idle_backoff_changed()

    def _on_idle_interval_changed(self, value: int) -> None:
        app_settings.set_idle_interval(value)
        if self._on_idle_backoff_changed:
            self._on_idle_backoff_changed()

    def _on_notify_toggled(self, checked: bool) -> None:
        app_settings.set_reset_notify(checked)
        self._sync_notify_subtoggles()

    def _on_notify_toast_toggled(self, checked: bool) -> None:
        app_settings.set_reset_notify_toast(checked)
        self._sync_notify_subtoggles()

    def _on_notify_sound_toggled(self, checked: bool) -> None:
        app_settings.set_reset_notify_sound(checked)

    def _on_notify_popup_toggled(self, checked: bool) -> None:
        app_settings.set_reset_notify_popup(checked)

    def _on_notify_push_toggled(self, checked: bool) -> None:
        app_settings.set_reset_notify_push(checked)
        self._sync_notify_subtoggles()

    def _rebuild_push_channels(self) -> None:
        """Re-render the added-channel rows from settings and refresh the
        Add-a-channel menu (which only offers channels not yet added)."""
        for row in self._push_rows.values():
            self.notify_push_list.removeWidget(row)
            row.deleteLater()
        self._push_rows = {}
        for provider in app_settings.get_reset_notify_push_channels():
            row = _PushChannelRow(provider, PUSH_CHANNEL_NAMES.get(provider, provider))
            row.removed.connect(lambda p=provider: self._remove_push_channel(p))
            self.notify_push_list.addWidget(row)
            self._push_rows[provider] = row
        self._refresh_push_add_menu()

    def _refresh_push_add_menu(self) -> None:
        remaining = [p for p in app_settings.PUSH_PROVIDERS if p not in self._push_rows]
        self.notify_push_add_menu.set_items([
            (PUSH_CHANNEL_NAMES.get(p, p),
             lambda prov=p: self._add_push_channel(prov))
            for p in remaining
        ])
        self.notify_push_add_btn.setEnabled(bool(remaining))

    def _add_push_channel(self, provider: str) -> None:
        chans = app_settings.get_reset_notify_push_channels()
        if provider not in chans:
            chans.append(provider)
            app_settings.set_reset_notify_push_channels(chans)
        self._rebuild_push_channels()
        row = self._push_rows.get(provider)
        if row is not None:
            row.set_editing(True)  # open the new row so the field is ready to fill

    def _remove_push_channel(self, provider: str) -> None:
        chans = [c for c in app_settings.get_reset_notify_push_channels() if c != provider]
        app_settings.set_reset_notify_push_channels(chans)
        self._rebuild_push_channels()

    def _on_test_push_clicked(self) -> None:
        """Send a one-off push with the current settings so the user can verify
        their setup. Runs off the UI thread; the result returns via signal."""
        self.notify_push_test_btn.setEnabled(False)
        self.notify_push_test_status.setText("Sending…")

        def worker() -> None:
            ok, msg = _dispatch_push(
                "Clawdmeter test",
                "If you can see this, your phone notifications are set up.",
            )
            self._push_test_result.emit(ok, msg)

        threading.Thread(target=worker, name="push-test", daemon=True).start()

    def _on_test_push_result(self, ok: bool, msg: str) -> None:
        self.notify_push_test_btn.setEnabled(True)  # re-enable after the send
        # msg = "sent to ntfy, Discord" on success; capitalise + add a nudge.
        nice = (msg[:1].upper() + msg[1:]) if msg else "Sent"
        self.notify_push_test_status.setText(
            f"{nice} — check your notifications." if ok else f"Failed: {msg}"
        )

    def _sync_notify_subtoggles(self) -> None:
        """Show/hide the notification sub-options to match the hierarchy: the
        threshold box follows the approaching master; the shared delivery
        channels appear when ANY alert type is on; each channel's sub-box
        (Windows sound/pop, or the push channel list) hides when its channel is
        off."""
        # Threshold sub-box follows the approaching master.
        self.approaching_box.setVisible(self.approaching_check.isChecked())

        # Shared "how" channels are relevant when any alert type is enabled.
        any_on = self.notify_check.isChecked() or self.approaching_check.isChecked()
        self.notify_how_box.setVisible(any_on)

        # Windows channel sub-box (sound + pop): only when shown + Windows on.
        toast_on = any_on and self.notify_toast_check.isChecked()
        self.notify_toast_box.setVisible(toast_on)

        # Push channel sub-box (the added-channel list): only when shown + push on.
        push_on = any_on and self.notify_push_check.isChecked()
        self.notify_push_box.setVisible(push_on)
        if not push_on:
            self.notify_push_test_status.clear()

    def _on_run_at_startup_toggled(self, checked: bool) -> None:
        ok, msg = run_at_startup.enable() if checked else run_at_startup.disable()
        if not ok:
            QMessageBox.warning(
                self, "Clawdmeter",
                f"Couldn't update the startup setting:\n{msg}")
            # Revert the box to the registry's actual state without re-firing.
            self.startup_check.blockSignals(True)
            self.startup_check.setChecked(run_at_startup.is_enabled())
            self.startup_check.blockSignals(False)

    def _refresh_start_menu_btn(self) -> None:
        if start_menu.has_shortcut():
            self.start_btn.setText("Remove from Start menu")
        else:
            self.start_btn.setText("Add to Start menu")

    def _on_start_menu_toggled(self) -> None:
        if start_menu.has_shortcut():
            ok, msg = start_menu.remove_shortcut()
            if not ok:
                QMessageBox.warning(self, "Clawdmeter", f"Failed to remove shortcut:\n{msg}")
        else:
            ok, msg = start_menu.create_shortcut()
            if ok:
                QMessageBox.information(
                    self, "Clawdmeter",
                    "Added Clawdmeter to your Start menu.\n\n"
                    "Open Start, find Clawdmeter, right-click it, "
                    "and choose Pin to Start.",
                )
            else:
                QMessageBox.warning(self, "Clawdmeter", f"Failed to create shortcut:\n{msg}")
        self._refresh_start_menu_btn()

    def _choose_credentials(self) -> None:
        start = str(credentials_path())
        path, _ = QFileDialog.getOpenFileName(self, "Locate .credentials.json", start, "JSON (*.json)")
        if path:
            os.environ["CLAUDE_CREDENTIALS_PATH"] = path
            app_settings.set_credentials_override(path)
            self._refresh_cred_status()
            QMessageBox.information(self, "Clawdmeter", f"Using:\n{path}\n(next poll within 60s)")

    def _reset_credentials(self) -> None:
        os.environ.pop("CLAUDE_CREDENTIALS_PATH", None)
        app_settings.set_credentials_override("")
        self._refresh_cred_status()


class NavRail(QWidget):
    """Slim vertical icon rail down the content area's left edge — icon-only, with
    names shown via tooltips. The mascot sits at the top; the active page is
    accent-highlighted. Lives only on the full window.

    Parented to root rather than to the content column so it owns the whole left
    edge independently of the title bar — which is why the mascot pinned at its
    top stays put while the title bar auto-hides (Windows/Linux; auto-hide is
    unavailable on macOS, see AUTO_HIDE_SUPPORTED). It spans root's full height
    off macOS; on macOS it starts TOP_INSET px down so its right-edge divider
    runs between the mascot and the native traffic lights."""

    COLLAPSED = 46   # the rail's fixed width

    def __init__(self, parent: QWidget, on_select) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._on_select = on_select

        col = QVBoxLayout(self)
        col.setContentsMargins(6, 6, 6, 10)
        col.setSpacing(4)

        # Mascot at the rail's top-left — where the title-bar icon used to sit,
        # but on the rail so it survives the auto-hide title bar where that
        # exists (Windows/Linux; see AUTO_HIDE_SUPPORTED).
        icon_lbl = QLabel()
        ip = assets_root() / "icon.png"
        if ip.exists():
            icon_lbl.setPixmap(QPixmap(str(ip)).scaled(
                34, 34, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        icon_lbl.setFixedSize(34, 34)
        col.addWidget(icon_lbl, 0, Qt.AlignLeft)
        col.addSpacing(12)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self.dash_btn = self._item(chr(0xF015), "Dashboard", page=0)   # house
        self.stats_btn = self._item(chr(0xF201), "Stats", page=1)      # chart-line
        col.addWidget(self.dash_btn)
        col.addWidget(self.stats_btn)
        col.addStretch(1)
        self.settings_btn = self._item(chr(0xF013), "Settings", page=2)  # gear
        col.addWidget(self.settings_btn)

        self.dash_btn.setChecked(True)
        self._group.idClicked.connect(self._on_select)

    def select(self, page: int) -> None:
        """Move the rail's highlight to ``page`` without emitting a click.

        For when something OTHER than the user picks a destination — e.g. a
        notification click, which has to land on the Dashboard whatever page was
        left open. setChecked doesn't fire idClicked, so the caller drives the
        stack itself and the two can't fight."""
        b = self._group.button(page)
        if b is not None:
            b.setChecked(True)

    def _item(self, glyph: str, label: str, page) -> QPushButton:
        # Icon only; the destination name lives on the tooltip.
        b = QPushButton(glyph, objectName="railBtn")
        b.setCursor(Qt.PointingHandCursor)
        b.setFocusPolicy(Qt.NoFocus)
        b.setToolTip(label)
        b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        if page is not None:
            b.setCheckable(True)
            self._group.addButton(b, page)
        return b

    # macOS: start the rail below the native title-bar row so its right-edge
    # divider runs BETWEEN the mascot and the traffic lights instead of cutting
    # through them; the lights sit in the freed top-left corner.
    TOP_INSET = 28 if sys.platform == "darwin" else 0

    def reposition(self) -> None:
        """Anchor to the parent's left edge, full height (below the macOS
        title-bar row on Mac)."""
        p = self.parentWidget()
        if not p:
            return
        self.setGeometry(0, self.TOP_INSET, self.COLLAPSED,
                         p.height() - self.TOP_INSET)


_PLAN_LABELS = {
    "default_claude_max_20x": "Max 20×",
    "default_claude_max_5x": "Max 5×",
    "default_claude_pro": "Pro",
    "default_claude_free": "Free",
}


def plan_label(tier: str | None) -> str:
    """Human label for an organization.rate_limit_tier (e.g. 'Max 5×')."""
    if not tier:
        return "Claude"
    return _PLAN_LABELS.get(
        tier, tier.replace("default_claude_", "").replace("_", " ").title())


def _fmt_count(n: int) -> str:
    """Compact token/count label: 3_470_000_000 -> '3.5B'."""
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}K"
    return str(int(n))


def _fmt_dur(secs: float) -> str:
    """Compact duration label: 9120 -> '2.5h', 1500 -> '25m', 42 -> '42s'."""
    secs = max(0, int(secs))
    if secs >= 3600:
        return f"{secs / 3600:.1f}h"
    if secs >= 60:
        return f"{secs // 60}m"
    return f"{secs}s"


# Per-language bar colours for the code-by-language breakdown — brand-ish hues
# tuned for contrast on the dark card. Languages without an entry use the
# neutral default; "Other" is the muted grey.
LANGUAGE_COLORS = {
    "Python": "#4FB0E0", "C#": "#A77BE0", "Java": "#E0894B", "Kotlin": "#C792EA",
    "Scala": "#E06A5A", "Groovy": "#A7C0E0", "C/C++": "#F26D9E", "Go": "#39C5CF",
    "Rust": "#E0A584", "Swift": "#F0683B", "Dart": "#3AC2B8", "Ruby": "#E0556A",
    "PHP": "#8A93D0", "JavaScript": "#E6D24A", "TypeScript": "#3D8FE0",
    "Vue": "#54C99A", "Svelte": "#FF5A2B", "HTML": "#E0764A", "CSS": "#B07BD8",
    "Lua": "#6E8AEF", "R": "#6FB7F0", "Shell": "#8FD96B", "PowerShell": "#3B6FB8",
    "Perl": "#C7A04F", "Haskell": "#A77BD0", "Elixir": "#9B7BC0", "Erlang": "#D05A6A",
    "Clojure": "#6FD08A", "F#": "#5AB0C7", "Visual Basic": "#7F9AD0", "Julia": "#B07BD0",
    "Zig": "#E0A04F", "Nim": "#E0D24A", "OCaml": "#E0964F", "Solidity": "#9AA0A6",
    "SQL": "#D9A441", "GraphQL": "#E060A0", "Markdown": "#9CA3AF", "JSON": "#C9A85A",
    "YAML": "#D85C5C", "TOML": "#C58A5A", "XML": "#7FA6CF", "Protobuf": "#7FB0B0",
    "Other": "#6b7280",
}
_LANG_DEFAULT = "#8a93a3"


class _StatsWorker(QThread):
    """Computes the full Stats aggregate off the UI thread (a lifetime transcript
    scan + valuation, cached per file). Emits the aggregate dict when done."""

    ready = Signal(object)

    def run(self) -> None:
        try:
            agg = stats.compute_aggregate(time.time())
        except Exception:
            # Keep a scan crash off the UI, but leave a trace (a blank Stats tab
            # with no diagnostics is worse than a stderr traceback in dev).
            import traceback
            traceback.print_exc()
            return
        self.ready.emit(agg)


class Dashboard(QMainWindow):
    def __init__(self, mock: bool = False) -> None:
        super().__init__()
        self.setWindowTitle("Clawdmeter")
        if sys.platform == "darwin":
            # macOS: a native window (rounded corners + shadow + real traffic
            # lights); macos_window.style() makes the titlebar transparent + full-
            # size content on show, and WA_ContentsMarginsRespectsSafeArea (below)
            # drops Qt's 28px safe-area inset so our chrome fills to the top edge,
            # onto the traffic-lights row. Frameless is Windows/Linux only.
            self.setWindowFlags(Qt.Window)
            self.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        else:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        # The window height tracks its content (see _fit_window_height): it grows
        # and shrinks with the mascot shelf so there's no dead space below the
        # bars. This low floor only stops a manual drag from clipping badly; the
        # actual height is driven by the fit.
        # Width floor only. The HEIGHT floor is left to Qt: every child now
        # declares an honest minimum (the mascots are elastic, the bars and the
        # tile's name/activity/status rows are not), so the layout's own
        # minimumSizeHint is the true floor. The old hard-coded 430 was BELOW
        # what the children needed, which is why Qt clipped them instead of
        # refusing the drag — the mascots were sawn in half by the window edge.
        self._min_window_h = 0
        # Width: ~3 shelf tiles (130px sprites + margins/spacing) fit without
        # scrolling; overflow scrolls horizontally inside the shelf's QScrollArea,
        # so the window never balloons sideways.
        # +NavRail.COLLAPSED so the shelf keeps room for ~3 tiles now that the
        # rail reserves the content's left edge (otherwise 3 sessions scroll).
        self.setMinimumSize(520 + NavRail.COLLAPSED, self._min_window_h)
        self.resize(520 + NavRail.COLLAPSED, 520)
        self.setStyleSheet(STYLESHEET)

        icon_path = assets_root() / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        self._root = root
        if sys.platform == "darwin":
            # Let our content fill to the very top edge (under the titlebar) rather
            # than being auto-inset by the ~28px title-bar safe area. We clear the
            # traffic lights ourselves via the nav-rail top pad + title-bar left pad.
            root.setAttribute(Qt.WA_ContentsMarginsRespectsSafeArea, False)
        # Nav rail down the left (overlay, created after content); the rest of the
        # UI sits in a right column whose left edge is reserved for the collapsed
        # rail. Keeping the rail outside the title bar means it — and the mascot
        # pinned at its top — survive the auto-hide title bar on the platforms
        # that have it. Full height off macOS; inset from the top on macOS so the
        # traffic lights get the corner (see NavRail.TOP_INSET).
        self._outer = QHBoxLayout(root)
        self._outer.setContentsMargins(NavRail.COLLAPSED, 0, 0, 0)
        self._outer.setSpacing(0)

        right_col = QWidget()
        self._outer.addWidget(right_col, 1)
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.title_bar = TitleBar(self, on_toggle=self._toggle_full_compact,
                                  on_mini=lambda: self._set_view_mode("mini"))
        right_layout.addWidget(self.title_bar)

        content = QWidget()
        right_layout.addWidget(content, 1)

        # The mascot + bars now live on a "Dashboard page"; it and the Stats page
        # sit in a stack the nav rail switches between. `layout` still refers to
        # the dashboard page below, so the existing content code is unchanged.
        self.dashboard_page = QWidget()
        layout = QVBoxLayout(self.dashboard_page)
        layout.setContentsMargins(24, 14, 24, 11)
        layout.setSpacing(12)

        # Hero mascot for the EMPTY (0-session) state — the single rate-driven
        # mascot that's been the app's face from day one. Wrapped in its own
        # widget so the whole block can hide as a unit when the shelf is shown.
        # Scales to the height it is given, capped at its long-standing 240px,
        # so a window with room looks as it always has. When there isn't room —
        # additional-limit rows taking height — the mascot shrinks instead of
        # its minimum size forcing the window taller (measured: 572 -> 627px
        # for one row), the same way the session shelf's mascots already fit.
        self.sprite = SpritePlayer(size=HERO_MASCOT_MAX, scale_to_fit=True,
                                   min_size=SessionShelf.MIN_MASCOT)
        self.sprite.setMaximumSize(HERO_MASCOT_MAX, HERO_MASCOT_MAX)
        self.hero = QWidget()
        sprite_row = QHBoxLayout(self.hero)
        sprite_row.setContentsMargins(0, 0, 0, 0)
        sprite_row.addStretch(1)
        sprite_row.addWidget(self.sprite)
        sprite_row.addStretch(1)
        # stretch 1 to match the shelf it shares this slot with, so spare
        # height collects around the mascot rather than under the bars.
        layout.addWidget(self.hero, 1)

        # Shelf of per-session mascots, shown whenever >=1 session is live. It
        # lives in the same slot as the hero and the two toggle visibility so
        # only one occupies the space at a time. Hidden until a session appears.
        self.shelf = SessionShelf()
        self.shelf.hide()
        layout.addWidget(self.shelf, 1)

        # Group label sits 6px above the session row (half of the main
        # layout's 12px) by nesting both into a sub-layout. The sub-layout
        # is then spaced 12px against the rest of the main layout.
        self.group_label = QLabel("IDLE", objectName="group", alignment=Qt.AlignCenter)
        group_session = QVBoxLayout()
        group_session.setContentsMargins(0, 0, 0, 0)
        group_session.setSpacing(6)
        group_session.addWidget(self.group_label)
        self.session_row, self.session_title, self.session_pct, self.session_bar, self.session_reset = self._build_row("SESSION (5h)")
        group_session.addLayout(self.session_row)
        layout.addLayout(group_session)
        self.weekly_row, self.weekly_title, self.weekly_pct, self.weekly_bar, self.weekly_reset = self._build_row("WEEKLY (7d)")
        layout.addLayout(self.weekly_row)

        # Scoped windows ticked in Settings (e.g. WEEKLY · FABLE): full-size
        # rows at the main layout's 12px rhythm. Hidden, it takes no space. The
        # window is not resized for them (see _apply_session_view) — they take
        # their height from the mascot area above.
        self.scoped_rows = ScopedRows(self._make_scoped_row, self._render_scoped_row,
                                      spacing=12)
        layout.addWidget(self.scoped_rows)
        # Scoped-window state: last-known list (kept across a failed usage
        # request) and the keys ticked in Settings, cached off the registry
        # because the 1s countdown reads it.
        self._scoped = scoped_windows.ScopedWindowTracker()
        self._scoped_shown = app_settings.get_scoped_shown()

        # Status badge: only visible when nearing/at the rate limit. When
        # hidden it takes zero vertical space so the WEEKLY bar hugs the
        # bottom; when shown the layout grows to fit it.
        self.status_container = QWidget()
        status_row = QHBoxLayout(self.status_container)
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.status_text = QLabel("", objectName="statusText")
        self.status_icon = QLabel("", objectName="statusIcon")
        status_row.addWidget(self.status_text)
        status_row.addWidget(self.status_icon)
        status_row.addStretch(1)
        self.status_container.setVisible(False)
        layout.addWidget(self.status_container)
        # Deliberately NO trailing stretch. Spare vertical space belongs to the
        # mascot area above (hero/shelf, both stretch 1), not below the last bar
        # — with a stretch here the slack was split 50/50 and every pixel of
        # extra height opened a void under WEEKLY.

        # Page stack (Dashboard + Stats) inside the content area. content_box
        # reserves the collapsed rail's width on the left so content never sits
        # under it; the rail itself is an overlay added after the settings panel.
        self.stats_page = self._build_stats_page()
        self._pages = QStackedWidget()
        self._pages.addWidget(self.dashboard_page)   # index 0
        self._pages.addWidget(self.stats_page)       # index 1
        content_box = QVBoxLayout(content)
        content_box.setContentsMargins(0, 0, 0, 0)  # rail width reserved at root
        content_box.setSpacing(0)
        content_box.addWidget(self._pages)

        # Settings is the third page in the content stack — a nav-rail
        # destination, not an overlay. You leave it by clicking another rail item.
        self._content = content
        self.settings_panel = SettingsPanel(
            content,
            on_always_on_top_changed=self._set_always_on_top,
            on_auto_hide_changed=self._apply_auto_hide,
            on_refresh_token=self._request_token_refresh,
            on_auto_refresh_changed=self._set_auto_refresh,
            on_poll_interval_changed=self._set_poll_interval,
            on_sessions_view_changed=self._apply_session_view,
            on_token_view_changed=self._apply_token_view,
            on_check_updates=self._check_for_updates_now,
            on_idle_backoff_changed=self._apply_poll_cadence,
            on_scoped_view_changed=self._apply_scoped_view,
        )
        self._pages.addWidget(self.settings_panel)   # index 2 (Settings)

        # Slim icon nav rail down root's left edge. Parented to root (not content)
        # so it owns the left side independently of the title bar and the mascot
        # at its top stays put when the title bar auto-hides. Full height off
        # macOS; NavRail.TOP_INSET drops it below the traffic-light row on macOS.
        self.nav_rail = NavRail(root, on_select=self._show_page)
        self.nav_rail.reposition()
        self.nav_rail.raise_()
        root.installEventFilter(self)

        # Apply persisted always-on-top before the first show().
        if app_settings.get_always_on_top():
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        # Auto-hide title bar: the title bar stays in the outer layout. When
        # mouse-revealed it animates its height 0 -> 48 AND the window's
        # height grows by the same delta in lockstep, so the content area
        # never gets squeezed and there's no empty band at the bottom.
        self._tb_max_anim = QPropertyAnimation(self.title_bar, b"maximumHeight", self)
        self._tb_min_anim = QPropertyAnimation(self.title_bar, b"minimumHeight", self)
        self._win_size_anim = QPropertyAnimation(self, b"size", self)
        for _a in (self._tb_max_anim, self._tb_min_anim, self._win_size_anim):
            _a.setDuration(180)
            _a.setEasingCurve(QEasingCurve.OutCubic)
        self._titlebar_anim_group = QParallelAnimationGroup(self)
        for _a in (self._tb_max_anim, self._tb_min_anim, self._win_size_anim):
            self._titlebar_anim_group.addAnimation(_a)
        self._titlebar_anim_group.finished.connect(self._on_titlebar_anim_finished)

        self._mouse_poll = QTimer(self)
        self._mouse_poll.setInterval(80)
        self._mouse_poll.timeout.connect(self._check_titlebar_hover)
        self._auto_hide_enabled = False
        # Window height when the title bar is fully collapsed. Used as an
        # absolute reference so interrupted animations don't accumulate.
        self._collapsed_window_height: int | None = None
        self._apply_auto_hide(app_settings.get_auto_hide_titlebar())

        # Smoothly resizes the window height to fit content as the shelf changes.
        self._fit_anim = QPropertyAnimation(self, b"size", self)
        self._fit_anim.setDuration(240)  # matches the shelf enter/resize animation
        self._fit_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fit_anim.finished.connect(self._on_fit_anim_finished)
        self._fitting = False        # True during our own height snap/animation
        self._fit_armed = False      # don't treat the first show as a user resize
        # Last size observed while the window was settled (see
        # _size_is_settled). None until one is seen, which is why a session
        # that never shows the main window cannot overwrite a good saved size.
        self._last_settled_size: tuple[int, int] | None = None
        # Debounce for persisting the size: a drag emits a resize per frame, and
        # each one would otherwise be a registry write.
        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(600)
        self._size_save_timer.timeout.connect(self._save_window_size)
        # A saved size wins outright. With nothing left that resizes the window
        # on its own, the content snap is a first-run default only -- running it
        # after a restore would throw the user's height away on every launch.
        self._size_restored = self._restore_window_size()

        self._rate = RateGroupTracker()
        self._reset_notifier = ResetNotifier()
        self._approaching_notifier = ApproachingNotifier()
        self._auth_notifier = AuthNotifier()
        # Idle poll back-off: track the last time a local session was active, and
        # whether the poll is currently slowed. Start "active" so a fresh launch
        # polls normally until the idle window elapses.
        self._last_session_activity_ts = time.time()
        self._poll_idle = False
        self._last_sample: UsageSample | None = None
        self._last_tooltip = ""
        self._transcript_state: TranscriptState | None = None
        # Last sessions from the watcher, re-rendered through the Settings toggles.
        self._last_raw_states: list[TranscriptState] = []
        # True while the shelf is showing >=1 live session. Tracked explicitly
        # (not via shelf.isVisible(), which is False until the window is shown)
        # so the usage-poll handler knows to leave the mascots to the shelf.
        self._shelf_active = False

        self._transcript = TranscriptWatcher(self)
        # sessions_changed drives the whole multi-mascot path (shelf + the
        # focused-session mini mascot + empty-state mood). state_changed is
        # the back-compat single-session signal; sessions_changed[0] carries the
        # same focused state, so we listen to the richer one only.
        self._transcript.sessions_changed.connect(self._on_sessions)
        # NOTE: started at the END of __init__, not here. start() does a
        # synchronous first poll that emits sessions_changed, and _on_sessions
        # touches widgets (self.mini, self.sprite, self.shelf) that aren't
        # built until later in __init__ — starting here AttributeErrors on
        # real-mode launch.

        self._tray = QSystemTrayIcon(self)
        self._tray.setIcon(tray_icon(icon_path) if icon_path.exists()
                           else tray_icon(_tray_pixmap(0)))
        tray_menu = QMenu(self)   # native NSMenu on macOS; unaffected
        self._tray_menu = tray_menu   # keep a reference so it isn't GC'd
        show_action = QAction("Show", self)
        show_action.triggered.connect(self._restore_view)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        # Explicit per-mode entries.
        for label, mode in (("Full view", "full"), ("Compact view", "compact"),
                            ("Mini view", "mini")):
            act = QAction(label, self)
            act.triggered.connect(lambda _=False, m=mode: self._set_view_mode(m))
            tray_menu.addAction(act)
        tray_menu.addSeparator()
        # Hidden until a newer release is found; clicking opens the download page.
        self._update_action = QAction("Update available", self)
        self._update_action.setVisible(False)
        self._update_action.triggered.connect(self._open_update_page)
        tray_menu.addAction(self._update_action)
        self._check_updates_action = QAction("Check for updates", self)
        self._check_updates_action.triggered.connect(self._check_for_updates_now)
        tray_menu.addAction(self._check_updates_action)
        tray_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._real_quit)
        tray_menu.addAction(quit_action)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        # Clicking the "update available" balloon should open the release page;
        # messageClicked is global, so the handler checks for a pending update.
        self._tray.messageClicked.connect(self._on_tray_message_clicked)
        self._update_info = None
        self._tray.setToolTip("Clawdmeter - starting…")
        self._tray.show()
        # Whether the desktop actually has a system tray to dock into. On Windows
        # there always is one; on Linux it depends on the DE (e.g. GNOME needs an
        # AppIndicator/Tray extension). main.py reads this to avoid launching
        # invisibly at sign-in, and _maybe_warn_no_tray() surfaces a one-time
        # notice when the window is shown without a tray.
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()

        # When the user is on "Follow System", re-resolve the theme whenever the
        # OS flips light/dark. Ignored for fixed presets (apply_theme no-ops the
        # re-resolve because selected() != SYSTEM).
        QApplication.instance().styleHints().colorSchemeChanged.connect(
            self._on_os_scheme_changed)

        # Tray-flash state for the limit-reset notification.
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(400)
        self._flash_timer.timeout.connect(self._flash_tick)
        self._flash_alert_icon = tray_icon(_tray_alert_pixmap())
        self._flash_saved_icon: QIcon | None = None
        self._flash_remaining = 0
        self._flash_on = False

        # Mini mode: a tiny always-on-top floating widget mirroring usage.
        self._view_mode = "full"
        self._mini_return_mode = "full"   # the view mini was entered from
        self.mini = MiniWidget()
        # Double-click / Expand on mini returns to whichever view it came from.
        self.mini.expand_requested.connect(
            lambda: self._set_view_mode(self._mini_return_mode))
        self.mini.quit_requested.connect(self._real_quit)

        # Compact mode: a denser list view (one row per session).
        self.compact_view = CompactView()
        self.compact_view.set_mode_requested.connect(self._set_view_mode)
        # Double-click the compact title bar also expands to full.
        self.compact_view.grow_requested.connect(lambda: self._set_view_mode("full"))
        self.compact_view.hide_requested.connect(self._stash_compact)  # to tray
        self.compact_view.quit_requested.connect(self._real_quit)

        # Custom limit-reset toast (replaces the native OS notification);
        # clicking it brings the dashboard forward.
        self._toast = ResetToast()
        self._toast.clicked.connect(self._show_window_from_alert)

        self._countdown = QTimer(self)
        self._countdown.setInterval(1000)
        self._countdown.timeout.connect(self._tick_countdown)
        self._countdown.start()

        # Persisted usage history for Stats trends. Disk off in mock so synthetic
        # samples never land in the real on-disk history.
        self.usage_history = UsageHistory(persist=not mock)

        # First run only: pick a snug starting height instead of the arbitrary
        # 520 above. Everything is built by now, so minimumSizeHint is real, and
        # the window has not been shown yet so this is a silent snap rather than
        # a visible resize. A restored size already won, and after this nothing
        # resizes the window again unless the user asks (reset_to_fit).
        if not self._size_restored:
            self._fit_window_height()

        if mock:
            self._start_mock()
        else:
            self._start_poller()
            self._start_update_checker()
            self._start_pricing_refresh()
            # Now that self.mini / self.sprite / self.shelf exist, the
            # watcher's initial synchronous poll can safely drive the shelf.
            self._transcript.start()


    def eventFilter(self, obj, ev):
        if ev.type() == ev.Type.Resize and obj is self._root:
            self.nav_rail.reposition()   # rail spans full root height
        return super().eventFilter(obj, ev)

    def _show_page(self, idx: int) -> None:
        """Switch the content stack to a nav-rail destination (0=Dashboard,
        1=Stats, 2=Settings)."""
        self._pages.setCurrentIndex(idx)
        if idx == 2:
            # Re-entering Settings on the Connection tab: the sub-tab index has
            # not changed, so its own currentChanged never fires.
            self.settings_panel.on_shown()

    def _build_stats_page(self) -> QWidget:
        """Stats page: ROI + extra-usage spend, a per-day value strip, a 7x24
        activity heatmap, and a 'this month' recap. Scrolls when tall."""
        scroll = QScrollArea(objectName="settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: transparent;")
        body = QWidget(objectName="settingsBody")
        scroll.setWidget(body)
        v = QVBoxLayout(body)
        v.setContentsMargins(24, 18, 24, 18)
        v.setSpacing(14)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(QLabel("STATS", objectName="settingsTitle"))
        header.addStretch(1)
        self.stat_plan = QLabel("", objectName="statPlan")
        header.addWidget(self.stat_plan, 0, Qt.AlignVCenter)
        v.addLayout(header)

        def num_card(label: str):
            f = QFrame(objectName="statCard")
            cl = QVBoxLayout(f)
            cl.setContentsMargins(16, 14, 16, 16)
            cl.setSpacing(3)
            cl.addWidget(QLabel(label, objectName="statLabel"))
            big = QLabel("—", objectName="statBig")
            cl.addWidget(big)
            sub = QLabel("", objectName="sectionHint")
            sub.setWordWrap(True)
            cl.addWidget(sub)
            v.addWidget(f)
            return big, sub

        def viz_card(label: str, widget: QWidget, delta: QLabel | None = None) -> None:
            f = QFrame(objectName="statCard")
            cl = QVBoxLayout(f)
            cl.setContentsMargins(16, 14, 16, 14)
            cl.setSpacing(8)
            head = QHBoxLayout()
            head.setContentsMargins(0, 0, 0, 0)
            head.addWidget(QLabel(label, objectName="statLabel"))
            if delta is not None:
                head.addStretch(1)
                head.addWidget(delta, 0, Qt.AlignVCenter)
            cl.addLayout(head)
            cl.addWidget(widget)
            v.addWidget(f)

        def num_pair(label_a: str, label_b: str):
            row = QHBoxLayout()
            row.setSpacing(14)
            outs = []
            for lab in (label_a, label_b):
                f = QFrame(objectName="statCard")
                cl = QVBoxLayout(f)
                cl.setContentsMargins(16, 14, 16, 16)
                cl.setSpacing(3)
                cl.addWidget(QLabel(lab, objectName="statLabel"))
                big = QLabel("—", objectName="statBig")
                cl.addWidget(big)
                sub = QLabel("", objectName="sectionHint")
                sub.setWordWrap(True)
                cl.addWidget(sub)
                row.addWidget(f, 1)
                outs.append((big, sub))
            v.addLayout(row)
            return outs

        # ROI card — API value this month, with lifetime value + break-even day
        # grouped underneath (the "what is my plan worth" block).
        roi = QFrame(objectName="statCard")
        rl = QVBoxLayout(roi)
        rl.setContentsMargins(16, 14, 16, 16)
        rl.setSpacing(3)
        rl.addWidget(QLabel("API VALUE THIS MONTH", objectName="statLabel"))
        self.stat_value = QLabel("—", objectName="statBig")
        rl.addWidget(self.stat_value)
        self.stat_value_sub = QLabel("", objectName="sectionHint")
        self.stat_value_sub.setWordWrap(True)
        rl.addWidget(self.stat_value_sub)
        divider = QFrame(objectName="statDivider")
        rl.addSpacing(6)
        rl.addWidget(divider)
        rl.addSpacing(8)
        extra = QHBoxLayout()
        extra.setContentsMargins(0, 0, 0, 0)
        extra.setSpacing(16)

        def mini(label: str):
            box = QVBoxLayout()
            box.setSpacing(1)
            val = QLabel("—", objectName="statMid")
            box.addWidget(val)
            box.addWidget(QLabel(label, objectName="statLabel"))
            holder = QWidget()
            holder.setLayout(box)
            extra.addWidget(holder, 1)
            return val

        self.stat_lifetime = mini("LIFETIME VALUE")
        self.stat_breakeven = mini("BROKE EVEN")
        rl.addLayout(extra)
        v.addWidget(roi)

        self.stat_spend, self.stat_spend_sub = num_card("EXTRA USAGE THIS MONTH")
        self.stat_cache, self.stat_cache_sub = num_card("CACHE SAVINGS THIS MONTH")
        self.stat_burn, self.stat_burn_sub = num_card("TIME TO 7-DAY CAP")

        (self.stat_streak, self.stat_streak_sub), \
            (self.stat_sessions, self.stat_sessions_sub) = \
            num_pair("CURRENT STREAK", "SESSIONS THIS MONTH")

        self.stat_models = ModelBreakdown()
        viz_card("VALUE BY MODEL", self.stat_models)

        self.stat_projects = ModelBreakdown()
        viz_card("VALUE BY PROJECT", self.stat_projects)

        self.stat_lang = CategoryBars(empty_text="No files edited yet")
        self.stat_lang_count = QLabel("", objectName="statCount")
        viz_card("CODE BY LANGUAGE", self.stat_lang, delta=self.stat_lang_count)

        self.stat_activity = CategoryBars(empty_text="No tool activity yet")
        viz_card("ACTIVITY MIX", self.stat_activity)

        self.stat_week = WeekBars()
        self.stat_week_delta = QLabel("", objectName="statDelta")
        viz_card("THIS WEEK VS LAST", self.stat_week, delta=self.stat_week_delta)

        self.stat_bars = DailyBars()
        viz_card("VALUE PER DAY", self.stat_bars)

        self.stat_heat = Heatmap()
        viz_card("WHEN YOU WORK  ·  LOCAL TIME", self.stat_heat)

        wrap = QFrame(objectName="statCard")
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(16, 14, 16, 16)
        wl.setSpacing(4)
        wl.addWidget(QLabel("THIS MONTH", objectName="statLabel"))
        self.stat_wrap = QLabel("—", objectName="sectionHint")
        self.stat_wrap.setWordWrap(True)
        wl.addWidget(self.stat_wrap)
        v.addWidget(wrap)

        # Per-model usage windows the API reports (e.g. Weekly · Fable 5). The
        # overall 5h/7d windows aren't repeated here — they live on the
        # Dashboard. Kept at the very bottom.
        self.stat_windows = PercentBars(empty_text="No per-model windows reported")
        viz_card("USAGE WINDOWS", self.stat_windows)

        v.addStretch(1)
        return scroll

    def _on_aggregate(self, agg: dict) -> None:
        """Update the Stats trend visuals + recap from a computed aggregate, and
        feed the ROI card's dollar value."""
        self._agg = agg
        self._render_roi()

        cr = agg.get("cache_read_tokens", 0)
        rate = agg.get("cache_hit_rate", 0.0) * 100
        self.stat_cache.setText(f"${agg.get('cache_savings_usd', 0):,.2f}")
        self.stat_cache_sub.setText(
            f"{rate:.0f}% cache hit rate · vs full input price on "
            f"{_fmt_count(cr)} reads")

        self.stat_models.set_data(
            [(stats.model_display(m).replace("Claude ", ""), v)
             for m, v in agg.get("by_model_value", {}).items()])
        self.stat_projects.set_data(list(agg.get("by_project_value", {}).items()))

        # Code by language — distinct files edited, top languages + an Other
        # bucket, drawn with the per-language palette.
        langs = agg.get("language_counts") or {}
        files_edited = agg.get("files_edited", 0)
        total = files_edited or sum(langs.values()) or 1
        named = sorted(((n, c) for n, c in langs.items() if n != "Other"),
                       key=lambda kv: kv[1], reverse=True)
        other = langs.get("Other", 0)
        if other or len(named) > 8:
            other += sum(c for _, c in named[7:])
            named = named[:7]
        lang_rows = [(n, c / total * 100.0, LANGUAGE_COLORS.get(n, _LANG_DEFAULT))
                     for n, c in named]
        if other:
            lang_rows.append(("Other", other / total * 100.0, LANGUAGE_COLORS["Other"]))
        self.stat_lang.set_data(lang_rows)
        self.stat_lang_count.setText(
            f"{files_edited:,} file{'' if files_edited == 1 else 's'}")

        self.stat_bars.set_data(agg.get("value_by_day", []))
        self.stat_heat.set_data(agg.get("heatmap"))

        # Streak + sessions
        cur = agg.get("current_streak", 0)
        best = agg.get("best_streak", 0)
        self.stat_streak.setText(str(cur))
        self.stat_streak_sub.setText(
            f"day{'' if cur == 1 else 's'} · best ever {best}")
        sess = agg.get("sessions") or {}
        self.stat_sessions.setText(f"{sess.get('count', 0):,}")
        self.stat_sessions_sub.setText(
            f"avg {_fmt_dur(sess.get('avg_secs', 0))} · "
            f"longest {_fmt_dur(sess.get('longest_secs', 0))}")

        # Activity breakdown — counts -> % rows with the mascot activity colours.
        counts = agg.get("activity_counts") or {}
        total = sum(counts.values())
        rows = []
        if total:
            for key, n in counts.items():
                try:
                    act = TranscriptActivity(key)
                except ValueError:
                    continue
                rows.append((ACTIVITY_LABELS.get(act, key.upper()),
                             n / total * 100.0,
                             ACTIVITY_COLORS.get(act, theme.active().text_muted)))
            rows.sort(key=lambda r: r[1], reverse=True)
        self.stat_activity.set_data(rows)

        # This week vs last week + delta badge
        wt = agg.get("week_this_usd", 0.0)
        wl = agg.get("week_last_usd", 0.0)
        self.stat_week.set_data(wt, wl)
        if wl > 0:
            pct = (wt - wl) / wl * 100.0
            up = pct >= 0
            self.stat_week_delta.setText(f"{'+' if up else '−'}{abs(pct):.0f}%")
            self.stat_week_delta.setStyleSheet(
                f"color: {theme.active().positive if up else theme.active().danger_strong};")
        elif wt > 0:
            self.stat_week_delta.setText("new")
            self.stat_week_delta.setStyleSheet(f"color: {theme.active().positive};")
        else:
            self.stat_week_delta.setText("")

        # Recap
        parts = []
        tm = agg.get("top_model")
        if tm and tm[1]:
            parts.append(f"Top model — {stats.model_display(tm[0])} (${tm[1]:,.0f})")
        bd = agg.get("busiest_day")
        if bd and bd[1]:
            parts.append(f"Busiest day — {bd[0]:%a %b %d} (${bd[1]:,.0f})")
        rd = agg.get("record_day")
        if rd and rd[1]:
            parts.append(f"Biggest day ever — {rd[0]:%b %d, %Y} (${rd[1]:,.0f})")
        parts.append(f"{agg.get('turns', 0):,} turns over "
                     f"{agg.get('active_days', 0)} active days")
        self.stat_wrap.setText("\n".join(parts))

    def _refresh_stats(self) -> None:
        """Recompute the Stats aggregate off the UI thread (held ref so it lives
        until it finishes). Skips if a scan is still running so we never orphan a
        live QThread or double-fire _on_aggregate with a staler result."""
        worker = getattr(self, "_stats_worker", None)
        if worker is not None and worker.isRunning():
            return
        self._stats_worker = _StatsWorker()
        self._stats_worker.ready.connect(self._on_aggregate)
        self._stats_worker.start()

    def _mock_aggregate(self) -> dict:
        """Synthetic Stats aggregate for --mock (no transcript scan).

        `turns` counts assistant MESSAGES, not JSONL records — one message is
        written as several records (one per content block), so a figure lifted
        from a pre-2026-08-06 build reads ~2.4x too high. Scale any new value
        against `activity_counts` (roughly one to two tool calls per turn), not
        against an old screenshot.
        """
        from datetime import date, timedelta
        today = date.today()
        series = [(today - timedelta(days=n), round(80 + abs(8 - (n % 17)) * 35.0, 2))
                  for n in range(16, -1, -1)]
        heat = [[(2 if 9 <= h <= 19 else 0) * (r + 1) + ((h * (r + 2)) % 7)
                 for h in range(24)] for r in range(7)]
        return {
            "value_total": 3380.0, "value_by_day": series, "heatmap": heat,
            "by_model_value": {"claude-opus-4-8": 2500.0, "claude-sonnet-4-6": 880.0},
            "top_model": ("claude-opus-4-8", 2500.0),
            "busiest_day": (today - timedelta(days=3), 412.0),
            "turns": 6_960, "active_days": 12,
            "cache_savings_usd": 16999.36,
            "cache_read_tokens": 3_470_000_000, "input_tokens": 12_400_000,
            "cache_hit_rate": 3_470_000_000 / (3_470_000_000 + 12_400_000),
            "by_project_value": {"Clawdmeter": 1880.0, "ReadyUp-Dev": 960.0,
                                 "Watchlist-Dev": 540.0},
            "lifetime_value_usd": 48231.0,
            "record_day": (today - timedelta(days=21), 612.0),
            "current_streak": 12, "best_streak": 19,
            "week_this_usd": 1180.0, "week_last_usd": 960.0,
            "sessions": {"count": 147, "avg_secs": 38 * 60, "longest_secs": 2.4 * 3600},
            "activity_counts": {"coding": 5200, "reading": 3100, "planning": 1400,
                                "thinking": 900, "searching": 420, "integrating": 180},
            "language_counts": {"Python": 96, "C#": 28, "Markdown": 14,
                                "TypeScript": 8, "PowerShell": 4, "JSON": 3,
                                "Other": 5},
            "files_edited": 158,
        }

    def _update_stats(self, s: UsageSample) -> None:
        """Per-poll Stats update: the extra-usage spend card (from K1) and the
        ROI card's plan side. The ROI dollar value comes from the aggregate."""
        if s.extra_usage_enabled or s.extra_usage_used_usd:
            self.stat_spend.setText(f"${s.extra_usage_used_usd:,.2f}")
            self.stat_spend_sub.setText(
                f"of ${s.extra_usage_limit_usd:,.2f} cap"
                if s.extra_usage_limit_usd else "pay-as-you-go · no monthly cap")
        else:
            self.stat_spend.setText("$0.00")
            self.stat_spend_sub.setText("Pay-as-you-go off")
        self._render_roi()
        # Scoped usage windows the API reports (e.g. Weekly · Fable) — every
        # one, ticked on the Dashboard or not. The overall 5h/7d windows aren't
        # repeated here. API order (don't sort) keeps rows from reshuffling.
        windows = [(w.label, w.pct) for w in self._scoped.windows]
        self.stat_windows.set_data(windows, sort=False)
        self._update_burn(s)

    def _update_burn(self, s: UsageSample) -> None:
        """Time-to-cap card from the recent 7-day utilisation slope (K2 ring)."""
        if s.weekly_pct >= 100:
            self.stat_burn.setText("over")
            self.stat_burn_sub.setText("7-day window already in overage")
            return
        ring = getattr(self, "usage_history", None)
        points = [(p["ts"], p["w"]) for p in ring.ring] if ring else []
        now = time.time()
        eta = stats.cap_eta(points, s.weekly_pct, now)
        if eta is None:
            span = (points[-1][0] - points[0][0]) if len(points) >= 2 else 0
            if len(points) < 2 or span < 600:
                self.stat_burn.setText("—")
                self.stat_burn_sub.setText("gathering pace data")
            else:
                self.stat_burn.setText("steady")
                self.stat_burn_sub.setText("not on pace to cap")
            return
        secs = eta - now
        reset_secs = (s.weekly_reset_minutes or 0) * 60
        if reset_secs and secs > reset_secs:
            self.stat_burn.setText("clear")
            self.stat_burn_sub.setText("resets before you'd cap · at current pace")
            return
        if secs >= 86400:
            amt = f"~{secs / 86400:.1f} days"
        elif secs >= 3600:
            amt = f"~{secs / 3600:.0f} hours"
        else:
            amt = f"~{max(1, int(secs / 60))} min"
        self.stat_burn.setText(amt)
        self.stat_burn_sub.setText("until the 7-day cap · at current pace")

    def _render_roi(self) -> None:
        """ROI card from the latest aggregate (the dollar value) + the latest
        sample (the plan tier). Either source updating refreshes the card."""
        s = getattr(self, "_last_sample", None)
        tier = s.plan_tier if s else None
        price = stats.plan_monthly_usd(tier)
        self.stat_plan.setText(
            f"{plan_label(tier)} · ${price:,.0f}/mo" if price else plan_label(tier))
        agg = getattr(self, "_agg", None)
        if agg is None:                       # aggregate not computed yet
            self.stat_value.setText("—")
            self.stat_value_sub.setText("computing…")
            self.stat_lifetime.setText("—")
            self.stat_breakeven.setText("—")
            return
        val = agg["value_total"]
        self.stat_value.setText(f"${val:,.2f}")
        if price and val:
            mult = val / price
            mult_s = f"{mult:.0f}×" if mult >= 10 else f"{mult:.1f}×"
            self.stat_value_sub.setText(f"{mult_s} your subscription this month")
        else:
            self.stat_value_sub.setText("of pay-as-you-go API value this month")

        self.stat_lifetime.setText(f"${agg.get('lifetime_value_usd', 0):,.0f}")
        be = stats.break_even_day(agg.get("value_by_day", []), price)
        self.stat_breakeven.setText(f"{be:%b %d}" if be else ("—" if price else "n/a"))

    def _set_always_on_top(self, on: bool) -> None:
        """Zero-flicker topmost via SetWindowPos. Qt's setWindowFlag forces a
        window re-creation, which flickers; SetWindowPos changes the OS-level
        WS_EX_TOPMOST bit on the existing HWND."""
        winutil.set_topmost(self, on)

    def _apply_auto_hide(self, on: bool) -> None:
        """Toggle the auto-hide title bar feature.

        Title bar stays in the outer layout in both modes. When enabled it
        starts collapsed (min/max height 0) and grows on hover. Window
        height is animated in lockstep so the content area never changes
        size — title bar growth pushes the bottom edge down, not into
        content.

        The single gate for AUTO_HIDE_SUPPORTED: forced off on macOS however it
        is reached (settings toggle, or a value persisted on another platform
        and synced over).
        """
        on = on and AUTO_HIDE_SUPPORTED
        if self._auto_hide_enabled == on:
            return
        self._auto_hide_enabled = on
        self._titlebar_anim_group.stop()
        h = TitleBar.HEIGHT

        # The window's own minimum is deliberately NOT adjusted here. Collapsing
        # the title bar to 0 already drops the layout's minimumSizeHint by h, so
        # Qt recomputes the floor by itself. The old code did it by hand --
        # setMinimumHeight(self.minimumHeight() - h) -- and that ran during
        # construction, before the layout had activated, when minimumHeight()
        # was still the explicit 0 from setMinimumSize(). Qt clamped the
        # resulting -48 up to 0 but kept the "explicitly set" flag, so
        # QLayout::activate() never raised the floor again: with auto-hide on,
        # the window had NO height floor for the whole process and could be
        # dragged down to ~120px with the usage bars overlapping. Auto-fit used
        # to snap that back; nothing does now, and the broken height persists.
        if on:
            self.title_bar.setMinimumHeight(0)
            self.title_bar.setMaximumHeight(0)
            new_h = max(self.minimumHeight(), self.height() - h)
            self.resize(self.width(), new_h)
            self._collapsed_window_height = new_h
            self._mouse_poll.start()
        else:
            self._mouse_poll.stop()
            self.title_bar.setMinimumHeight(h)
            self.title_bar.setMaximumHeight(h)
            self.resize(self.width(), self.height() + h)
            self._collapsed_window_height = None

    def _check_titlebar_hover(self) -> None:
        """Reveal title bar when mouse is near the top edge; hide otherwise.

        Hysteresis: when hidden, only the top 8 px reveals; when visible, the
        cursor has to leave the full title-bar region (48 px) to hide.
        """
        local = self.mapFromGlobal(QCursor.pos())
        in_window = self.rect().contains(local)
        visible = self.title_bar.maximumHeight() >= TitleBar.HEIGHT // 2
        threshold = TitleBar.HEIGHT if visible else 8

        if in_window and local.y() < threshold:
            self._reveal_titlebar()
        else:
            self._hide_titlebar()

    def _animate_titlebar_to(self, target_height: int) -> None:
        """Animate title bar height + window height in parallel to absolute
        targets so that restarting the animation mid-flight never accumulates."""
        if self._collapsed_window_height is None:
            return
        current = self.title_bar.maximumHeight()
        if current == target_height:
            return
        target_win_h = self._collapsed_window_height + target_height
        self._titlebar_anim_group.stop()
        self._tb_max_anim.setStartValue(current)
        self._tb_max_anim.setEndValue(target_height)
        self._tb_min_anim.setStartValue(self.title_bar.minimumHeight())
        self._tb_min_anim.setEndValue(target_height)
        self._win_size_anim.setStartValue(self.size())
        self._win_size_anim.setEndValue(QSize(self.width(), target_win_h))
        self._titlebar_anim_group.start()

    def _reveal_titlebar(self) -> None:
        self._animate_titlebar_to(TitleBar.HEIGHT)

    def _hide_titlebar(self) -> None:
        self._animate_titlebar_to(0)

    def nativeEvent(self, eventType, message):
        """Handle WM_NCHITTEST so Windows itself does edge-resize: cursor
        changes, edge snap, etc. all come for free."""
        if eventType == b"windows_generic_MSG":
            msg = winutil.parse_msg(message)
            if msg.message == winutil.WM_NCHITTEST and not self.isMaximized():
                # Use Qt's own logical cursor position so the coordinate space
                # matches mapFromGlobal(). The native lParam is in physical
                # pixels, which mismatches Qt's device-independent geometry
                # under high-DPI scaling (e.g. 200%) and makes the whole client
                # area read as a resize border. See issue #7.
                local = self.mapFromGlobal(QCursor.pos())
                hit = winutil.hit_test(local.x(), local.y(), self.width(), self.height())
                if hit != winutil.HTCLIENT:
                    return True, hit
        return super().nativeEvent(eventType, message)

    def mousePressEvent(self, e) -> None:
        """Off Windows, drive edge/corner resize from a border press.

        Windows resizes via the WM_NCHITTEST path in ``nativeEvent`` above, so
        this is purely additive for the non-Windows compositor route: a left
        press inside the resize border hands off to ``QWindow.startSystemResize``
        (via winutil). Interior presses (HTCLIENT) fall through to the default
        so the title-bar drag-move and all normal input are unaffected.
        """
        if (not winutil.is_windows() and e.button() == Qt.LeftButton
                and not self.isMaximized()):
            pos = e.position().toPoint()
            hit = winutil.hit_test(pos.x(), pos.y(), self.width(), self.height())
            if hit != winutil.HTCLIENT:
                winutil.start_resize(self, winutil.edges_for_hit(hit))
                e.accept()
                return
        super().mousePressEvent(e)

    def _build_row(self, label_text: str):
        outer = QVBoxLayout()
        outer.setSpacing(4)
        header = QHBoxLayout()
        label = QLabel(label_text, objectName="rowLabel")
        pct = QLabel("-", objectName="pct")
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        header.addWidget(label)
        header.addStretch(1)
        header.addWidget(pct)
        bar = UsageBar(height=14)
        reset = QLabel("resets in -", objectName="reset")
        outer.addLayout(header)
        outer.addWidget(bar)
        outer.addWidget(reset)
        return outer, label, pct, bar, reset

    def _make_scoped_row(self):
        outer, label, pct, bar, reset = self._build_row("")
        # A widget's layout gets default margins; the SESSION/WEEKLY rows are
        # bare layouts with none, so without this the row sits inset from them.
        outer.setContentsMargins(0, 0, 0, 0)
        row = QWidget()
        row.setLayout(outer)
        return row, (label, pct, bar, reset)

    @staticmethod
    def _render_scoped_row(parts, window, minutes: int, warn_at: int) -> None:
        label, pct, bar, reset = parts
        # No token figure: the local transcripts can't be split per limit.
        apply_overage_bar(label, pct, bar, window.label.upper(), window.pct, warn_at,
                          over_tag=SCOPED_OVER_TAG)
        reset.setText(scoped_reset_text(window, minutes))

    def _scoped_items(self, s: UsageSample) -> list:
        """``(window, reset_minutes, warn_at)`` for each scoped window ticked in
        Settings, for every view's rows. Reset minutes count down from the
        sample the same way as the 5h/7d windows (_tick_countdown), so a scoped
        window resetting with the weekly one shows the same time."""
        s_warn, w_warn = bar_warn_thresholds()
        elapsed_min = int((time.time() - s.timestamp) // 60)
        return [
            (w, max(0, w.reset_minutes(s.timestamp) - elapsed_min),
             scoped_windows.warn_threshold_for(w, s_warn, w_warn))
            for w in scoped_windows.shown(self._scoped.windows, self._scoped_shown)
        ]

    def _start_poller(self) -> None:
        self._poller = UsagePoller(interval_seconds=app_settings.get_poll_interval())
        self._poller.sample.connect(self._on_sample)
        self._poller.refresh_status.connect(self._on_refresh_status)
        self._poller.start()
        # Stats trend aggregate: compute now + every 10 min, off the UI thread.
        self._refresh_stats()
        self._stats_timer = QTimer(self)
        self._stats_timer.setInterval(600_000)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start()

    def _start_update_checker(self) -> None:
        self._update_checker = UpdateChecker()
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.check_finished.connect(self._on_update_check_finished)
        self._update_checker.start()

    def _start_pricing_refresh(self) -> None:
        # Keeps USD rates current without a new release — see pricing_refresh.
        # No UI hookup needed: a change clears pricing's cache, so the next
        # _refresh_stats() tick (already running every 10 min) just picks it up.
        self._pricing_refresh = PricingRefresher()
        self._pricing_refresh.start()

    def _on_update_available(self, info) -> None:
        """A newer release was found (background or manual check)."""
        self._update_info = info
        self._update_action.setText(f"Update available ({info.version}) — get it")
        self._update_action.setVisible(True)
        self._tray.setToolTip(f"Clawdmeter — update {info.version} available")
        self._toast.show_message(
            "Clawdmeter update available",
            f"Version {info.version} is out — click here or the tray menu to "
            "open the download page.",
            on_click=self._open_update_page,
        )
        self._start_tray_flash()

    def _on_update_check_finished(self, info) -> None:
        """Feedback for a manual 'Check for updates' (info is None when current)."""
        if info is None:
            self._toast.show_message(
                "You're up to date",
                f"Clawdmeter {app_settings.APP_VERSION} is the latest version.",
            )

    def _check_for_updates_now(self) -> None:
        checker = getattr(self, "_update_checker", None)
        if checker is None:
            self._toast.show_message(
                "Clawdmeter", "Update check isn't available in mock mode.")
            return
        self._toast.show_message("Clawdmeter", "Checking for updates…")
        checker.request_check()

    def _open_update_page(self) -> None:
        info = getattr(self, "_update_info", None)
        # There's no in-app installer on any platform — and macOS can't
        # self-replace a running .app — so a detected update opens the release
        # *page* for a manual download. update_check.download_url() picks the
        # safe URL (only this repo on github.com; see its docstring).
        QDesktopServices.openUrl(QUrl(update_check.download_url(info)))

    def _on_tray_message_clicked(self) -> None:
        # Only act if there's a pending update — other balloons are informational.
        if getattr(self, "_update_info", None) is not None:
            self._open_update_page()

    def _request_token_refresh(self) -> None:
        poller = getattr(self, "_poller", None)
        if poller is None:
            self.settings_panel.set_token_status("Not available in mock mode.")
            return
        poller.request_manual_refresh()

    def _set_auto_refresh(self, on: bool) -> None:
        poller = getattr(self, "_poller", None)
        if poller is not None:
            poller.set_auto_refresh(on)

    def _set_poll_interval(self, seconds: int) -> None:
        poller = getattr(self, "_poller", None)
        if poller is not None:
            poller.set_interval(seconds)

    def _note_session_activity(self) -> None:
        """A live session was seen — refresh the activity clock and, if polling
        had backed off while idle, snap straight back to the normal cadence."""
        self._last_session_activity_ts = time.time()
        if self._poll_idle:
            self._poll_idle = False
            poller = getattr(self, "_poller", None)
            if poller is not None:
                poller.set_interval(app_settings.get_poll_interval())
                poller.wake()  # don't wait out the long idle sleep

    def _maybe_backoff_poll(self) -> None:
        """Each poll: slow the cadence when idle back-off is on and no session
        has been active for the idle window; otherwise keep it normal."""
        poller = getattr(self, "_poller", None)
        if poller is None:
            return
        now = time.time()
        enabled = app_settings.get_idle_backoff_enabled()
        idle_after = app_settings.get_idle_after_minutes() * 60
        self._poll_idle = poll_cadence.is_idle(
            now, self._last_session_activity_ts,
            enabled=enabled, idle_after_secs=idle_after)
        poller.set_interval(poll_cadence.target_interval(
            now, self._last_session_activity_ts,
            enabled=enabled,
            normal=app_settings.get_poll_interval(),
            idle_interval=app_settings.get_idle_interval(),
            idle_after_secs=idle_after))

    def _apply_poll_cadence(self, *args) -> None:
        """Re-evaluate the cadence after an idle-back-off setting changes; wake
        the poller if we just dropped out of idle so the change is immediate."""
        was_idle = self._poll_idle
        self._maybe_backoff_poll()
        if was_idle and not self._poll_idle:
            poller = getattr(self, "_poller", None)
            if poller is not None:
                poller.wake()

    def _refresh_token_status_if_watched(self) -> None:
        """Keep the token line live while the user is actually looking at it.

        Replaces an earlier once-per-process latch. That filled the line as
        soon as the poller had warmed the Keychain cache, then froze it -- so
        a countdown could sit at "Valid for ~7h 58m" hours after the token had
        expired, because on macOS nothing else re-renders it (a real refresh
        would, and macOS cannot do one). Recomputing per sample is cheap: it
        reads the cached blob and formats a string, no Keychain call, and only
        when the Connection tab is the page on screen.
        """
        if self._pages.currentIndex() != 2:       # Settings isn't showing
            return
        if not self.settings_panel.connection_tab_is_current():
            return
        # Keep the controls live even when a refresh failure is on screen --
        # only the message is held back. See refresh_token_status().
        self.settings_panel.refresh_token_status(
            preserve_message=self.settings_panel.showing_transient_token_status())

    def _on_refresh_status(self, result) -> None:
        """token_refresh.RefreshResult from the poll thread (auto or manual)."""
        if result.ok:
            self.settings_panel.refresh_token_status()
        else:
            self.settings_panel.set_token_status("⚠ " + result.status)

    def _start_mock(self) -> None:
        self._mock_pct = 12
        self._mock_sample_timer = QTimer(self)
        from datetime import datetime, timezone
        mock_weekly_reset = datetime.fromtimestamp(
            time.time() + (4 * 24 * 60 + 6 * 60) * 60, timezone.utc).isoformat()

        def sample_tick():
            # Cycle 0..129 so both windows cross 100% — the per-window red
            # overage state on the SESSION and WEEKLY bars is visible in mock
            # (weekly is offset so the two cross at different times).
            self._mock_pct = (self._mock_pct + 1) % 130
            self._on_sample(UsageSample(
                session_pct=self._mock_pct,
                session_reset_minutes=140,
                weekly_pct=(self._mock_pct + 65) % 130,
                weekly_reset_minutes=4 * 24 * 60 + 6 * 60,
                status="ok (mock)",
                ok=True,
                error=None,
                timestamp=time.time(),
                tokens_5h=914_000,
                tokens_7d=19_400_000,
                plan_tier="default_claude_max_5x",
                extra_usage_enabled=True,
                extra_usage_used_usd=round(self._mock_pct * 0.3, 2),
                # Shaped like the live API (limits[] names the model "Fable").
                # Fable cycles so its row's yellow/red states show too. Only a
                # window the live API really reports: mock mode uses the real
                # settings, and Settings remembers every window it has seen.
                scoped_windows=scoped_windows.windows_from_limits([
                    {"group": "weekly", "percent": (self._mock_pct + 30) % 130,
                     "resets_at": mock_weekly_reset,
                     "scope": {"model": {"display_name": "Fable"}}},
                ]),
            ))
        self._mock_sample_timer.timeout.connect(sample_tick)
        self._mock_sample_timer.start(800)
        sample_tick()
        self._on_aggregate(self._mock_aggregate())  # synthetic trends for --mock



        # Drive the shelf through a scripted roster that grows 1->4 sessions and
        # shrinks back, reordering and cycling activities along the way, so every
        # transition (tile enter/leave, resize, reorder, live<->idle) is on show
        # without launching concurrent Claude Code windows. Steps every 2s.
        self._mock_phase = 0
        self._mock_shelf_timer = QTimer(self)
        self._mock_shelf_timer.timeout.connect(self._emit_mock_sessions)
        self._mock_shelf_timer.start(2000)
        self._emit_mock_sessions()

    # Pool of fake sessions (session_id, label) the mock roster draws on. Labels
    # mirror the real precedence (custom/ai title, else cwd leaf): a mix of long
    # session titles (elided + tooltip) and short cwd fallbacks.
    _MOCK_POOL = [
        ("mock-clawdmeter", "Review clawdmeter UI design and implementation"),
        ("mock-api-gateway", "api-gateway"),
        ("mock-notes-cli", "Fix login redirect bug"),
        ("mock-data-pipeline", "data-pipeline"),
    ]

    # Each step lists the active pool indices, newest-first. The shelf is driven
    # through add (1->4), reorder (data-pipeline jumps to front) and remove
    # (4->1), exercising the tile enter/leave + count-based resize each step.
    _MOCK_SCHEDULE = [
        [0],
        [0, 1],
        [0, 1, 2],
        [0, 1, 2, 3],
        [3, 0, 1, 2],
        [3, 0, 1],
        [3, 0],
        [0],
    ]

    # Activities the live tiles rotate through, so the per-activity glow colors
    # and animations change over time.
    _MOCK_ACTIVITY_CYCLE = [
        TranscriptActivity.CODING,
        TranscriptActivity.THINKING,
        TranscriptActivity.READING,
        TranscriptActivity.SEARCHING,
        TranscriptActivity.PLANNING,
        TranscriptActivity.INTEGRATING,
    ]

    # The FOCUSED session (the one single mode shows) steps through every
    # expression so all moods are on display for screenshots:
    # (activity, tool label, subagent count, stale).
    _FOCUS_CYCLE = [
        (TranscriptActivity.CODING, "Edit", 0, False),
        (TranscriptActivity.READING, "Read", 0, False),
        (TranscriptActivity.SEARCHING, "WebSearch", 0, False),
        (TranscriptActivity.THINKING, None, 0, False),
        (TranscriptActivity.INTEGRATING, "github/list_issues", 0, False),
        (TranscriptActivity.PLANNING, "TodoWrite", 0, False),
        (TranscriptActivity.PLANNING, None, 3, False),  # supervising subagents
        (TranscriptActivity.IDLE, None, 0, True),       # idle / "last active …"
    ]

    def _mock_agent_list(self, phase: int, n: int) -> list:
        cyc = self._MOCK_ACTIVITY_CYCLE
        return [
            TranscriptAgentState(
                agent_id=f"mock-agent-{k}",
                activity=cyc[(phase + k) % len(cyc)],
                tool_name=None,
                is_stale=False,
            )
            for k in range(n)
        ]

    def _focus_state(self, phase: int, sid: str, project: str, now: float) -> TranscriptState:
        """Build the focused session for this phase, cycling all expressions."""
        act, tool, n_agents, stale = self._FOCUS_CYCLE[phase % len(self._FOCUS_CYCLE)]
        if n_agents:
            return TranscriptState(
                activity=TranscriptActivity.PLANNING, tool_name=None,
                transcript_path=None, last_event_ts=now, session_id=sid, cwd=None,
                project_name=project, is_stale=False,
                agents=self._mock_agent_list(phase, n_agents),
            )
        if stale:
            return TranscriptState(
                activity=TranscriptActivity.IDLE, tool_name=None,
                transcript_path=None, last_event_ts=now - 4 * 60, session_id=sid,
                cwd=None, project_name=project, is_stale=True,
            )
        return TranscriptState(
            activity=act, tool_name=tool, transcript_path=None, last_event_ts=now,
            session_id=sid, cwd=None, project_name=project, is_stale=False,
        )

    def _emit_mock_sessions(self) -> None:
        phase = self._mock_phase
        self._mock_phase += 1
        now = time.time()
        active = self._MOCK_SCHEDULE[phase % len(self._MOCK_SCHEDULE)]
        states = []
        for slot, pool_idx in enumerate(active):
            sid, project = self._MOCK_POOL[pool_idx]
            if slot == 0:
                # Focused session — single mode shows only this, so cycle it
                # through the full expression set.
                states.append(self._focus_state(phase, sid, project, now))
                continue
            # Other tiles (multi mode only): cycle activities; oldest reads idle.
            if slot == len(active) - 1:
                states.append(TranscriptState(
                    activity=TranscriptActivity.IDLE, tool_name=None,
                    transcript_path=None, last_event_ts=now - 4 * 60, session_id=sid,
                    cwd=None, project_name=project, is_stale=True,
                ))
            else:
                act = self._MOCK_ACTIVITY_CYCLE[
                    (phase + pool_idx) % len(self._MOCK_ACTIVITY_CYCLE)
                ]
                states.append(TranscriptState(
                    activity=act,
                    tool_name="Edit" if act == TranscriptActivity.CODING else None,
                    transcript_path=None, last_event_ts=now, session_id=sid, cwd=None,
                    project_name=project, is_stale=False,
                ))
        # Give each mock session a distinct token tally (hover breakdown) and a
        # demo "target" so the sub-label shows what's being acted on.
        mock_targets = {
            TranscriptActivity.CODING: "dashboard.py",
            TranscriptActivity.READING: "transcript.py",
            TranscriptActivity.SEARCHING: "qt elided label",
            TranscriptActivity.INTEGRATING: "list_issues",
        }
        for i, st in enumerate(states):
            base = i + 1
            st.tokens = TokenUsage(
                input=base * 220_000, output=base * 410_000,
                cache_read=base * 9_800_000, cache_write=base * 180_000,
            )
            st.target = mock_targets.get(st.activity)
        self._on_sessions(states)

    def _on_sample(self, s: UsageSample) -> None:
        # The poller has just read credentials on its worker, so the macOS
        # Keychain cache the Settings expiry line reads from is now warm. That
        # line is built with blocking=False (it cannot touch the Keychain from
        # the UI thread), so it starts out blank and needs filling from here.
        # It cannot ride on _on_refresh_status: that only fires on a token
        # REFRESH, which never happens on macOS, so the line would read
        # "unknown" forever on the one platform the non-blocking read exists
        # for. Gated on the line being on screen -- opening the Connection tab
        # re-renders it anyway, so this only has to serve someone already
        # sitting on the page.
        self._refresh_token_status_if_watched()
        if s.ok:
            self._observe_scoped(s)
        # Feed every sample (incl. errors) so the notifiers can ignore them
        # without disturbing their baselines.
        decision = self._reset_notifier.observe(s)
        approaching = self._approaching_notifier.observe(
            s,
            enabled=app_settings.get_approaching_enabled(),
            session_threshold=app_settings.get_approaching_session_pct(),
            weekly_threshold=app_settings.get_approaching_weekly_pct(),
            overage_enabled=app_settings.get_overage_alert_enabled(),
            scoped=scoped_windows.shown(self._scoped.windows, self._scoped_shown),
        )
        # Dispatched here, before the not-ok early return below, because the
        # alert that matters most fires on exactly those samples.
        auth_alert = self._auth_notifier.observe(
            s, enabled=app_settings.get_auth_notify())
        if auth_alert is not None:
            self._dispatch_auth_alert(auth_alert)
        self._last_sample = s
        self.usage_history.record(s)   # ring + throttled disk log (skips errors)
        self._maybe_backoff_poll()     # adjust cadence to local session activity
        if not s.ok:
            self._apply_status_badge(s.status)
            # Same wording as the badge, so the tray and the window can't
            # disagree — and so the tooltip stops reading as a raw status slug.
            failure = FAILURE_BADGES.get((s.status or "").lower())
            self._tray.setToolTip(f"Clawdmeter - {failure[0] if failure else s.status}")
            self._last_tooltip = ""  # force a fresh stats tooltip on recovery
            return

        # Each window handles its own overage: once 5h / 7d crosses 100% the bar
        # restarts red and a red OVERAGE tag joins its title.
        self._render_usage_bars(s)
        self._set_full_scoped_rows(self._scoped_items(s))

        self._refresh_reset_lines(
            s, s.session_reset_minutes, s.weekly_reset_minutes)

        self._sync_mini(s)
        self._update_compact_usage(
            s, s.session_reset_minutes, s.weekly_reset_minutes)
        self._update_stats(s)

        self._rate.observe(s.session_pct)
        # While the shelf is up it owns the mascots (per-session tiles + the
        # mini widget via _drive_mini_from), so the rate-based mood must
        # NOT also drive them here — two paths with different set_anims keys for
        # the same activity would restart and flicker the mini animation on
        # every usage poll. Only refresh the rate mood in the empty state.
        if not self._shelf_active:
            self._update_sprite_selection()

        self._apply_status_badge(s.status)
        self._set_tray_tooltip(s.session_pct, s.session_reset_minutes,
                               s.weekly_pct, s.weekly_reset_minutes)

        # Fire last, so the UI already reflects the post-reset state before we
        # (optionally) pop the window to the foreground.
        if decision.notify and app_settings.get_reset_notify():
            self._fire_reset_notification(decision)
        if approaching:
            self._fire_approaching_notification(approaching)

    def _fire_reset_notification(self, decision: ResetDecision) -> None:
        """Surface a gated limit reset via the user's chosen methods."""
        which = " & ".join(r.capitalize() for r in decision.reasons) or "Usage"
        body = f"{which} limit has reset — you can resume."
        self._deliver_alert("Claude limit reset", body)

    def _observe_scoped(self, s: UsageSample) -> None:
        """Take an OK sample's scoped windows: hold the last-known list (a
        failed usage request, None, keeps it), and remember any newly reported
        window so Settings can offer its checkbox."""
        self._scoped.observe(s.scoped_windows)
        if s.scoped_windows is None:
            return
        seen = app_settings.get_scoped_seen()
        merged = scoped_windows.merge_seen(seen, s.scoped_windows)
        if merged != seen:
            app_settings.set_scoped_seen(merged)
        self.settings_panel.set_scoped_windows(
            merged, [w.key for w in s.scoped_windows])

    def _apply_scoped_view(self) -> None:
        """A scoped-window checkbox changed in Settings: re-render every view's
        rows from the last sample now rather than on the next poll."""
        self._scoped_shown = app_settings.get_scoped_shown()
        s = self._last_sample
        if s is None or not s.ok:
            return
        self._tick_countdown()  # every view's rows + reset lines, current times

    def _set_full_scoped_rows(self, items) -> None:
        """The full window's scoped rows. They take their height from the
        mascot area (the window is not resized), and the shelf's resize event
        can arrive before its viewport has shrunk — measured: a 248px mascot
        left in a 185px viewport, clipped. So once the layout pass the change
        queues has run, re-size the mascots to the viewport they really have."""
        if self.scoped_rows.set_windows(items):
            QTimer.singleShot(0, self.shelf.relayout_tiles)

    def _fire_approaching_notification(self, events: list) -> None:
        """Surface approaching-limit / overage events via the shared channels."""
        overage = any(e.kind == "overage" for e in events)
        reached = any(e.kind == "reached" for e in events)
        if overage:
            title = "Claude overage started"
        elif reached:
            title = "Claude limit reached"
        else:
            title = "Approaching Claude limit"

        def line(e) -> str:
            if e.kind == "overage":
                return f"{e.window} passed 100% — now using paid credits."
            if e.kind == "reached":
                # A scoped window: what happens past 100% is unverified, so say
                # only what is known. See approaching_notify.LimitEvent.
                return f"{e.window} reached 100% of its limit."
            return f"{e.window} is at {e.pct}% of your limit."

        self._deliver_alert(title, "\n".join(line(e) for e in events))

    def _deliver_alert(self, title: str, body: str) -> None:
        """Send an alert over the user's chosen channels (shared by reset and
        approaching alerts). Windows channel: the themed toast + tray flash,
        with sound and window-pop as sub-options. Off -> the push channel can
        still fire, so the user can be notified only on their phone/Discord."""
        if app_settings.get_reset_notify_toast():
            self._toast.show_message(title, body)
            self._start_tray_flash()
            if app_settings.get_reset_notify_sound():
                QApplication.beep()
            if app_settings.get_reset_notify_popup():
                self._show_window()
        if app_settings.get_reset_notify_push():
            self._send_push(title, body)

    def _send_push(self, title: str, body: str) -> None:
        """Fire the phone push off the UI thread; a failure is logged, not raised."""
        if not _push_configured():  # nothing to send to — stay a clean no-op
            return

        def worker() -> None:
            ok, msg = _dispatch_push(title, body)
            # The frozen app runs windowed (console=False), where stderr is None
            # and print() would raise — guard so the failure stays silent-but-safe.
            if not ok and sys.stderr is not None:
                sys.stderr.write(f"[clawdmeter] {msg}\n")

        threading.Thread(target=worker, name="push-notify", daemon=True).start()

    def _start_tray_flash(self, cycles: int = 6) -> None:
        if self._flash_timer.isActive():  # already flashing — just extend it
            self._flash_remaining = max(self._flash_remaining, cycles * 2)
            return
        self._flash_saved_icon = self._tray.icon()  # snapshot the real icon
        self._flash_remaining = cycles * 2
        self._flash_on = False
        self._flash_timer.start()

    def _flash_tick(self) -> None:
        if self._flash_remaining <= 0:
            self._flash_timer.stop()
            if self._flash_saved_icon is not None:
                self._tray.setIcon(self._flash_saved_icon)  # restore exactly
                self._flash_saved_icon = None
            return
        self._flash_on = not self._flash_on
        self._tray.setIcon(
            self._flash_alert_icon if self._flash_on
            else (self._flash_saved_icon or self._tray.icon())
        )
        self._flash_remaining -= 1

    def _set_tray_tooltip(self, session_pct: int, session_reset: int,
                          weekly_pct: int, weekly_reset: int) -> None:
        """Tray hover tooltip with live session/weekly usage. Shared by the
        60s poll and the 1s countdown tick so the reset times stay current."""
        text = (
            "Clawdmeter\n"
            f"Session {session_pct}% (resets {_format_minutes(session_reset)})\n"
            f"Weekly {weekly_pct}% (resets {_format_minutes(weekly_reset)})"
        )
        s = self._last_sample
        if s is not None and app_settings.get_show_token_usage():
            text += (
                f"\nTokens {fmt_tokens(s.tokens_5h)} (5h) · "
                f"{fmt_tokens(s.tokens_7d)} (7d)"
            )
        if text != self._last_tooltip:
            self._last_tooltip = text
            self._tray.setToolTip(text)

    def _restore_window_size(self) -> bool:
        """Reopen at the size the window was left at, clamped to this screen.

        Returns True when a saved size was applied, so the caller knows not to
        snap to content over the top of it.

        Both dimensions, unconditionally. Nothing computes the height any more,
        so a saved height is simply the user's height -- the earlier "only if
        they took manual control" rule existed because the window used to
        re-fit itself in the background, and it no longer does.

        Clamped because the saved size may come from a monitor that is no
        longer attached: a 2000px-tall window restored onto a 1080p laptop
        panel puts its lower half, and often the resize edge, out of reach.
        """
        size = app_settings.get_main_size()
        if size is None:
            return False
        w, h = size
        # Saved as if the title bar were shown (see _save_window_size). The bar
        # starts collapsed when auto-hide is on, so take it back off -- without
        # this the window would open 48px too tall and, because that becomes the
        # new collapsed baseline, stay that way.
        if self._auto_hide_enabled:
            h -= TitleBar.HEIGHT
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            w = min(w, avail.width())
            h = min(h, avail.height())
        self.resize(max(w, self.minimumWidth()), max(h, self.minimumHeight()))
        # Keep the auto-hide baseline in step. resizeEvent normally maintains
        # it, but Qt does not deliver one to a window that has not been shown,
        # so without this the baseline stays at the pre-restore placeholder --
        # which then drove a phantom title-bar reveal to the wrong height.
        if self._auto_hide_enabled:
            self._collapsed_window_height = self.height()
        return True

    def _size_is_settled(self) -> bool:
        """Whether the window's current height is one worth remembering.

        Earlier versions tried to NORMALISE a transient height back to a
        resting one -- subtracting the title bar, rebuilding from the collapsed
        baseline, special-casing normalGeometry(). Each round of that arithmetic
        fixed one case and broke another: the window grew 48px per launch, then
        a never-shown window wrote a stale baseline over a good height, then
        Win+Up maximising walked it 48px DOWN per launch. Observing only
        settled states removes the arithmetic instead of correcting it.

        Unsettled means any of:
          - not visible: a run-at-login start, or a session spent in the
            compact/mini view, never gives the main window a real geometry
          - maximised or full-screen: transient by definition, and the size to
            restore down to was already recorded before the user maximised
          - a title-bar reveal/hide animation in flight: height is mid-tween
          - auto-hide on with the bar revealed: the height includes 48px of
            bar that is not there at rest. The close button lives IN that bar,
            so quitting ALWAYS happens in this state -- which is exactly how
            the 48px-per-launch growth got in.
        """
        if not self.isVisible():
            return False
        if self.isMaximized() or self.isFullScreen():
            return False
        if self._titlebar_anim_group.state() != QAbstractAnimation.Stopped:
            return False
        if self._auto_hide_enabled and self.title_bar.maximumHeight() != 0:
            return False
        return True

    def _remember_settled_size(self) -> None:
        """Snapshot the size if the window is settled; otherwise leave the last
        good one alone.

        Split from the disk write so the two can happen at different moments.
        They must: the last resize before quitting is made at rest, but the
        quit itself happens with the title bar revealed, so a save that read
        the live geometry at quit time would either record the bar or, if it
        refused, lose that final resize entirely.
        """
        if not self._size_is_settled():
            return
        # Stored as if the bar were shown, so the value survives the user
        # toggling auto-hide between sessions. This is the ONLY normalisation
        # left, and it applies to a height already known to be at rest.
        height = self.height() + (TitleBar.HEIGHT if self._auto_hide_enabled else 0)
        self._last_settled_size = (self.width(), height)

    def _save_window_size(self) -> None:
        """Write the last settled size to disk. Debounced; also called on quit."""
        if self._last_settled_size is None:
            return          # no settled size observed this session; keep what's saved
        app_settings.set_main_size(*self._last_settled_size)

    def _target_window_height(self) -> int:
        """Snug window height for the current content: the title bar plus the
        content area, counting the shelf at its settled (target) height rather
        than a value still mid-animation, so the fit aims at the final size."""
        # Derived from the mode, never measured. Reading title_bar.height() got
        # this wrong twice: with auto-hide ON the bar is deliberately collapsed
        # to 0, and the `or TitleBar.HEIGHT` fallback read that as "not laid out
        # yet" and added 48px for a bar that is not there; with auto-hide OFF
        # and the window not yet shown it returned Qt's default 30 rather than
        # the real 48. The mode alone says how much room the bar occupies at
        # rest: none when it auto-hides, a full bar otherwise.
        tb_h = 0 if self._auto_hide_enabled else TitleBar.HEIGHT
        content_min = self._content.minimumSizeHint().height()
        if self._shelf_active:
            content_min += self.shelf.reserved_target() - self.shelf.reserved_current()
        return tb_h + content_min

    def _fit_window_height(self) -> None:
        """Snap the window height to hug its content, with no dead space below
        the bars.

        Only ever called deliberately: once before the first show when there is
        no saved size, and from reset_to_fit() when the user double-clicks the
        title bar. It used to run on every shelf change, badge change and page
        switch, which meant a session starting in the background resized the
        window while the user was doing something else. Mascot sprites are
        scale-to-fit and the shelf reserves a text-only floor, so the content
        absorbs those changes within the height it already has.
        """
        # Only the Dashboard has a content-hugging height. Stats/Settings scroll,
        # so snapping to their content is meaningless.
        if self._pages.currentIndex() != 0:
            return
        if self.isMaximized() or self.isFullScreen():
            return
        if self._titlebar_anim_group.state() == QAbstractAnimation.Running:
            return  # don't fight the auto-hide title-bar resize
        target = max(self.minimumHeight(), self._target_window_height())
        if abs(target - self.height()) <= 1:
            return
        if not self.isVisible():
            self._fitting = True
            self.resize(self.width(), target)  # snap before the first show
            self._fitting = False
            return
        self._fit_anim.stop()
        self._fitting = True  # set AFTER stop() so a restart stays guarded
        self._fit_anim.setStartValue(self.size())
        self._fit_anim.setEndValue(QSize(self.width(), target))
        self._fit_anim.start()

    def _on_fit_anim_finished(self) -> None:
        self._fitting = False
        # Persist the snapped height. resizeEvent cannot do it: the animation's
        # final QResizeEvent is delivered while _fitting is still True (this
        # slot runs after it), so the debounce was never started and the new
        # height only reached disk if the user happened to quit cleanly. That
        # made double-click-to-fit -- now the ONLY way to fix a bad height --
        # the one change most likely to be lost.
        self._remember_settled_size()
        self._size_save_timer.start()

    def _on_titlebar_anim_finished(self) -> None:
        """Snapshot once the title bar has finished collapsing.

        A resize made while the bar was revealed is unsettled, so resizeEvent
        skips the snapshot. The hide animation then lands the window on the
        right height -- resizeEvent kept `_collapsed_window_height` in step
        during the drag, and that is what the hide targets -- but the
        animation's final QResizeEvent is delivered while the group is still
        Running, so nothing records the new height. In practice a later content
        reflow usually does, which is why the loss is intermittent rather than
        reliable; this makes it deterministic instead of a race.

        Safe in the reveal direction: that ends with the bar shown, which
        `_size_is_settled` rejects, so the snapshot is a no-op. An animation
        interrupted by `stop()` never emits `finished`, so a mid-tween height
        can't be recorded either.
        """
        self._remember_settled_size()
        self._size_save_timer.start()

    def reset_to_fit(self) -> None:
        """Snap back to the snug content height — the title-bar double-click.

        Kept deliberately when automatic resizing was removed: it is the manual
        way back to a tidy height after dragging, and with nothing resizing the
        window on its own it is now the ONLY way, which makes it more useful
        than it was, not less.
        """
        if self.isMaximized():
            self.showNormal()
        self._fit_window_height()

    def _macos_apply_native_chrome(self) -> None:
        """Put the NSWindow back the way we want it: transparent titlebar, and
        the persisted always-on-top level. Safe to call repeatedly."""
        macos_window.style(self, theme.active().bg_deep)
        macos_window.set_level(self, app_settings.get_always_on_top())

    def apply_theme_style(self) -> None:
        """Repaint the native NSWindow background in the new palette on a live
        theme switch. Picked up by apply_theme()'s widget-walk broadcast; the
        window's QSS is handled by the normal swap. No-op off macOS."""
        if sys.platform == "darwin":
            macos_window.style(self, theme.active().bg_deep)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "darwin":
            # Transparent titlebar + full-size content view (native traffic lights
            # kept), deferred past Qt's own setup so the style-mask change sticks.
            # Re-applied on EVERY show rather than latched once: anything that
            # makes Qt rebuild the NSWindow hands us a stock window, and a latch
            # keyed on the Python object (or on winId(), which is the NSView and
            # survives the rebuild) would skip the repair. style() is idempotent,
            # so paying it per show is cheaper than another stale-latch bug.
            QTimer.singleShot(0, self._macos_apply_native_chrome)
        # Arm user-resize detection only after the show settles, so the initial
        # show geometry isn't mistaken for a manual height drag.
        QTimer.singleShot(0, lambda: setattr(self, "_fit_armed", True))

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        # A macOS zoom/minimise transition wipes our transparent-titlebar
        # styling; re-apply it once the window is back to a normal state.
        #
        # The isFullScreen() guard looks dead — style() sets FullScreenNone, so
        # the green button zooms and native fullscreen is unreachable. It is not:
        # style() no-ops without pyobjc and returns False, in which case
        # FullScreenNone was never applied and fullscreen IS reachable. Keep the
        # guard so that build doesn't re-style mid-transition.
        #
        # style() only, NOT _macos_apply_native_chrome() — deliberately, and it
        # is not the always-on-top bug it looks like. A code review flagged that
        # this omits set_level(), so always-on-top would be lost on a state
        # change. Measured on the Intel VM (macOS 15.7.7, 2026-08-02) against
        # the live NSWindow level, with a control proving the reader could see a
        # forced 0: the level held at NSFloatingWindowLevel (3) through
        # minimise/restore AND zoom/unzoom. Two reasons, and they differ per
        # transition:
        #   - restore: showEvent fires and runs _macos_apply_native_chrome(),
        #     which DOES set_level(). Confirmed by call-count, 1 -> 2.
        #   - zoom/unzoom: set_level() is never called (count unchanged) and the
        #     level still holds — that transition doesn't disturb it at all.
        # style() itself never touches the level (macos_window.style), so there
        # is nothing here to undo. Adding set_level() would be harmless but
        # unverifiable: no test could fail on its absence.
        if sys.platform == "darwin":
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.Type.WindowStateChange and not self.isFullScreen():
                QTimer.singleShot(
                    0, lambda: macos_window.style(self, theme.active().bg_deep))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Keep the auto-hide collapsed-height baseline in step with manual resizes
        # (only when no title-bar animation is in flight, so animation ticks don't
        # poison it).
        if (self._auto_hide_enabled
                and self._titlebar_anim_group.state() == QAbstractAnimation.Stopped):
            self._collapsed_window_height = self.height() - self.title_bar.maximumHeight()
        if _should_persist_size(self._fit_armed, self._fitting):
            self._remember_settled_size()
            self._size_save_timer.start()

    def _dispatch_auth_alert(self, alert) -> None:
        """Say once that usage can't be read, and once when it can again.

        Goes through _deliver_alert like the reset and approaching alerts, so
        it honours the channels the user already configured — including a
        push-only setup, which is the one that reaches someone who is not
        looking at the window. That is the whole point of this alert.
        """
        self._deliver_alert(alert.title, alert.body)

    def _apply_status_badge(self, status: str) -> None:
        """Show/hide the bottom-left rate-limit badge and reflow the window.

        `anthropic-ratelimit-unified-5h-status` is `allowed` / `allowed_warning`
        / `rejecting` / etc. Only warn and blocked are surfaced. The status
        container is hidden when there's nothing to say so the WEEKLY bar
        sits tight against the bottom; minimum window height grows by the
        badge row's footprint when it appears.

        A FAILED poll carries one of poller's STATUS_* values instead, and those
        are matched exactly, ahead of the substring checks. They have to say
        something: before this, a failed poll fell through to the else branch
        and CLEARED the badge, so an expired token left the 5h / 7d bars frozen
        on their last good values with no explanation anywhere in the window.
        """
        s = (status or "").lower()
        failure = FAILURE_BADGES.get(s)
        if failure:
            text, icon, level = failure
            self.status_text.setText(text)
            self.status_icon.setText(icon)
            self.status_text.setProperty("level", level)
            has_badge = True
        elif "reject" in s or "block" in s:
            self.status_text.setText("Limit reached")
            self.status_icon.setText("❌")
            self.status_text.setProperty("level", "block")
            has_badge = True
        elif "warn" in s:
            self.status_text.setText("Nearing limit")
            self.status_icon.setText("⚠️")
            self.status_text.setProperty("level", "warn")
            has_badge = True
        else:
            self.status_text.clear()
            self.status_icon.clear()
            self.status_text.setProperty("level", "")
            has_badge = False

        self.status_container.setVisible(has_badge)
        # Property selectors don't re-evaluate until restyled.
        self.status_text.style().unpolish(self.status_text)
        self.status_text.style().polish(self.status_text)

        # Deliberately does NOT resize the window. The badge appears and clears
        # on its own schedule, so refitting here moved the window while the user
        # was not touching it. Qt's layout minimum still guarantees the row is
        # not clipped; it just uses the space already there.

    def _reset_line(self, minutes: int, tokens: int) -> str:
        line = f"resets in {_format_minutes(minutes)}"
        if app_settings.get_show_token_usage():
            line += f" · {fmt_tokens(tokens)}"
        return line

    def _refresh_reset_lines(self, s: UsageSample, sr: int, wr: int) -> None:
        """Render the session/weekly reset lines with the per-window token total
        appended when the token display is on. Shared by the poll handler and the
        1s countdown so they never disagree. Overage is part of the same window
        now (the bar/title go red past 100%), so the reset is just the 5h / 7d
        reset — no separate overage clock."""
        self.session_reset.setText(self._reset_line(sr, s.tokens_5h))
        self.weekly_reset.setText(self._reset_line(wr, s.tokens_7d))

    def _tick_countdown(self) -> None:
        s = self._last_sample
        if not s or not s.ok:
            return
        elapsed_min = int((time.time() - s.timestamp) // 60)
        sr = max(0, s.session_reset_minutes - elapsed_min)
        wr = max(0, s.weekly_reset_minutes - elapsed_min)
        self._refresh_reset_lines(s, sr, wr)
        scoped = self._scoped_items(s)
        self._set_full_scoped_rows(scoped)
        self.mini.set_resets(sr, wr, scoped)
        self._update_compact_usage(s, sr, wr)
        self._set_tray_tooltip(s.session_pct, sr, s.weekly_pct, wr)

    def _on_sessions(self, states: list[TranscriptState]) -> None:
        """Receive the watcher's per-session states, remember them, and render
        through the current Settings view (multiple-sessions / subagents toggles)."""
        self._last_raw_states = list(states)
        if states:  # a live session is present -> reset the idle clock
            self._note_session_activity()
        self._apply_session_view()

    def _apply_session_view(self) -> None:
        """Render the last-seen sessions honouring the Settings toggles: collapse
        to the focused session when 'show multiple sessions' is off, and strip
        child agents when 'show subagents' is off. Called on each watcher update
        and whenever a toggle changes, so a flip takes effect immediately.

        With >=1 live session the shelf takes over the mascot slot and the mini
        widget mirrors the focused (newest) session; with 0 the hero returns and
        the rate-based mood drives hero + mini + group_label."""
        states = _view_states(
            self._last_raw_states,
            app_settings.get_show_multiple_sessions(),
            app_settings.get_show_subagents(),
        )

        cv = getattr(self, "compact_view", None)
        if cv is not None and cv.isVisible():
            cv.set_show_tokens(app_settings.get_show_token_usage())
            cv.set_sessions(states)

        if states:
            self._shelf_active = True
            self.hero.hide()
            # Pause the hidden hero so its 240px mascot isn't animating offscreen
            # the whole time the shelf is up.
            self.sprite.stop()
            # The shelf owns its own header, so hide the rate-mood group label
            # rather than leave it showing stale text beside live tiles.
            self.group_label.hide()
            self.shelf.show()
            # The "ACTIVE SESSIONS — N" count is only meaningful in multi mode.
            self.shelf.set_header_visible(app_settings.get_show_multiple_sessions())
            self.shelf.set_show_tokens(app_settings.get_show_token_usage())
            self.shelf.set_sessions(states)
            # The mini widget stays single-mascot, so it follows the focused
            # (newest) session.
            self._transcript_state = states[0]
            self._drive_mini_from(states[0])
        else:
            self._shelf_active = False
            self.shelf.hide()
            self.shelf.set_sessions([])  # drop any leftover tiles + reset header
            self.hero.show()
            self.group_label.show()
            # No session: fall back to today's rate-driven mood for hero/mini.
            self._transcript_state = None
            self._update_sprite_selection()
            # set_anims no-ops on an unchanged key, so re-show the paused hero —
            # but only when the full window is actually the visible view (in
            # compact/mini the hero is hidden and must stay paused).
            if self._view_mode == "full":
                self.sprite.resume()

        # Deliberately does NOT resize the window. Sessions start and end on
        # their own, so following the shelf here meant the window grew and
        # shrank while the user was doing something else -- the single most
        # intrusive thing a background event can do. Mascot sprites are
        # scale-to-fit, so the shelf absorbs the change within whatever height
        # it already has.

    def _apply_token_view(self) -> None:
        """Token-usage display toggled in Settings: re-render the per-bar token
        figures + tray and refresh the shelf's hover breakdown immediately."""
        s = self._last_sample
        if s is not None and s.ok:
            # Toggled ON: the last sample was polled with tokens off (windows 0),
            # so fill them in now rather than showing "· 0" until the next poll.
            if (app_settings.get_show_token_usage()
                    and not s.tokens_5h and not s.tokens_7d):
                try:
                    s.tokens_5h, s.tokens_7d = account_window_tokens(time.time())
                except OSError:
                    pass
            elapsed_min = int((time.time() - s.timestamp) // 60)
            sr = max(0, s.session_reset_minutes - elapsed_min)
            wr = max(0, s.weekly_reset_minutes - elapsed_min)
            self._refresh_reset_lines(s, sr, wr)
            self._update_compact_usage(s, sr, wr)
            self._set_tray_tooltip(s.session_pct, sr, s.weekly_pct, wr)
        # Re-render the shelf so mascot-hover tooltips appear/disappear at once.
        self._apply_session_view()

    def _drive_mini_from(self, state: TranscriptState) -> None:
        """Mirror one session's activity on the mini mascot only — the hero
        is hidden while the shelf is up, so it isn't driven here. Idle/stale
        sessions fall back to the calm group-0 loop."""
        idle = state.is_stale or state.activity == TranscriptActivity.IDLE
        anims = None if idle else ACTIVITY_ANIMS.get(state.activity)
        if anims:
            self.mini.sprite.set_anims(f"mini:{state.activity.value}", anims)
        else:
            self.mini.sprite.set_anims("mini:idle", GROUP_ANIMS[0])

    def _set_sprite_anims(self, key: str, names) -> None:
        """Drive both the full-window and mini mascots in lockstep."""
        self.sprite.set_anims(key, names)
        self.mini.sprite.set_anims(key, names)

    def _update_sprite_selection(self) -> None:
        """Transcript-driven activity takes precedence; rate-based when idle."""
        ts = self._transcript_state
        if ts and ts.activity != TranscriptActivity.IDLE:
            anims = ACTIVITY_ANIMS.get(ts.activity)
            if anims:
                self._set_sprite_anims(f"transcript:{ts.activity.value}", anims)
                label = ACTIVITY_LABELS[ts.activity]
                if ts.tool_name:
                    label = f"{label} — {ts.tool_name}"
                self.group_label.setText(label)
                return

        group_id = self._rate.group()
        self._set_sprite_anims(f"group:{group_id}", GROUP_ANIMS[group_id])
        self.group_label.setText(GROUP_NAMES[group_id].upper())

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._restore_view()

    def _sync_mini(self, s: UsageSample) -> None:
        """Push a usage sample into the mini widget. Reset times use the same
        relative 'resets in 4h 56m' form as the main window."""
        self.mini.update_usage(
            s.session_pct, s.weekly_pct,
            s.session_reset_minutes, s.weekly_reset_minutes,
            self._scoped_items(s),
        )

    # The title-bar button cycles forward (full -> compact -> mini -> full);
    # double-click / "grow" steps back toward full; the tray restores the last
    # mode. Order math lives in module-level next_view_mode/grow_view_mode.
    def _set_view_mode(self, mode: str, persist: bool = True) -> None:
        """Switch to full / compact / mini. Persists the choice unless persist
        is False (transient foregrounding for a notification must not overwrite
        the user's chosen mode)."""
        if mode not in VIEW_ORDER:
            mode = "full"
        # Remember which view we drop into mini FROM, so expanding mini returns
        # there (full->mini->full, compact->mini->compact).
        if mode == "mini" and self._view_mode in ("full", "compact"):
            self._mini_return_mode = self._view_mode
        self._view_mode = mode
        if persist:
            app_settings.set_view_mode(mode)
        # Keep both switchers' active segment in sync with the real mode.
        self.title_bar.set_active_mode(mode)
        self.compact_view.set_active_mode(mode)
        self._stash_mini()
        self._stash_compact()
        if mode == "full":
            self.showNormal()
            self.raise_()
            self.activateWindow()
            # The hero only animates in the empty (0-session) full view; resume
            # it here in case it was paused while hidden in compact/mini.
            if not self._shelf_active:
                self.sprite.resume()
        else:
            # Hidden in compact/mini — pause the full window's hero so its
            # 240px mascot isn't animating offscreen.
            self.sprite.stop()
            self.hide()
            self._show_compact() if mode == "compact" else self._show_mini()

    def _toggle_full_compact(self) -> None:
        """The square-caret toggle: full <-> compact."""
        self._set_view_mode("full" if self._view_mode == "compact" else "compact")

    def _restore_view(self) -> None:
        """Tray click / 'Show' / pop-to-front: re-show the last-used mode without
        re-persisting it (it's already the saved value)."""
        self._set_view_mode(getattr(self, "_view_mode", "full"), persist=False)

    def _render_usage_bars(self, s) -> None:
        """The SESSION/WEEKLY bars for one sample. Each window turns yellow at
        its own approaching-limit threshold (uiutil.bar_warn_thresholds), so the
        two bars deliberately warm up at different points."""
        s_warn, w_warn = bar_warn_thresholds()
        apply_overage_bar(self.session_title, self.session_pct, self.session_bar,
                          "SESSION (5h)", s.session_pct, s_warn)
        apply_overage_bar(self.weekly_title, self.weekly_pct, self.weekly_bar,
                          "WEEKLY (7d)", s.weekly_pct, w_warn)

    def refresh_usage_bar_colors(self) -> None:
        """Re-render every usage bar from the last sample. Called when the
        approaching-limit thresholds change, so the bars recolour as the user
        moves the slider instead of staying wrong until the next poll."""
        s = getattr(self, "_last_sample", None)
        if s is None or not getattr(s, "ok", False):
            return
        self._render_usage_bars(s)
        self._set_full_scoped_rows(self._scoped_items(s))
        self._sync_mini(s)
        self._update_compact_usage(
            s, s.session_reset_minutes, s.weekly_reset_minutes)

    def refresh_dynamic_theme_colors(self) -> None:
        """Re-render the shelf/compact from the last-seen states so the inline
        (non-QSS) colours — session glows, activity dots — pick up the new
        palette immediately instead of lagging until the next poll. Reuses the
        normal per-poll render path, so no bespoke re-colour logic. Safe before
        the first poll (nothing to render yet). Stylesheets are handled by
        apply_theme's widget walk."""
        if hasattr(self, "_last_raw_states"):
            self._apply_session_view()

    def _on_os_scheme_changed(self, _scheme=None) -> None:
        """OS light/dark flipped. Only matters on Follow System — re-resolve to
        the matching preset (apply_theme no-ops for fixed presets)."""
        if theme.selected() == theme.SYSTEM:
            apply_theme(theme.SYSTEM)
            self.refresh_dynamic_theme_colors()

    def show_initial(self) -> None:
        """Launch into the last-used view mode directly (no full-window flash)."""
        self._maybe_warn_no_tray()
        mode = app_settings.get_view_mode()
        if mode == "full":
            self.show()
        else:
            self._set_view_mode(mode, persist=False)

    def _maybe_warn_no_tray(self) -> None:
        """Once, if no system tray was detected, tell the user the tray icon
        won't appear (and, on GNOME, how to fix it).

        Clawdmeter is tray-first, so a silent tray-less launch would look broken.
        Only fires when the window is actually being shown (never during a
        hidden sign-in launch), and only the first time — gated on a QSettings
        flag so it doesn't nag on every open.
        """
        if self.tray_available:
            return
        # Never pop a modal under the offscreen platform (CI launch-smoke): with
        # no one to dismiss it, the dialog would block the event loop yet the
        # smoke only checks "still alive at 8s", so a real hang could slip by.
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        settings = app_settings._settings()
        if settings.value("ui/tray_notice_shown", False, type=bool):
            return
        settings.setValue("ui/tray_notice_shown", True)
        QMessageBox.information(
            self, "Clawdmeter",
            "No system tray was detected, so the Clawdmeter tray icon won't "
            "appear. The window still works normally.\n\n"
            "On GNOME, enable the AppIndicator/Tray extension to get the tray "
            "icon back.",
        )

    def _stash_mini(self) -> None:
        if self.mini.isVisible():
            app_settings.set_mini_pos(self.mini.x(), self.mini.y())
            self.mini.hide()

    def _stash_compact(self) -> None:
        if self.compact_view.isVisible():
            app_settings.set_compact_pos(
                self.compact_view.x(), self.compact_view.y())
            self.compact_view.hide()

    def _default_corner(self, w: int, h: int, top: bool) -> tuple[int, int]:
        scr = self.screen() or QGuiApplication.primaryScreen()
        geo = scr.availableGeometry()
        x = geo.right() - w - 24
        y = (geo.top() + 24) if top else (geo.bottom() - h - 24)
        return x, y

    def _onscreen(self, x: int, y: int, w: int) -> bool:
        """True if a chunk of the window's top edge lands on some screen — so a
        position saved on a now-disconnected monitor doesn't open off-screen
        and ungrabbable."""
        return QGuiApplication.screenAt(QPoint(int(x + w / 2), int(y + 10))) is not None

    def _show_mini(self) -> None:
        self.mini.lock_size()
        pos = app_settings.get_mini_pos()
        if pos is None or not self._onscreen(pos[0], pos[1], self.mini.width()):
            pos = self._default_corner(self.mini.width(), self.mini.height(), top=False)
        self.mini.move(pos[0], pos[1])
        s = self._last_sample
        if s and s.ok:
            self._sync_mini(s)
        self.mini.show()
        self.mini.raise_()
        self.mini.activateWindow()

    def _show_compact(self) -> None:
        self._sync_compact_view()
        pos = app_settings.get_compact_pos()
        if pos is None or not self._onscreen(pos[0], pos[1], self.compact_view.width()):
            pos = self._default_corner(
                self.compact_view.width(), self.compact_view.height(), top=True)
        self.compact_view.move(pos[0], pos[1])
        self.compact_view.show()
        self.compact_view.raise_()
        self.compact_view.activateWindow()

    def _sync_compact_view(self) -> None:
        """Push the current sessions + usage into the compact view."""
        cv = getattr(self, "compact_view", None)
        if cv is None:
            return
        cv.set_show_tokens(app_settings.get_show_token_usage())
        cv.set_sessions(_view_states(
            self._last_raw_states,
            app_settings.get_show_multiple_sessions(),
            app_settings.get_show_subagents(),
        ))
        s = self._last_sample
        if s and s.ok:
            e = int((time.time() - s.timestamp) // 60)
            cv.update_usage(s,
                            max(0, s.session_reset_minutes - e),
                            max(0, s.weekly_reset_minutes - e),
                            app_settings.get_show_token_usage(),
                            self._scoped_items(s))

    def _update_compact_usage(self, s, sr: int, wr: int) -> None:
        cv = getattr(self, "compact_view", None)
        if cv is not None and cv.isVisible():
            cv.update_usage(s, sr, wr, app_settings.get_show_token_usage(),
                            self._scoped_items(s))

    def _show_window(self) -> None:
        """Bring the app to the front for a tray click / second launch. Restores
        whatever mode the user last chose (NOT forced to full) so this never
        silently overwrites their compact/mini choice, and leaves them on the
        page they were last on — they asked for 'the app', not for anything
        specific."""
        self._restore_view()

    def _show_window_from_alert(self) -> None:
        """Toast click. Same as _show_window, but lands on the DASHBOARD
        whatever page was last open.

        Both platforms' guidance is that clicking a notification should open a
        view related to its content — an "at 90% of your limit" alert dropping
        you on the Settings page you happened to leave open is the opposite of
        that. The view MODE (full/compact/mini) is still the user's choice; only
        the page is overridden, and only for an alert."""
        self.nav_rail.select(0)
        self._show_page(0)     # set the page BEFORE showing, so there's no
        self._restore_view()   # flash of whatever page was open

    def closeEvent(self, event) -> None:
        # Minimize to tray unless the user opted into quit-on-close (or there is
        # no system tray to minimize to — e.g. some Linux DEs — in which case
        # closing must actually exit, not hide the window into nowhere).
        # isVisible() reports the icon's requested state, not whether a tray host
        # exists, so gate on the authoritative isSystemTrayAvailable() flag.
        # Re-query now rather than trusting the __init__ snapshot: a tray host can
        # register after startup (Linux autostart racing the panel), and the icon
        # is always show()n, so it docks the moment a host appears.
        self.tray_available = QSystemTrayIcon.isSystemTrayAvailable()
        if app_settings.get_quit_on_close() or not getattr(self, "tray_available", True):
            event.accept()
            self._real_quit()
        else:
            self.hide()
            event.ignore()

    def _real_quit(self) -> None:
        if hasattr(self, "_poller"):
            self._poller.stop()
            self._poller.wait(2000)
        if hasattr(self, "_update_checker"):
            self._update_checker.stop()
            self._update_checker.wait(2000)
        if hasattr(self, "_pricing_refresh"):
            self._pricing_refresh.stop()
            self._pricing_refresh.wait(2000)
        worker = getattr(self, "_stats_worker", None)
        if worker is not None:
            worker.wait(2000)   # don't destroy a QThread mid-scan
        # Flush the debounced size now: quitting inside the debounce window
        # (resize, then straight to the tray menu) would otherwise drop the
        # last resize the user made.
        self._size_save_timer.stop()
        self._save_window_size()
        self._transcript.stop()
        self.sprite.stop()
        self.shelf.stop_all()
        if self.mini.isVisible():
            app_settings.set_mini_pos(self.mini.x(), self.mini.y())
        self.mini.sprite.stop()
        self.mini.close()
        if self.compact_view.isVisible():
            app_settings.set_compact_pos(
                self.compact_view.x(), self.compact_view.y())
        self.compact_view.stop_all()
        self.compact_view.close()
        self._toast.sprite.stop()
        self._toast.close()
        self._tray.hide()
        QGuiApplication.quit()
