"""Mascot shelf — a row of per-session tiles (Variant C).

Each live Claude Code session gets a SessionTile: a mascot with a colored
activity glow, a project name, an activity label and a live/idle status dot.
SessionShelf lays them out in a horizontal scroll area so the window width
stays stable as sessions come and go, and DIFFS by session id on each update
so animations keep running instead of restarting every poll.

The quota bars stay account-wide and live in dashboard.py — this module only
owns the multiplied mascot/activity layer.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSpacerItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontMetrics, QIcon, QPainter,
)

import macos_window
import theme
import winutil
from mood import GROUP_ANIMS
from sprite_player import SpritePlayer, assets_root
from uiutil import (WARN_PCT_DEFAULT, bar_warn_thresholds, make_popup,
                    format_minutes, heat)
from transcript import (
    ACTIVITY_ANIMS,
    ACTIVITY_COLORS,
    ACTIVITY_LABELS,
    Activity,
    AgentState,
    TranscriptState,
    fmt_tokens,
)


# Calm animation used when a tile is idle/stale — the firmware's group-0 roster
# leads with the sleep expression, matching the dashboard's empty-state mood.
_IDLE_ANIMS = GROUP_ANIMS[0]

# Chrome tokens from the app palette (Phase 1 theming). Frozen at import for
# now; Phase 2 adds a refresh hook so a live theme switch restyles the shelf.
_P = theme.active()
_BG = _P.bg
_TEXT = _P.text
_MUTED = _P.text_dim
# Idle indicator (label/dot/glow): a theme role so it stays readable on every
# background — unlike the other activity hues, which are fixed & meaning-bearing.
_IDLE_COLOR = _P.idle

def _build_shelf_qss() -> str:
    return f"""
QWidget#shelfRoot {{ background-color: {_BG}; }}
QScrollArea#shelfScroll {{ background: transparent; border: none; }}
QWidget#shelfRow {{ background: transparent; }}
QLabel#shelfHeader {{
    font-size: 13px; font-weight: 600; color: {_MUTED}; letter-spacing: 2px;
}}
QWidget#sessionTile {{ background: transparent; }}
QToolTip {{
    background-color: {_P.surface}; color: {_TEXT};
    border: 1px solid {_P.border}; padding: 5px 8px;
}}
QLabel#tileActivity {{
    font-size: 11px; font-weight: 600; letter-spacing: 1px;
}}
QLabel#tileDot {{ font-size: 11px; }}
QLabel#tileAgentsText {{ font-size: 10px; color: {_MUTED}; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0 2px; }}
QScrollBar::handle:horizontal {{
    background: {_P.border}; border-radius: 4px; min-width: 24px;
}}
QScrollBar::handle:horizontal:hover {{ background: {_P.border_dim}; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
"""


SHELF_STYLESHEET = _build_shelf_qss()


# Top padding inside a tile so the mascot's glow (drop-shadow blur ~38) isn't
# clipped at the tile's top edge. The mascot sits with internal margin now
# (consistent-crop), so the glow only needs a little extra room above.
_GLOW_PAD = 14

# The tile column's vertical padding: _GLOW_PAD above, 4 below.
_TILE_V_MARGINS = _GLOW_PAD + 4

# The shelf row's own padding (4 top + 4 bottom, see _row's margins). The
# tile is given the viewport MINUS this, so sizing against the raw viewport
# height over-commits every tile by exactly this much.
_ROW_V_MARGINS = 8


def _ago_text(last_event_ts: float | None) -> str:
    """Human 'last active Nm ago' from an event timestamp, for idle tiles."""
    if not last_event_ts:
        return "idle"
    secs = max(0, int(time.time() - last_event_ts))
    if secs < 60:
        return f"last active {secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"last active {mins}m ago"
    hours, m = divmod(mins, 60)
    return f"last active {hours}h {m:02d}m ago"


def _abs_time_text(last_event_ts: float | None) -> str:
    """Absolute local time of the last activity, for the idle tile's tooltip
    (the relative 'Nm ago' is shown inline; hover gives the exact wall time)."""
    if not last_event_ts:
        return ""
    return "Last active " + datetime.fromtimestamp(last_event_ts).strftime(
        "%a %b %d, %I:%M %p"
    )


def _token_tooltip(t) -> str:
    """Per-session token breakdown shown when hovering the mascot. Headline is
    'work' (input+output); the cache buckets are listed separately so the big
    cache-read figure is visible but doesn't distort the headline."""
    return (
        f"This session — {fmt_tokens(t.work)} tokens (input + output)\n"
        f"  input  {fmt_tokens(t.input)}\n"
        f"  output {fmt_tokens(t.output)}\n"
        f"  cache  {fmt_tokens(t.cache_read + t.cache_write)} "
        f"(read {fmt_tokens(t.cache_read)} + write {fmt_tokens(t.cache_write)})"
    )


# Cap for the project/title label. Session titles (from ai-title/custom-title)
# are far longer than a cwd leaf, so the label is capped at this width (or the
# mascot's width if larger): a long title can't stretch a tile far wider than
# its mascot. At rest it's elided; on hover it scrolls to reveal the full text
# (see ScrollingLabel).
_LABEL_MIN_W = 150

# Child-mascot (subagent) sizing.
AGENT_SPRITE = 38
# Column spacing above the agents row (matches the tile's QVBoxLayout spacing).
AGENTS_ROW_SPACING = 4

# Gap between a tile's stacked rows. Named so _content_h and the column that
# lays those rows out cannot drift apart.
TILE_ROW_SPACING = 4
# Height reserved for the agents row before a real one can be measured; once a
# tile has agents the true measured height is used instead (see _agent_extra).
AGENT_ROW_FALLBACK = 46


class ScrollingLabel(QWidget):
    """A single-line title that elides at rest and, on hover, smoothly scrolls
    (ping-pong) to reveal the full text — but only when it doesn't fit. The full
    text is also exposed as a tooltip.

    Hand-painted on purpose: elidedText() must measure with the SAME font we
    render with (a QSS-applied font isn't seen by QFontMetrics, so it would
    mis-measure and never truncate), and QLabel can't scroll its text.
    """

    _START_PAUSE = 0.18      # fraction of the cycle spent paused at each end
    _SPEED_PX_S = 42         # scroll speed in px/sec (drives the duration)
    _END_GAP_PX = 12         # trailing gap so the last glyph isn't flush to edge

    def __init__(self, parent=None, *, px: int = 13, bold: bool = True,
                 color: str | None = None, role: str = "text",
                 letter_spacing: float = 0.5,
                 max_w: int = _LABEL_MIN_W, align=Qt.AlignHCenter) -> None:
        super().__init__(parent)
        self._full = ""
        self._offset = 0
        self._hovering = False
        self._align = align           # how non-scrolling text sits (centre vs left)
        self._explicit_tt: str | None = None   # None = auto (overflow-based)
        self._font = QFont()
        self._font.setPixelSize(px)
        self._font.setBold(bold)
        if letter_spacing:
            self._font.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
        self._fm = QFontMetrics(self._font)
        # A themed role ("text"/"muted") re-reads the live palette on a theme
        # switch (see refresh_theme_color). An explicit `color` is fixed and
        # opts out. Default is "text" so a bare ScrollingLabel() follows the
        # primary text colour rather than baking it at import.
        self._role = None if color else role
        self._color = QColor(color if color else self._role_color(role))
        self.setFixedHeight(self._fm.height())
        self.setMaximumWidth(max_w)
        self._anim = QPropertyAnimation(self, b"scrollOffset", self)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)

    @staticmethod
    def _role_color(role: str) -> str:
        return _MUTED if role == "muted" else _TEXT

    def refresh_theme_color(self) -> None:
        """Re-read the palette for this label's role (called on a theme switch).
        No-op for labels created with an explicit fixed colour."""
        if self._role is not None:
            self._color = QColor(self._role_color(self._role))
            self.update()

    # --- public API (QLabel-like) -------------------------------------------
    def setText(self, text: str, tooltip: str | None = None) -> None:
        """Set the label text. `tooltip=None` → auto (full text shown on hover
        only when it overflows); pass an explicit string to override (e.g. the
        idle line shows the absolute last-active time instead)."""
        text = text or ""
        self._explicit_tt = tooltip
        if text == self._full:
            self._refresh_tooltip()
            return
        self._full = text
        self._offset = 0
        self._refresh_tooltip()
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._full

    # --- geometry -----------------------------------------------------------
    def _avail(self) -> int:
        # Realized width once laid out; the cap as a fallback before show (and in
        # headless tests) so overflow/elision decisions stay deterministic.
        return self.width() or self.maximumWidth()

    def _text_w(self) -> int:
        return self._fm.horizontalAdvance(self._full)

    def _overflows(self) -> bool:
        return self._text_w() > self._avail()

    def sizeHint(self) -> QSize:
        return QSize(min(self._text_w(), self.maximumWidth()), self._fm.height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self._fm.height())

    def _refresh_tooltip(self) -> None:
        if self._explicit_tt is not None:
            self.setToolTip(self._explicit_tt)
        else:
            self.setToolTip(self._full if self._overflows() else "")

    # --- animatable scroll offset -------------------------------------------
    def _get_offset(self) -> int:
        return self._offset

    def _set_offset(self, v: int) -> None:
        self._offset = int(v)
        self.update()

    scrollOffset = Property(int, _get_offset, _set_offset)

    # --- events -------------------------------------------------------------
    def enterEvent(self, e) -> None:
        self._hovering = True
        self._maybe_scroll()
        super().enterEvent(e)

    def leaveEvent(self, e) -> None:
        self._hovering = False
        self._anim.stop()
        self._offset = 0
        self.update()
        super().leaveEvent(e)

    def resizeEvent(self, e) -> None:
        self._refresh_tooltip()
        if self._hovering:
            self._maybe_scroll()
        else:
            self._offset = 0
        super().resizeEvent(e)

    def _maybe_scroll(self) -> None:
        self._anim.stop()
        span = self._text_w() + self._END_GAP_PX - self._avail()
        if span <= 0:
            self._offset = 0
            self.update()
            return
        # Ping-pong: pause, scroll out, pause, scroll back — loop while hovered.
        travel_s = span / self._SPEED_PX_S
        total = max(1.2, 2 * travel_s / (1 - 2 * self._START_PAUSE))
        self._anim.setDuration(int(total * 1000))
        p = self._START_PAUSE
        self._anim.setKeyValueAt(0.0, 0)
        self._anim.setKeyValueAt(p, 0)
        self._anim.setKeyValueAt(0.5 - p / 2, span)
        self._anim.setKeyValueAt(0.5 + p / 2, span)
        self._anim.setKeyValueAt(1.0 - p, 0)
        self._anim.setKeyValueAt(1.0, 0)
        self._anim.setLoopCount(-1)
        self._anim.start()

    def paintEvent(self, e) -> None:
        if not self._full:
            return
        p = QPainter(self)
        p.setFont(self._font)
        p.setPen(self._color)
        rect = self.rect()
        if not self._overflows():
            p.drawText(rect, self._align | Qt.AlignVCenter, self._full)
        elif self._hovering:
            y = self._fm.ascent() + (self.height() - self._fm.height()) // 2
            p.drawText(-self._offset, y, self._full)
        else:
            elided = self._fm.elidedText(self._full, Qt.ElideRight, self._avail())
            p.drawText(rect, self._align | Qt.AlignVCenter, elided)
        p.end()


# Usage-bar palette (was QProgressBar QSS; now painted so the bar can render
# past 100% with a distinct overage colour).
_BAR_TRACK = _P.surface
_BAR_BORDER = _P.border

# The usage-bar fill tracks the theme so the bar reads as part of the palette.
# Sub-100% it ramps from the accent (low usage) through amber to red near the
# limit; over 100% it flips to an alarm red. The shipped default keeps its exact
# hand-tuned salmon ramp and deep overage red so it doesn't change.
_DEFAULT_HEAT = {"cool": "#CE7D6B", "warm": "#B85C42", "hot": "#8B2E1A"}
_DEFAULT_OVERAGE = "#A50F1A"   # deep fire-truck red


def _heat_ramp(p) -> dict:
    if p is theme.MIDNIGHT_SALMON:
        return dict(_DEFAULT_HEAT)
    return {"cool": p.accent, "warm": p.warn, "hot": p.danger}


def _overage_color(p) -> str:
    """The fill once a window is past 100%.

    danger_strong, not danger: the "hot" band below 100% is already p.danger, so
    reusing it left the red band and the overage bar the exact same colour on
    every theme but the shipped default. danger_strong is the darker shade each
    preset already curates (and which custom themes derive as danger blended 20%
    toward black), so overage reads as "past danger" without inventing a colour.
    Midnight Salmon keeps its hand-tuned deep red."""
    return _DEFAULT_OVERAGE if p is theme.MIDNIGHT_SALMON else p.danger_strong


_BAR_HEAT = _heat_ramp(_P)
_BAR_OVERAGE = _overage_color(_P)


class UsageBar(QWidget):
    """A usage bar that handles overage per-window. Below 100% it fills 0–100%
    in its heat colour. Once the window is in overage the heat fill is dropped
    and the bar RESTARTS in red, filling from the left by the amount past 100%
    (so 120% shows a red bar ~1/5 full). Shared by the full window and the
    compact view."""

    def __init__(self, parent=None, *, height: int = 14) -> None:
        super().__init__(parent)
        self._value = 0      # base utilisation 0..100 (drawn when not in overage)
        self._overage = 0    # amount past 100 (0 = none); when >0 the bar is red
        self._heat = "cool"
        self.setFixedHeight(height)

    def set_values(self, value: int, overage: int = 0, heat: str = "cool") -> None:
        value = max(0, int(value))
        overage = max(0, int(overage))
        heat = heat if heat in _BAR_HEAT else "cool"
        if (value, overage, heat) == (self._value, self._overage, self._heat):
            return
        self._value, self._overage, self._heat = value, overage, heat
        self.update()

    def paintEvent(self, e) -> None:
        p = QPainter(self)
        rect = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(QColor(_BAR_BORDER))
        p.setBrush(QColor(_BAR_TRACK))
        p.drawRect(rect)

        inner = rect.adjusted(1, 1, 0, 0)
        w, h, x, y = inner.width(), inner.height(), inner.x(), inner.y()
        p.setPen(Qt.NoPen)
        if self._overage > 0:
            # Overage: the window is maxed, so the heat fill is gone and the bar
            # restarts in red, filling from the left by the amount past 100%
            # (capped at a second full bar at 200%).
            over_w = int(round(w * min(self._overage, 100) / 100))
            if over_w > 0:
                p.setBrush(QColor(_BAR_OVERAGE))
                p.drawRect(x, y, over_w, h)
        else:
            base_w = int(round(w * min(self._value, 100) / 100))
            if base_w > 0:
                p.setBrush(QColor(_BAR_HEAT[self._heat]))
                p.drawRect(x, y, base_w, h)
        p.end()


def apply_overage_bar(title_label, pct_label, bar, title: str, pct: int,
                      warn_at: int = WARN_PCT_DEFAULT, over_tag: str = "OVERAGE") -> None:
    """Render one usage bar's title, percentage and fill with per-window overage.

    Under 100% the bar fills in its heat colour and the title is plain. At/over
    100% the window is in overage: the bar restarts red (the amount past 100),
    the title gains a red OVERAGE tag, and the percent shows the full figure
    (e.g. 120%). Shared by the full window's SESSION/WEEKLY bars and the compact
    view's rows so both behave identically.

    ``warn_at`` is this window's yellow point — see uiutil.bar_warn_thresholds;
    it differs between the 5h and 7d bars, so callers must pass the right one.
    ``over_tag`` is the red word past 100% (SCOPED_OVER_TAG for scoped rows)."""
    pct = max(0, int(pct))
    over = max(0, pct - 100)
    if over > 0:
        title_label.setText(
            f'{title} <span style="color:{_BAR_OVERAGE}; '
            f'font-weight:700">{over_tag}</span>')
        bar.set_values(0, over, "cool")
    else:
        title_label.setText(title)
        bar.set_values(pct, 0, heat(pct, warn_at))
    pct_label.setText(f"{pct}%")


# A scoped row past 100% says OVER LIMIT, not OVERAGE: OVERAGE means paid
# credits on the 5h/7d windows, and whether a model's own cap spills onto them
# is unverified (the alert wording follows the same rule — see
# approaching_notify.LimitEvent).
SCOPED_OVER_TAG = "OVER LIMIT"


def scoped_reset_text(window, minutes: int) -> str:
    """The reset line for a scoped window's row. The usage API has sent a reset
    time for every scoped window seen so far, but one without must not read as
    "resets in 0m"."""
    if window.resets_at is None:
        return "reset time not reported"
    return f"resets in {format_minutes(minutes)}"


class ScopedRows(QWidget):
    """The scoped usage windows' rows (e.g. Weekly · Fable) under a view's own
    5h / 7d bars.

    Each view passes how to build one row and how to draw it, so the rows look
    like that view's existing ones; this class only adds, removes and orders
    them by window key, and hides itself — taking no space or layout spacing —
    when there are none. Shared by the full window, the compact view and the
    mini widget.
    """

    def __init__(self, make_row, render_row, *, spacing: int, top_margin: int = 0,
                 parent=None) -> None:
        super().__init__(parent)
        self._make_row = make_row      # () -> (row widget, parts)
        self._render_row = render_row  # (parts, window, reset_minutes, warn_at)
        self._rows: dict[str, tuple[QWidget, object]] = {}
        self._order: list[str] = []
        self._col = QVBoxLayout(self)
        self._col.setContentsMargins(0, top_margin, 0, 0)
        self._col.setSpacing(spacing)
        self.setVisible(False)

    def keys(self) -> list[str]:
        return list(self._order)

    def set_windows(self, items) -> bool:
        """Show exactly ``items`` — ``(window, reset_minutes, warn_at)`` — in
        order. Returns True when the set or order of rows changed, so a view
        that sizes to its content knows to re-fit."""
        keys = [w.key for w, _m, _warn in items]
        changed = keys != self._order
        if changed:
            for key in [k for k in self._rows if k not in keys]:
                widget, _parts = self._rows.pop(key)
                self._col.removeWidget(widget)
                widget.hide()
                widget.deleteLater()
            # Re-inserting a row already in this layout moves it, so this both
            # places new rows and reorders existing ones.
            for index, key in enumerate(keys):
                if key not in self._rows:
                    self._rows[key] = self._make_row()
                self._col.insertWidget(index, self._rows[key][0])
            self._order = keys
            self.setVisible(bool(keys))
        for window, minutes, warn_at in items:
            self._render_row(self._rows[window.key][1], window, minutes, warn_at)
        return changed

    def settle(self) -> None:
        """Recompute the rows' layouts, then this widget's and every ancestor's,
        now — leaves first, since each activation is what passes a changed size
        up to its parent. For a small window that sizes itself to its content
        (compact, mini), called just before it re-fits: each layout otherwise
        recomputes on its own queued event, so the re-fit reads stale hints.
        Both were measured: removing two rows left the compact view at 261px
        where its content needed 177, and a longer reset text ("resets in 2d
        22h") was clipped because the mini locked its width to the old one.
        Not for the full window, whose shelf sizes itself from resize events —
        forcing its layouts every tick broke the mascot sizing."""
        for widget, _parts in self._rows.values():
            if widget.layout() is not None:
                widget.layout().activate()
        widget = self
        while widget is not None:
            if widget.layout() is not None:
                widget.layout().activate()
            widget = widget.parentWidget()


class AgentMascot(QWidget):
    """A small child mascot for one subagent: a mini sprite with an activity glow
    (no label — the tooltip names its activity/tool)."""

    def __init__(self, agent_id: str, parent=None) -> None:
        super().__init__(parent)
        self._agent_id = agent_id
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.sprite = SpritePlayer(size=AGENT_SPRITE)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(16)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(_IDLE_COLOR))
        self.sprite.setGraphicsEffect(self._glow)
        lay.addWidget(self.sprite, 0, Qt.AlignHCenter)

    def update_state(self, agent: AgentState) -> None:
        idle = agent.is_stale or agent.activity == Activity.IDLE
        color = _IDLE_COLOR if idle else ACTIVITY_COLORS.get(agent.activity, _IDLE_COLOR)
        self._glow.setColor(QColor(color))
        anims = _IDLE_ANIMS if idle else (ACTIVITY_ANIMS.get(agent.activity) or _IDLE_ANIMS)
        self.sprite.set_anims(f"{self._agent_id}:{'idle' if idle else agent.activity.value}", anims)
        label = ACTIVITY_LABELS.get(agent.activity, "")
        self.setToolTip(f"agent · {label}" + (f" — {agent.tool_name}" if agent.tool_name else ""))

    def stop(self) -> None:
        self.sprite.stop()


class SessionTile(QWidget):
    """One session: glowing mascot + project name + activity + status dot,
    plus a row of small child mascots when the session has live subagents.

    The mascot and the subagent row are ELASTIC — they scale with the tile and
    drop out when it gets short. The name, activity and status rows are not:
    they are the facts you read at a glance, so they are what survives.

    Dropping something out is a discrete decision, which Qt's continuous layout
    cannot express on its own — a sprite told to shrink just keeps shrinking,
    which is how the mascots ended up 20px tall instead of gone. Hence the two
    thresholds below, with a deliberate gap between them: hiding a child changes
    the layout, which changes the height, so without hysteresis the tile would
    sit on the boundary flickering the mascot in and out forever.
    """

    # Height the mascot needs before it is worth drawing at all, and the (higher)
    # height at which it comes back. The gap is the hysteresis band.

    # A single status dot reused for both states — the glyph stays, only its
    # color changes (warm/active accent when live, dim grey when idle).
    _DOT = "●"

    def __init__(self, session_id: str, sprite_size: int = 120, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sessionTile")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._session_id = session_id
        self._show_tokens = True   # mascot-hover token breakdown (gated by Settings)

        col = QVBoxLayout(self)
        # Side margins double as the inter-tile gap (the row spacing is 0) so a
        # leaving tile collapses to truly zero width with no leftover gap. The
        # generous TOP margin gives the mascot's drop-shadow glow room to render —
        # child widgets are clipped to the tile, so without it the glow's upper
        # halo is sliced off at the tile's top edge.
        col.setContentsMargins(12, _GLOW_PAD, 12, 4)
        col.setSpacing(TILE_ROW_SPACING)
        col.setAlignment(Qt.AlignHCenter)
        # SetNoConstraint + a zero minimum let the enter/leave width animation
        # shrink the tile below the mascot's fixed size (the content is clipped),
        # so neighbours slide over instead of the tile popping in/out. The row
        # layout still allocates the tile its sizeHint width when unconstrained.
        col.setSizeConstraint(QLayout.SetNoConstraint)
        self.setMinimumWidth(0)
        # Fill the shelf's height. Without this the tile sized itself to its
        # CONTENT and sat at the top of the strip, so the density check read a
        # ~70px tile inside a 500px shelf, concluded there was no room for a
        # mascot, and left the rest as a void above the bars. The tile has to
        # occupy the space before it can decide what fits in it.
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

        # Scale-to-fit: the mascot renders into whatever height the layout
        # gives it, rather than pinning a size that the window then has to grow
        # to accommodate. min_size 0 is what makes "collapse to text-only" a
        # LAYOUT outcome instead of a special case — when the shelf runs out of
        # room the sprite shrinks away and the name / activity / status rows,
        # which have real minimums of their own, are what survive.
        self.sprite = SpritePlayer(size=sprite_size, scale_to_fit=True,
                                   min_size=0)
        # The drop shadow is the colored "glow" behind the mascot. Offset 0 so
        # it haloes evenly; the color is swapped per activity in update_state.
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(38)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(_IDLE_COLOR))
        self.sprite.setGraphicsEffect(self._glow)
        # NO alignment flag: like Qt.AlignTop on the tile, Qt.AlignHCenter here
        # pinned the sprite's WIDTH to its sizeHint, so the mascot could never
        # render wider than 110px however wide the tile got. The QLabel already
        # centres its own pixmap (setAlignment in SpritePlayer), so the flag
        # bought nothing and cost the whole horizontal axis.
        # Leading spacer, inert while a mascot is drawn so the rows stay
        # top-aligned under it. In text-only mode it is made expanding, which
        # pairs it with the trailing stretch below and centres the rows in the
        # space the mascot vacated — rather than leaving all of it underneath,
        # which read as a dead band between the session text and the usage bars.
        #
        # Two matched stretches rather than a computed half-leftover: the
        # trailing stretch also absorbs the column's own margins, so arithmetic
        # here came out 41px above against 55px below. Letting the layout
        # divide it is exact, and it stays exact if the margins ever change.
        self._top_pad = QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Fixed)
        col.insertItem(0, self._top_pad)

        col.addWidget(self.sprite, 0)   # FIXED box; the shelf sizes it

        # Title label: elides at rest, scrolls on hover, full text in a tooltip.
        self.project_label = ScrollingLabel()
        self._label_max_w = max(sprite_size, _LABEL_MIN_W)
        self.project_label.setMaximumWidth(self._label_max_w)
        col.addWidget(self.project_label, 0, Qt.AlignHCenter)

        # Activity + a leading status dot share one row so the dot reads as a
        # badge on the label rather than floating loose.
        act_row = QHBoxLayout()
        act_row.setSpacing(5)
        act_row.setAlignment(Qt.AlignHCenter)
        self.status_dot = QLabel(self._DOT, objectName="tileDot")
        self.activity_label = QLabel("", objectName="tileActivity")
        # Fixed height: a QLabel defaults to Preferred and will happily absorb
        # spare vertical space, which pushed the name / activity / status lines
        # apart whenever the mascot was hidden and its stretch went unused.
        # ScrollingLabel already pins its own height; these two did not.
        for _lbl in (self.status_dot, self.activity_label):
            _lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            _lbl.setFixedHeight(_lbl.sizeHint().height())
        act_row.addWidget(self.status_dot)
        act_row.addWidget(self.activity_label)
        col.addLayout(act_row)

        # Secondary line — the target being acted on (file/pattern/…, else the
        # tool name) when live, "last active Nm ago" when idle. Scrolls on hover
        # like the title, since file paths/queries can be long.
        self.sub_label = ScrollingLabel(px=10, bold=False, role="muted",
                                        letter_spacing=0, max_w=self._label_max_w)
        col.addWidget(self.sub_label, 0, Qt.AlignHCenter)

        # Text stand-in for the subagent mascots, used when the tile is too short
        # to draw them: "3 subagents" says the same thing in one line.
        self.agents_label = QLabel("", objectName="tileAgentsText")
        self.agents_label.setAlignment(Qt.AlignHCenter)
        self.agents_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.agents_label.hide()
        col.addWidget(self.agents_label, 0, Qt.AlignHCenter)

        # Row of child mascots for live subagents — hidden until the session has any.
        self._agents: dict[str, AgentMascot] = {}
        self._agents_box = QWidget()
        self._agents_row = QHBoxLayout(self._agents_box)
        self._agents_row.setContentsMargins(0, 4, 0, 0)
        self._agents_row.setSpacing(6)
        self._agents_row.setAlignment(Qt.AlignHCenter)
        self._agents_box.hide()
        col.addWidget(self._agents_box)
        # Slack collects HERE, below everything. With the sprite taking it
        # instead, a tile with two text rows gave its mascot more height than a
        # tile with three, so the session names started at different heights and
        # the activity/status lines under them never lined up.
        col.addStretch(1)

    def has_agents(self) -> bool:
        return bool(self._agents)

    def agents_box_height(self) -> int:
        """Height the agents row contributes to the tile's sizeHint (0 if empty)."""
        return self._agents_box.sizeHint().height() if self._agents else 0

    def update_agents(self, agents: list[AgentState]) -> None:
        """Diff child mascots by agent id (add/remove/update in place) and show
        the row only while the session has live subagents."""
        incoming = {a.agent_id: a for a in agents}
        for aid in list(self._agents):
            if aid not in incoming:
                m = self._agents.pop(aid)
                self._agents_row.removeWidget(m)
                m.stop()
                m.deleteLater()
        for aid, agent in incoming.items():
            m = self._agents.get(aid)
            if m is None:
                m = AgentMascot(aid, parent=self._agents_box)
                self._agents[aid] = m
                self._agents_row.addWidget(m, 0, Qt.AlignVCenter)
            m.update_state(agent)
        # Presentation is decided shelf-wide; ask the shelf to re-run it rather
        # than deciding locally, or a tile gaining agents overrides the uniform
        # sizing and hides its own mascot.
        shelf = self.parent()
        while shelf is not None and not hasattr(shelf, "_layout_tiles"):
            shelf = shelf.parent()
        if shelf is not None:
            shelf._layout_tiles()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        # Sizing is decided shelf-wide (SessionShelf._layout_tiles) so every tile
        # agrees; the tile only applies what it is told.
        pass

    def _content_h(self) -> int:
        """Exact height of this tile's visible text rows, spacing included.

        Computed the way the column actually lays them out — the sum of the
        shown rows plus one spacing between each — rather than an approximation.
        Approximating it left the centred names up to 7px apart, and the status
        line is hidden when a session has nothing to put there, so tiles
        genuinely differ.
        """
        rows = [l for l in (self.project_label, self.activity_label,
                            self.sub_label) if not l.isHidden()]
        if not rows:
            return 0
        return (sum(l.sizeHint().height() for l in rows)
                + TILE_ROW_SPACING * (len(rows) - 1))

    def _row_span(self):
        """(top, bottom) of this tile's visible content, in tile coords.

        Spans the mascot and the subagent row too (when shown), not just the
        text — so the vertical centring measures the WHOLE block. Real geometry
        rather than sizeHints, which run about 15px short (rows render smaller
        than they advertise), enough to leave a "centred" block visibly off.
        """
        items = [self.project_label, self.activity_label, self.sub_label,
                 self.agents_label, self.sprite, self._agents_box]
        tops, bottoms = [], []
        for w in items:
            if w.isHidden() or w.height() <= 0:
                continue
            tops.append(w.mapTo(self, w.rect().topLeft()).y())
            bottoms.append(w.mapTo(self, w.rect().bottomLeft()).y())
        if not tops:
            return None
        return min(tops), max(bottoms)

    def _text_rows_height(self) -> int:
        """Content height plus the column's own vertical padding — what the
        shelf must reserve for a tile that shows text only."""
        return self._content_h() + 16

    def set_text_centered(self, on: bool, top_pad: int = 0) -> None:
        """Push the content block down by ``top_pad`` to centre it vertically.

        ``top_pad`` comes from the shelf and is the SAME for every tile, so all
        the mascots (and first text rows) land on one line. Letting each tile
        centre its own content with a stretch staggered them, because tiles
        carry different numbers of rows. Applied in BOTH modes now — with a
        mascot (to centre the block on a tall window) and text-only (to centre
        the rows in the space the mascot vacated).

        ``on`` is text-only mode: it drops the top margin from the generous
        mascot-glow padding to a plain 4px, since with no mascot that padding
        would only push the text low.
        """
        lay = self.layout()
        lay.setContentsMargins(12, 4 if on else _GLOW_PAD, 12, 4)
        self._top_pad.changeSize(0, top_pad,
                                 QSizePolicy.Minimum, QSizePolicy.Fixed)
        lay.invalidate()
        lay.activate()

    def _agents_line_height(self) -> int:
        """What the "N subagents" fallback row costs, including its spacing.

        Measured from the real label rather than assumed: it is only shown when
        the subagent mascots don't fit, and the shelf has to reserve it or the
        line lands below the viewport and is clipped away.
        """
        h = self.agents_label.sizeHint().height()
        return h + AGENTS_ROW_SPACING if h > 0 else 0

    def set_presentation(self, show_sprite: bool, agents_as_mascots: bool) -> None:
        """Apply the shelf's shelf-wide decision. The tile no longer decides for
        itself — each tile has a different amount of text and a different agent
        count, so per-tile decisions produced a row that disagreed with itself."""
        self._sprite_shown = show_sprite
        self.sprite.setVisible(show_sprite)
        n = len(self._agents)
        self._agents_box.setVisible(bool(n) and agents_as_mascots)
        self.agents_label.setVisible(bool(n) and not agents_as_mascots)
        if n and not agents_as_mascots:
            self.agents_label.setText(f"{n} subagent{'s' if n != 1 else ''}")

    def set_sprite_box(self, px: int) -> None:
        """Fix the mascot's box to ``px`` square, uniform across the shelf.

        Sets the FIXED HEIGHT and the render/hint width together, so the tile is
        ``px`` wide as well as tall. That width is what makes a row of tiles
        overflow the viewport and scroll once there are too many to fit, rather
        than each mascot being squeezed thinner as sessions are added.

        The shelf computes ONE ``px`` for every tile from the window height, so
        the mascots are identical and every tile's text starts at the same y —
        including tiles carrying a subagent row, which used to steal height from
        their own mascot and leave it smaller than its neighbours'.
        """
        if px <= 0:
            self.sprite.setFixedHeight(0)
            self.sprite.setMinimumWidth(0)
            return
        self.sprite.setFixedHeight(px)
        # A real minimum WIDTH is what forces the tile to stay ``px`` wide, so a
        # row of them overflows the viewport and scrolls once too many are live
        # — without it the tile packs down to its text width (~112px) and the
        # square mascot is rendered thin. The tile's own minimum stays 0 (see
        # __init__), so the enter/leave width animation can still collapse it to
        # nothing; this only sets a floor for the settled, non-animating state.
        self.sprite.setMinimumWidth(px)
        self.set_sprite_size(px)

    def set_sprite_size(self, px: int) -> None:
        # set_size (not setFixedSize) so the mascot pixmap re-scales too; it
        # early-returns when the size is unchanged, so calling it every poll is
        # cheap and won't churn the layout.
        self.sprite.set_size(px)
        # Keep the label cap in step with the mascot width; the label re-elides
        # / re-evaluates its scroll on the resulting resize.
        new_w = max(px, _LABEL_MIN_W)
        if new_w != self._label_max_w:
            self._label_max_w = new_w
            self.project_label.setMaximumWidth(new_w)
            self.sub_label.setMaximumWidth(new_w)

    def update_state(self, state: TranscriptState) -> None:
        """Reflect one session's state: name, activity color/label, glow, dot,
        and the mascot animation (idle sessions get the calm sleep loop)."""
        self.project_label.setText(state.project_name or "unknown")

        # Per-session token breakdown on mascot hover (when enabled and there's
        # something to show).
        tokens = getattr(state, "tokens", None)
        if self._show_tokens and tokens is not None and tokens.total > 0:
            self.sprite.setToolTip(_token_tooltip(tokens))
        else:
            self.sprite.setToolTip("")

        idle = state.is_stale or state.activity == Activity.IDLE
        color = ACTIVITY_COLORS.get(state.activity, _IDLE_COLOR)

        if idle:
            # Dim glow + calm animation. A fresh, distinct set_anims key is
            # needed so a tile transitioning live->idle actually restarts on
            # the idle loop (set_anims no-ops on an unchanged key).
            self._glow.setColor(QColor(_IDLE_COLOR))
            self._glow.setBlurRadius(20)
            self.activity_label.setText(ACTIVITY_LABELS[Activity.IDLE])
            self.activity_label.setStyleSheet(f"color: {_IDLE_COLOR};")
            self.status_dot.setStyleSheet(f"color: {_IDLE_COLOR};")
            self.sub_label.setText(_ago_text(state.last_event_ts),
                                   tooltip=_abs_time_text(state.last_event_ts))
            self.sprite.set_anims(f"{self._session_id}:idle", _IDLE_ANIMS)
        else:
            self._glow.setColor(QColor(color))
            self._glow.setBlurRadius(38)
            self.activity_label.setText(ACTIVITY_LABELS.get(state.activity, ""))
            self.activity_label.setStyleSheet(f"color: {color};")
            # Live dot tracks the activity color so the tile reads as a unit.
            self.status_dot.setStyleSheet(f"color: {color};")
            # Show what's being acted on (file/pattern/…); fall back to the tool.
            self.sub_label.setText(state.target or state.tool_name or "")
            anims = ACTIVITY_ANIMS.get(state.activity) or _IDLE_ANIMS
            self.sprite.set_anims(f"{self._session_id}:{state.activity.value}", anims)

        # An empty sub-line keeps its row height and leaves a blank gap above
        # whatever follows it (the "N subagents" line), so hide it outright.
        self.sub_label.setVisible(bool(self.sub_label.text()))

    def stop(self) -> None:
        self.sprite.stop()
        for m in self._agents.values():
            m.stop()

    # --- enter/leave transitions --------------------------------------------
    # A tile expands in (width 0 -> natural) and collapses out (natural -> 0),
    # so the row reflows smoothly as sessions come and go. The width rides the
    # widget's own maximumWidth property (the SetNoConstraint above lets it
    # shrink past the mascot, clipping the centred content). The glow stays on
    # throughout — we deliberately do NOT fade via a QGraphicsOpacityEffect on
    # the tile, because that would nest over the sprite's (and agents') own glow
    # effects, which Qt can't render (a graphics effect inside a graphics
    # effect). The shelf owns the animation objects so they aren't GC'd mid-flight.
    ENTER_MS = 240
    LEAVE_MS = 200
    _SIZE_MAX = 16777215  # Qt's QWIDGETSIZE_MAX — the "no maximum" sentinel.

    def build_enter_anim(self) -> QParallelAnimationGroup:
        target = max(1, self.sizeHint().width())
        self.setMaximumWidth(0)
        grp = QParallelAnimationGroup(self)
        grow = QPropertyAnimation(self, b"maximumWidth", grp)
        grow.setDuration(self.ENTER_MS)
        grow.setStartValue(0)
        grow.setEndValue(target)
        grow.setEasingCurve(QEasingCurve.OutCubic)
        grp.addAnimation(grow)
        grp.finished.connect(self._clear_transition)
        return grp

    def build_leave_anim(self) -> QParallelAnimationGroup:
        grp = QParallelAnimationGroup(self)
        shrink = QPropertyAnimation(self, b"maximumWidth", grp)
        shrink.setDuration(self.LEAVE_MS)
        shrink.setStartValue(max(1, self.width()))
        shrink.setEndValue(0)
        shrink.setEasingCurve(QEasingCurve.InCubic)
        grp.addAnimation(shrink)
        return grp

    def _clear_transition(self) -> None:
        """Release the width cap so the tile sizes naturally again after enter."""
        self.setMaximumWidth(self._SIZE_MAX)


class SessionShelf(QWidget):
    """Header + horizontal scroll row of SessionTiles, diffed by session id."""

    # Smallest mascot worth drawing. Below this the whole shelf goes text-only
    # rather than rendering something unreadable.
    #
    # This threshold is also what governs dead space: any room between 0 and
    # MIN_MASCOT is room the shelf holds and then cannot use, so it shows as an
    # empty band under the session text. Measured on macOS across a 360-720px
    # sweep, worst-case blank against the smallest mascot ever rendered:
    #
    #     72 -> 81px blank, no small mascot ever drawn
    #     64 -> 81px blank, smallest 72px
    #     56 -> 71px blank, smallest 60px
    #     48 ->   0px blank, smallest 52px      <- here
    #     40 ->   0px blank, smallest 52px      (no further gain)
    #
    # 48 removes the blank entirely while never actually drawing a mascot below
    # 52px, which is well clear of the unreadably-small ones this guard exists
    # to prevent. Note the reserved floor's 1.5x headroom is NOT the cause and
    # is deliberately left alone — dropping it moved the worst case 81px -> 81px.
    MIN_MASCOT = 64

    # Mascot size the parent must still reach for subagent mascots to be worth
    # drawing alongside it. Below this the tile shows "N subagents" instead.
    AGENTS_NEED_MASCOT = 144

    # Mascot box ceiling. The size follows the WINDOW HEIGHT up to this cap and
    # does NOT depend on the session count: a 4th or 8th session no longer
    # shrinks the others — the row scrolls horizontally instead. A taller window
    # grows the mascots toward this cap; beyond it the row is centred vertically
    # (see _layout_tiles) so the extra height doesn't open a dead band below the
    # mascots rather than making them ever larger.
    MASCOT_MAX = 250

    # Uniform sprite size by session count — one big mascot looks great solo,
    # but a row of six must stay compact enough to fit the scroll viewport.
    @staticmethod
    def _sprite_size_for(count: int) -> int:
        if count <= 1:
            return 200
        if count == 2:
            return 160
        if count == 3:
            return 130
        return 110

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("shelfRoot")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(SHELF_STYLESHEET)

        self._tiles: dict[str, SessionTile] = {}
        # Whether tiles expose the per-session token breakdown on mascot hover
        # (mirrors the Settings "Show token usage" toggle; applied to new tiles).
        self._show_tokens = True
        # Tiles mid-leave (animating out, still in the layout) keyed by id, and
        # the live animation groups keyed by tile so they aren't GC'd and so we
        # can tell when the shelf is "settled" (safe to reorder).
        self._leaving: dict[str, SessionTile] = {}
        self._anims: dict[SessionTile, object] = {}
        # The uniform sprite size currently applied to live tiles. When the
        # session count changes this target changes and survivors animate to it
        # rather than popping (which, with centre alignment, jolts the whole row).
        self._sprite_size: int | None = None
        # Animates the reserved shelf height when the tile size changes, so the
        # quota bars below glide to their new position instead of jumping.
        self._height_anim: QPropertyAnimation | None = None
        # Per-tile height beyond the mascot (labels + margins), measured once.
        # Measured height an agents row adds to a tile (sprite + padding + the
        # column spacing). Cached the first time a tile actually has agents so
        # every tile is measured the same way, so rows line up across the shelf.
        self._agent_oh: int | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)  # tight gap between the header and the mascots

        self.header = QLabel("ACTIVE SESSIONS — 0", objectName="shelfHeader")
        self.header.setAlignment(Qt.AlignHCenter)
        outer.addWidget(self.header)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("shelfScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        outer.addWidget(self._scroll, 1)

        self._row_widget = QWidget(objectName="shelfRow")
        self._row = QHBoxLayout(self._row_widget)
        self._row.setContentsMargins(4, 4, 4, 4)
        # Spacing is 0 — each tile carries its own side margins as the gap, so a
        # collapsing tile leaves no residual space behind when it animates out.
        self._row.setSpacing(0)
        self._row.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self._scroll.setWidget(self._row_widget)

    def set_sessions(self, states) -> None:
        """Add/remove/update tiles by session id without rebuilding the row.

        Rebuilding every poll would thrash the sprite QTimers and flicker, so we
        keep tiles keyed by id, animate out the ones whose session vanished,
        animate in ones that appeared, and update the survivors in place.
        """
        # Dedup by session id (the dict keeps the watcher's newest-first order).
        # Sizing, tile creation and the header all key off this one map so they
        # can't disagree if a duplicate id ever slips through.
        incoming = {s.session_id: s for s in states if s.session_id}
        size = self._sprite_size_for(len(incoming))
        old_size = self._sprite_size
        self._sprite_size = size

        # Animate out tiles whose session left the shelf (they stay in the layout
        # and collapse, so the neighbours slide in to fill the gap).
        for sid in list(self._tiles):
            if sid not in incoming:
                self._start_leave(sid, self._tiles.pop(sid))

        # Create/update tiles in newest-first order.
        for index, (sid, state) in enumerate(incoming.items()):
            tile = self._tiles.get(sid)
            if tile is None:
                tile = self._resurrect(sid)  # a session that reappeared mid-leave
                if tile is None:
                    tile = SessionTile(sid, sprite_size=size, parent=self._row_widget)
                    tile._show_tokens = self._show_tokens
                    self._tiles[sid] = tile
                    self._insert_live(tile, index)
                    tile.set_sprite_size(size)
                    tile.update_state(state)
                    tile.update_agents(state.agents)
                    self._start_enter(tile)
                    continue
            # Survivor: the mascot box is owned by _layout_tiles now (driven by
            # the window height, not the session count), so a count change no
            # longer resizes it here — the row simply reflows and scrolls. Just
            # refresh the tile's state.
            tile.update_state(state)
            tile.update_agents(state.agents)

        # Re-assert newest-first order. _reorder_live short-circuits when the
        # live order is unchanged (the common case) so there's no per-poll
        # churn, operates only on live tiles, and leaves entering tiles' width
        # animations running as they're repositioned.
        self._reorder_live(incoming)

        self._layout_tiles()
        self._sync_height(old_size, size)
        self.header.setText(f"ACTIVE SESSIONS — {len(self._tiles)}")

    def set_header_visible(self, on: bool) -> None:
        """Show/hide the 'ACTIVE SESSIONS — N' header (hidden in single-mascot
        mode, where the count is meaningless)."""
        self.header.setVisible(on)

    def set_show_tokens(self, on: bool) -> None:
        """Enable/disable the per-session token breakdown on mascot hover. The
        actual tooltip text is (re)applied by the next update_state."""
        self._show_tokens = bool(on)
        for tile in (*self._tiles.values(), *self._leaving.values()):
            tile._show_tokens = self._show_tokens

    def _start_enter(self, tile: SessionTile) -> None:
        anim = tile.build_enter_anim()
        self._anims[tile] = anim
        anim.finished.connect(lambda t=tile: self._anims.pop(t, None))
        anim.start()

    def _start_leave(self, sid: str, tile: SessionTile) -> None:
        self._cancel_anim(tile)  # if it was still entering, drop that animation
        self._leaving[sid] = tile
        anim = tile.build_leave_anim()
        self._anims[tile] = anim
        anim.finished.connect(lambda s=sid, t=tile: self._finish_leave(s, t))
        anim.start()

    def _finish_leave(self, sid: str, tile: SessionTile) -> None:
        self._anims.pop(tile, None)
        if self._leaving.get(sid) is tile:
            del self._leaving[sid]
        self._row.removeWidget(tile)
        tile.stop()
        tile.deleteLater()

    def _resurrect(self, sid: str) -> SessionTile | None:
        """A session that reappeared before its leave finished: cancel the leave,
        restore the tile and re-enter it instead of spawning a duplicate."""
        tile = self._leaving.pop(sid, None)
        if tile is None:
            return None
        self._cancel_anim(tile)
        self._tiles[sid] = tile
        self._start_enter(tile)
        return tile

    def _cancel_anim(self, tile: SessionTile) -> None:
        anim = self._anims.pop(tile, None)
        if anim is not None:
            anim.stop()

    def _insert_live(self, tile: SessionTile, logical_index: int) -> None:
        """Insert `tile` so it becomes the logical_index-th live tile, skipping
        any tiles currently animating out when counting positions."""
        leaving = set(self._leaving.values())
        seen = 0
        for i in range(self._row.count()):
            w = self._row.itemAt(i).widget()
            if w is None or w in leaving:
                continue
            if seen == logical_index:
                self._row.insertWidget(i, tile, 1)
                return
            seen += 1
        self._row.addWidget(tile, 1)

    def _reorder_live(self, incoming) -> None:
        desired = [self._tiles[sid] for sid in incoming if sid in self._tiles]
        current = [
            w for i in range(self._row.count())
            if (w := self._row.itemAt(i).widget()) in self._tiles.values()
        ]
        if current == desired:
            return
        for w in current:
            self._row.removeWidget(w)
        for i, tile in enumerate(desired):
            self._row.insertWidget(i, tile, 1)

    def _sync_height(self, old_size: int | None, new_size: int) -> None:
        """Keep the reserved shelf height in step with the tile size so the quota
        bars below don't jump. Snap on the first fill (or when the size is
        unchanged); animate — same curve/duration as the mascot resize — when it
        changes. The height is derived analytically from the target sprite size,
        not from the live layout, so a tile mid enter-animation (width clamped to
        0) can't report a stale, clipped height."""
        # The shelf no longer RESERVES height for the mascots: they scale into
        # whatever the layout hands them (SpritePlayer scale_to_fit). Reserving
        # a mascot-derived height is what made the window grow and shrink as
        # sessions came and went, and left dead space when it could not.
        target = self._reserved_height_for(0)
        start = self._scroll.minimumHeight()
        if old_size is None:
            # First fill — snap, no animation.
            if start != target:
                self._scroll.setMinimumHeight(target)
            return
        # Already animating toward this exact target? Let it finish — restarting
        # every poll (the steady state) would stutter and never settle.
        if self._height_anim is not None and self._height_anim.endValue() == target:
            return
        if self._height_anim is not None:
            self._height_anim.stop()
        if start == target:
            self._height_anim = None
            return
        anim = QPropertyAnimation(self._scroll, b"minimumHeight", self)
        anim.setDuration(SessionTile.ENTER_MS)
        anim.setStartValue(start)
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.finished.connect(self._on_height_anim_done)
        self._height_anim = anim
        anim.start()

    def _on_height_anim_done(self) -> None:
        self._height_anim = None

    def _any_agents(self) -> bool:
        return any(t.has_agents() for t in self._tiles.values())

    def _agent_extra(self) -> int:
        """Measured height an agents row adds to a tile. Taken from a real tile
        that has agents, so every tile budgets the same row and they cannot
        drift apart. Cached once."""
        if self._agent_oh is None:
            tile = next((t for t in self._tiles.values() if t.has_agents()), None)
            if tile is None:
                return AGENT_ROW_FALLBACK  # nothing to measure yet
            self._agent_oh = tile.agents_box_height() + AGENTS_ROW_SPACING
        return self._agent_oh


    def _reserved_height_for(self, sprite_size: int = 0) -> int:
        # Text-only floor: the name / activity / status rows. The mascot is
        # elastic above this; these rows are not.
        #
        # Measured from a real tile's text rows. Deriving it instead from the
        # tile's sizeHint minus the sprite size stopped holding once the sprite
        # became scale-to-fit — that subtraction returned 0, so this floor
        # silently evaluated to 33px instead of the intended ~130 and the last
        # line clipped. Instrumentation caught it; arithmetic had not.
        #
        # `sprite_size` is vestigial: the shelf no longer reserves any
        # mascot-derived height, so every caller passes 0. It is kept only so
        # the one existing test that names it still reads naturally.
        #
        # TILE_V_MARGINS is the column's own padding (_GLOW_PAD top + 4
        # bottom); 14 is horizontal-scrollbar allowance. The 1.5x is
        # deliberate headroom (Nick's call 2026-07-26).
        tile = next(iter(self._tiles.values()), None)
        rows = tile._text_rows_height() if tile is not None else 60
        return int((rows + _TILE_V_MARGINS + 14) * 1.5)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._layout_tiles()

    def relayout_tiles(self) -> None:
        """Re-size the mascots to the viewport as it is now. For a caller that
        took height from the shelf without resizing the window: the shelf's
        resize event can arrive before its viewport has shrunk, leaving the
        mascot sized for the old height and clipped."""
        self._layout_tiles()

    def _layout_tiles(self) -> None:
        """Size every mascot identically, from ONE calculation for the shelf.

        Per-tile sizing could never satisfy "all the same size" or "the names
        line up": each tile carries a different amount of text (an idle session
        has a "last active" line, a live one may not) and a different number of
        subagents, so each was left a different amount of room for its mascot.
        Computing the box once from the WORST case and handing it to every tile
        fixes both at their source.

        One pure function of (container, count), applied in a single place —
        rather than per-widget arithmetic scattered through resize handlers.
        """
        tiles = list(self._tiles.values())
        if not tiles:
            return
        # Measure the VIEWPORT, not the tiles. A tile's height depends on the
        # sprite box we are about to set, so feeding it back in makes this
        # function's output its own input — it converged on one value, then
        # collapsed to 0 on a second pass. The viewport is fixed by the window
        # and is genuinely independent.
        vp = self._scroll.viewport()
        # Reserve the horizontal scrollbar's height WHETHER OR NOT it is showing,
        # so the mascot size never depends on the scrollbar's presence. Without
        # this the size feeds back on itself: a size big enough to overflow
        # summons the scrollbar, the scrollbar steals ~8px of viewport height,
        # the smaller height yields a smaller size, which no longer overflows…
        # and it never settles (measured: 196px jittering to 188px on every
        # relayout). Always subtracting the bar's height breaks that loop.
        hbar = self._scroll.horizontalScrollBar()
        hbar_h = hbar.sizeHint().height()
        h = vp.height() - (0 if hbar.isVisible() else hbar_h)
        if h <= 0 or vp.width() <= 0:
            return                      # not laid out yet; nothing to size
        text_h = max(t._text_rows_height() for t in tiles)
        # Subagent mascots are drawn only if they fit WITHOUT eating into the
        # mascot box — otherwise every tile falls back to the "N subagents"
        # line, so one session sprouting agents cannot shrink its own mascot
        # below its neighbours'.
        agents_h = self._agent_extra() if self._any_agents() else 0
        # _ROW_V_MARGINS is the row layout's own 4px top + 4px bottom: the tile
        # never gets the whole viewport, and handing the sprite that difference
        # is what pushed the last row out of view.
        room = h - text_h - _TILE_V_MARGINS - _ROW_V_MARGINS
        # Subagent mascots have to EARN their space: they are only drawn if the
        # parent mascot is still comfortably sized afterwards, not merely above
        # the bare legibility floor. Otherwise a tile with agents spent its
        # height on three tiny children and a shrunken parent.
        agents_as_mascots = (bool(agents_h)
                             and (room - agents_h) >= self.AGENTS_NEED_MASCOT)
        if agents_as_mascots:
            room -= agents_h
        elif agents_h:
            # Falling back to the "N subagents" line — budget for it. Without
            # this the sprite took the space the line needed, the tile ended up
            # ~14px taller than the viewport, and since the shelf's vertical
            # scrollbar is always off the line was silently cut. The mascots
            # were dropped and their replacement never appeared, so a session
            # with subagents showed no sign of them at all.
            room -= max(t._agents_line_height() for t in tiles)
        # Mascot size follows the WINDOW HEIGHT, not the session count: every
        # tile gets the same box however many there are, and the row scrolls
        # horizontally (the scroll area's own bar) once they no longer fit side
        # by side — instead of every mascot being squeezed thinner by each new
        # session, which past ~4 sessions left them unreadable and past ~7 hid
        # them entirely. A lone session keeps a larger "hero" cap; a row of them
        # holds at a readable medium.
        edge = min(room, self.MASCOT_MAX)
        edge = max(0, edge - (edge % 4))     # quantised: exactly equal, crisper
        if edge < self.MIN_MASCOT:
            edge = 0                          # text-only
        # Text-only: centre the rows in the space the mascot vacated rather than
        # leaving all of it underneath, which read as a dead band between the
        # session text and the usage bars. Every tile is told the same thing and
        # every tile has the same height, so the rows still line up across the
        # row — the property that made the shelf worth reworking in the first
        # place.
        for t in tiles:
            t.set_sprite_box(edge)
            t.set_presentation(bool(edge), agents_as_mascots)
            t.set_text_centered(edge == 0, 0)
        # Centre the content block (mascot + text, or text-only) vertically in
        # the viewport, so a tall / maximised window doesn't leave a dead band
        # below the mascots — the block sits in the middle with balanced
        # whitespace instead of pinned to the top.
        #
        # Measured, not modelled. Deriving the block from sizeHints was wrong by
        # a constant ~15px — rows render shorter than they advertise — which
        # left "centred" content visibly low. So lay out once with no pad, read
        # where the block actually is, and pad from that. ONE pad for every tile
        # (from the tallest block) keeps the mascots on a single line across the
        # row rather than staggering with each tile's text. Settles in one extra
        # pass, since the pad shifts the block without changing its height.
        self._row_widget.layout().activate()
        spans = [s for s in (t._row_span() for t in tiles) if s is not None]
        if not spans:
            return
        first_top = min(s[0] for s in spans)
        block = max(s[1] for s in spans) - first_top
        # Available height comes from the viewport, not from tile.height():
        # mid-relayout a tile can still report a stale height, and using it
        # produced a 145px pad that shoved the subagents line clean out of view.
        # Use the REAL viewport height here (not the scrollbar-reserved `h` that
        # sizes the mascot): text-only tiles don't summon the mascot feedback
        # loop, and reserving 8px that isn't there would centre the rows a few
        # px high.
        avail = vp.height() - _ROW_V_MARGINS
        # ONE pad for every tile, so the first rows stay on a single line no
        # matter how many rows each carries. Capped at the slack that actually
        # exists, so a bad measurement can never push content out of the tile.
        pad = max(0, min((avail - block) // 2 - first_top, avail - block))
        if pad:
            for t in tiles:
                t.set_text_centered(edge == 0, pad)

    def reserved_current(self) -> int:
        """Currently reserved scroll height (may be mid height-animation)."""
        return self._scroll.minimumHeight()

    def reserved_target(self) -> int:
        """Settled reserved scroll height — lets the host window aim at the
        final size while this is still animating.

        Must agree with what ``_sync_height`` actually sets, which is the
        text-only floor: the shelf stopped reserving mascot-derived height when
        sprites became scale-to-fit. While this still returned the old
        mascot-derived figure, ``Dashboard._target_window_height`` added a
        difference that could never settle to zero, so the window grew by it on
        every fit. Measured on Windows: 799px tall against develop's 430, with
        reserved_target=497 versus reserved_current=127, permanently.
        """
        if self._sprite_size is None:
            return self._scroll.minimumHeight()
        return self._reserved_height_for(0)

    def stop_all(self) -> None:
        if self._height_anim is not None:
            self._height_anim.stop()
            self._height_anim = None
        for anim in list(self._anims.values()):
            anim.stop()
        self._anims.clear()
        for tile in list(self._tiles.values()) + list(self._leaving.values()):
            tile.stop()


# ---------------------------------------------------------------------------
# Compact mode — a denser list view (one horizontal row per session) for when
# you're running many sessions. Distinct from the shelf (vertical mascots) and
# from mini (the tiny readout).
# ---------------------------------------------------------------------------

COMPACT_MASCOT = 38

def _build_compact_qss() -> str:
    return f"""
QWidget#compactRoot {{ background: {_BG}; }}
QWidget#compactTitleBar {{ background: {_P.bg_deepest}; }}
QLabel#compactTitle {{ font-size: 12px; font-weight: 700; color: {_TEXT};
                       letter-spacing: 1.5px; }}
QToolButton#compactBtn {{ background: transparent; color: {_P.accent}; border: none;
                          font-size: 13px; padding: 2px 7px; }}
QToolButton#compactBtn:hover {{ background: {_P.surface}; }}
QWidget#compactRow:hover {{ background: {_P.surface_dim}; }}
QLabel#compactBarLabel {{ font-size: 10px; font-weight: 600; color: {_MUTED};
                          letter-spacing: 1px; }}
QLabel#compactPctLbl {{ font-size: 11px; font-weight: 700; color: {_TEXT}; }}
QLabel#compactReset {{ font-size: 10px; color: {_MUTED}; }}
QLabel#compactRowTokens {{ font-size: 11px; font-weight: 700; color: {_MUTED}; }}
QLabel#compactRowDot {{ font-size: 11px; }}
QLabel#compactRowActivity {{ font-size: 10px; font-weight: 600; letter-spacing: 1px; }}
QLabel#compactRowAgents {{ font-size: 10px; font-weight: 700; color: {_MUTED}; }}
QScrollArea#compactScroll {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px 0; }}
QScrollBar::handle:vertical {{ background: {_P.border}; border-radius: 4px; min-height: 24px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


COMPACT_STYLESHEET = _build_compact_qss()


def refresh_theme() -> None:
    """Recompute cached chrome colours + rebuild the shelf/compact stylesheets
    from the now-active palette (for a live theme switch). The app's
    apply_theme() re-applies SHELF_STYLESHEET/COMPACT_STYLESHEET to the live
    widgets and repaints them afterward. This includes the palette-derived
    _IDLE_COLOR, _BAR_HEAT ramp and _BAR_OVERAGE (they track the theme); only
    the semantic ACTIVITY_COLORS glow map stays fixed across themes."""
    global _P, _BG, _TEXT, _MUTED, _IDLE_COLOR, _BAR_TRACK, _BAR_BORDER
    global _BAR_HEAT, _BAR_OVERAGE, SHELF_STYLESHEET, COMPACT_STYLESHEET
    _P = theme.active()
    _BG = _P.bg
    _TEXT = _P.text
    _MUTED = _P.text_dim
    _IDLE_COLOR = _P.idle
    _BAR_TRACK = _P.surface
    _BAR_BORDER = _P.border
    _BAR_HEAT = _heat_ramp(_P)
    _BAR_OVERAGE = _overage_color(_P)
    SHELF_STYLESHEET = _build_shelf_qss()
    COMPACT_STYLESHEET = _build_compact_qss()


class CompactRow(QWidget):
    """One session as a horizontal row: small mascot | title + tokens, then a
    second line of ``● activity · target`` (or 'last active …' when idle)."""

    _DOT = "●"

    def __init__(self, session_id: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("compactRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._sid = session_id
        self._show_tokens = True

        h = QHBoxLayout(self)
        h.setContentsMargins(10, 5, 10, 5)
        h.setSpacing(9)

        self.sprite = SpritePlayer(size=COMPACT_MASCOT)
        self._glow = QGraphicsDropShadowEffect(self)
        self._glow.setBlurRadius(14)
        self._glow.setOffset(0, 0)
        self._glow.setColor(QColor(_IDLE_COLOR))
        self.sprite.setGraphicsEffect(self._glow)
        h.addWidget(self.sprite, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setSpacing(1)
        col.setContentsMargins(0, 0, 0, 0)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = ScrollingLabel(px=12, bold=True, role="text", max_w=230,
                                    align=Qt.AlignLeft)
        self.tokens = QLabel("", objectName="compactRowTokens")
        self.tokens.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.title, 1)
        top.addWidget(self.tokens, 0)
        col.addLayout(top)

        bot = QHBoxLayout()
        bot.setSpacing(5)
        self.dot = QLabel(self._DOT, objectName="compactRowDot")
        self.activity = QLabel("", objectName="compactRowActivity")
        self.sep = QLabel("·", objectName="compactRowActivity")
        self.target = ScrollingLabel(px=10, bold=False, role="muted",
                                     letter_spacing=0, max_w=170, align=Qt.AlignLeft)
        self.agents = QLabel("", objectName="compactRowAgents")
        bot.addWidget(self.dot, 0)
        bot.addWidget(self.activity, 0)
        bot.addWidget(self.sep, 0)
        bot.addWidget(self.target, 1)
        bot.addWidget(self.agents, 0)
        col.addLayout(bot)

        h.addLayout(col, 1)

    def set_show_tokens(self, on: bool) -> None:
        self._show_tokens = bool(on)

    def update_state(self, state: TranscriptState) -> None:
        self.title.setText(state.project_name or "unknown")
        idle = state.is_stale or state.activity == Activity.IDLE
        color = ACTIVITY_COLORS.get(state.activity, _IDLE_COLOR)
        if idle:
            self._glow.setColor(QColor(_IDLE_COLOR))
            self._glow.setBlurRadius(6)
            self.dot.setStyleSheet(f"color: {_IDLE_COLOR};")
            self.activity.setText(ACTIVITY_LABELS[Activity.IDLE])
            self.activity.setStyleSheet(f"color: {_IDLE_COLOR};")
            self.sep.hide()
            self.target.setText(_ago_text(state.last_event_ts),
                                tooltip=_abs_time_text(state.last_event_ts))
            self.sprite.set_anims(f"crow:{self._sid}:idle", _IDLE_ANIMS)
        else:
            self._glow.setColor(QColor(color))
            self._glow.setBlurRadius(14)
            self.dot.setStyleSheet(f"color: {color};")
            self.activity.setText(ACTIVITY_LABELS.get(state.activity, ""))
            self.activity.setStyleSheet(f"color: {color};")
            tgt = state.target or state.tool_name or ""
            self.sep.setVisible(bool(tgt))
            self.target.setText(tgt)
            anims = ACTIVITY_ANIMS.get(state.activity) or _IDLE_ANIMS
            self.sprite.set_anims(f"crow:{self._sid}:{state.activity.value}", anims)

        tk = getattr(state, "tokens", None)
        if self._show_tokens and tk is not None and tk.work > 0:
            self.tokens.setText(fmt_tokens(tk.work))
            self.sprite.setToolTip(_token_tooltip(tk))
        else:
            self.tokens.setText("")
            self.sprite.setToolTip("")

    def update_agents(self, agents) -> None:
        n = len(agents)
        self.agents.setText(f"+{n}" if n else "")
        self.agents.setToolTip(
            f"{n} subagent{'s' if n != 1 else ''}" if n else "")

    def stop(self) -> None:
        self.sprite.stop()


class CompactView(QWidget):
    """Compact mode window: slim usage bars over a scrollable list of session
    rows. Frameless, draggable by its title bar, always-on-top, no taskbar."""

    set_mode_requested = Signal(str)  # a view segment -> switch to that mode
    grow_requested = Signal()    # double-click title / menu -> full view
    hide_requested = Signal()    # close button -> hide to tray
    quit_requested = Signal()

    WIDTH = 400
    MAX_ROWS = 6
    ROW_H = 50

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("compactRoot")
        self.setWindowTitle("Clawdmeter")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(COMPACT_STYLESHEET)
        if sys.platform == "darwin":
            # macOS: a rounded HUD panel like the mini (NOT a native window with
            # traffic lights) -- keeps our own title bar (mascot/title/view
            # buttons/✕). round_window() rounds + shadows it on show; drop Qt.Tool
            # (which auto-hides on focus loss). Round the border to match.
            self.setStyleSheet(
                COMPACT_STYLESHEET + "\nQWidget#compactRoot{border-radius:13px}")
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint
                                | Qt.WindowStaysOnTopHint)
        else:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.Tool
                                | Qt.WindowStaysOnTopHint)
        icon_path = assets_root() / "icon.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.setFixedWidth(self.WIDTH)
        self._press_pos: QPoint | None = None
        self._rows: dict[str, CompactRow] = {}
        self._show_tokens = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # title bar
        self._title_bar = QWidget(objectName="compactTitleBar")
        trow = QHBoxLayout(self._title_bar)
        trow.setContentsMargins(10, 6, 6, 6)
        trow.setSpacing(6)
        icon_lbl = QLabel()
        if icon_path.exists():
            icon_lbl.setPixmap(QIcon(str(icon_path)).pixmap(18, 18))
        trow.addWidget(icon_lbl)
        trow.addWidget(QLabel("CLAWDMETER", objectName="compactTitle"))
        trow.addStretch(1)
        # Caret toggle (points UP in compact -> click expands to full) + the
        # mini button, matching the full window's switcher.
        self.caret_btn = self._tbtn(chr(0xF102), "Full view")  # angles-up
        self.caret_btn.clicked.connect(lambda: self.set_mode_requested.emit("full"))
        trow.addWidget(self.caret_btn)
        self.mini_btn = self._tbtn(chr(0xF422), "Mini view")  # compress-to-center
        self.mini_btn.clicked.connect(lambda: self.set_mode_requested.emit("mini"))
        trow.addWidget(self.mini_btn)
        self.close_btn = self._tbtn(chr(0xF00D), "Hide to tray")  # ✕
        self.close_btn.clicked.connect(self.hide_requested.emit)
        trow.addSpacing(4)
        trow.addWidget(self.close_btn)
        outer.addWidget(self._title_bar)

        # slim bars
        bars = QWidget()
        bcol = QVBoxLayout(bars)
        bcol.setContentsMargins(12, 6, 12, 8)
        bcol.setSpacing(3)
        self.s_label, self.s_pct, self.s_bar, self.s_reset = self._slim_bar(bcol)
        bcol.addSpacing(4)
        self.w_label, self.w_pct, self.w_bar, self.w_reset = self._slim_bar(bcol)
        # Scoped windows the user ticked (e.g. WEEKLY · FABLE), same slim rows
        # and the same 4px gap as between SESSION and WEEKLY.
        self.scoped_rows = ScopedRows(self._make_scoped_row, self._render_scoped_row,
                                      spacing=3 + 4, top_margin=4)
        bcol.addWidget(self.scoped_rows)
        outer.addWidget(bars)

        # scrollable session list
        self._scroll = QScrollArea(objectName="compactScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._list_widget = QWidget(objectName="compactList")
        self._list_widget.setStyleSheet("QWidget#compactList { background: transparent; }")
        self._list = QVBoxLayout(self._list_widget)
        self._list.setContentsMargins(0, 0, 0, 4)
        self._list.setSpacing(0)
        self._list.addStretch(1)
        self._scroll.setWidget(self._list_widget)
        outer.addWidget(self._scroll, 1)

        # ThemedPopup, not QMenu — see uiutil.ThemedPopup.
        menu = make_popup(self)
        menu.set_items([("Full view", self.grow_requested.emit),
                        ("Quit", self.quit_requested.emit)])
        self._menu = menu
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda p: self._menu.popup_at(self.mapToGlobal(p)))

    def showEvent(self, e) -> None:
        super().showEvent(e)
        # macOS: round the HUD panel + give it a native shadow (no traffic lights).
        if sys.platform == "darwin" and not getattr(self, "_macos_rounded", False):
            self._macos_rounded = True
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: macos_window.round_window(self, 13))

    def apply_theme_style(self) -> None:
        """Re-apply the (theme-updated) COMPACT_STYLESHEET + repaint. apply_theme's
        generic swap misses us on macOS because the radius append changes our
        stylesheet so it no longer matches the module global it's looking for."""
        qss = COMPACT_STYLESHEET
        if sys.platform == "darwin":
            qss += "\nQWidget#compactRoot{border-radius:13px}"
        self.setStyleSheet(qss)
        self.update()

    def _tbtn(self, glyph: str, tip: str) -> QToolButton:
        b = QToolButton()
        b.setObjectName("compactBtn")
        f = b.font()
        f.setFamilies(["Font Awesome 6 Free"])
        f.setWeight(QFont.Black)  # 900 = Solid
        b.setFont(f)
        b.setText(glyph)
        b.setToolTip(tip)
        b.setCursor(Qt.PointingHandCursor)
        return b

    def set_active_mode(self, mode: str) -> None:
        """Compact's caret always points up (click -> full); kept for parity
        with the full title bar's switcher API."""
        self.caret_btn.setText(chr(0xF102))  # angles-up

    def _slim_bar(self, parent_col: QVBoxLayout):
        head = QHBoxLayout()
        head.setSpacing(6)
        label = QLabel("", objectName="compactBarLabel")
        pct = QLabel("-", objectName="compactPctLbl")
        pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        head.addWidget(label)
        head.addStretch(1)
        head.addWidget(pct)
        bar = UsageBar(height=8)
        reset = QLabel("", objectName="compactReset")
        parent_col.addLayout(head)
        parent_col.addWidget(bar)
        parent_col.addWidget(reset)
        return label, pct, bar, reset

    def _make_scoped_row(self):
        row = QWidget()
        col = QVBoxLayout(row)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(3)
        return row, self._slim_bar(col)

    @staticmethod
    def _render_scoped_row(parts, window, minutes: int, warn_at: int) -> None:
        label, pct, bar, reset = parts
        # No token figure: the local transcripts can't be split per limit.
        apply_overage_bar(label, pct, bar, window.label.upper(), window.pct, warn_at,
                          over_tag=SCOPED_OVER_TAG)
        reset.setText(scoped_reset_text(window, minutes))

    @staticmethod
    def _apply_bar(label, pct, bar, reset, title, value,
                   reset_min, tokens, show_tokens, warn_at) -> None:
        # Title + % + fill (overage handled per-window when value > 100).
        apply_overage_bar(label, pct, bar, title, value, warn_at)
        line = f"resets in {format_minutes(reset_min)}"
        if show_tokens:
            line += f" · {fmt_tokens(tokens)}"
        reset.setText(line)

    def update_usage(self, s, sr: int, wr: int, show_tokens: bool,
                     scoped=()) -> None:
        """``scoped``: ``(window, reset_minutes, warn_at)`` for each scoped
        window being shown, in order."""
        # The 5h and 7d bars turn yellow at different points — each follows its
        # own approaching-limit notification threshold.
        s_warn, w_warn = bar_warn_thresholds()
        self._apply_bar(self.s_label, self.s_pct, self.s_bar, self.s_reset,
                        "SESSION 5h", s.session_pct, sr, s.tokens_5h, show_tokens,
                        s_warn)
        self._apply_bar(self.w_label, self.w_pct, self.w_bar, self.w_reset,
                        "WEEKLY 7d", s.weekly_pct, wr, s.tokens_7d, show_tokens,
                        w_warn)
        if self.scoped_rows.set_windows(scoped):
            # Grow/shrink by the rows added or removed. Width is fixed, so a
            # steady row set (only its text changing) never needs a re-fit.
            self.scoped_rows.settle()
            self.adjustSize()

    def set_show_tokens(self, on: bool) -> None:
        self._show_tokens = bool(on)
        for row in self._rows.values():
            row.set_show_tokens(on)

    def set_sessions(self, states) -> None:
        incoming = {s.session_id: s for s in states if s.session_id}
        for sid in list(self._rows):
            if sid not in incoming:
                row = self._rows.pop(sid)
                self._list.removeWidget(row)
                row.stop()
                row.deleteLater()
        # Only churn the layout (detach + re-insert every row) when the roster
        # ORDER actually changed; a steady roster just updates rows in place.
        new_order = list(incoming)
        if new_order != list(self._rows):
            for row in self._rows.values():
                self._list.removeWidget(row)
            ordered = {}
            for index, sid in enumerate(new_order):
                row = self._rows.get(sid)
                if row is None:
                    row = CompactRow(sid, parent=self._list_widget)
                    row.set_show_tokens(self._show_tokens)
                self._list.insertWidget(index, row)
                row.show()
                ordered[sid] = row
            self._rows = ordered
        for sid, st in incoming.items():
            row = self._rows[sid]
            row.update_state(st)
            row.update_agents(getattr(st, "agents", []) or [])
        self._relayout()

    def _relayout(self) -> None:
        visible = min(max(1, len(self._rows)), self.MAX_ROWS)
        if visible == getattr(self, "_last_visible_rows", None):
            return
        self._last_visible_rows = visible
        self._scroll.setFixedHeight(visible * self.ROW_H + 6)
        self.adjustSize()

    def stop_all(self) -> None:
        for row in self._rows.values():
            row.stop()

    # --- frameless drag + grow-on-double-click (title bar only) -------------
    def _in_title(self, e) -> bool:
        return self._title_bar.geometry().contains(e.position().toPoint())

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._in_title(e):
            self._press_pos = e.globalPosition().toPoint()
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        if (e.buttons() & Qt.LeftButton) and self._press_pos is not None:
            moved = (e.globalPosition().toPoint() - self._press_pos).manhattanLength()
            if moved >= QApplication.startDragDistance():
                self._press_pos = None
                winutil.start_move(self)
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._press_pos = None

    def mouseDoubleClickEvent(self, e) -> None:
        if e.button() == Qt.LeftButton and self._in_title(e):
            self.grow_requested.emit()
