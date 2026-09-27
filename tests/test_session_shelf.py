"""Offscreen smoke test for the mascot shelf UI (Variant C).

Builds a SessionShelf, feeds it fake sessions, and asserts the diff-by-id
behaviour: tiles are added, reused (not rebuilt), updated, and removed. Runs
fully headless via QT_QPA_PLATFORM=offscreen so it works in CI with no display.

This depends on transcript.py exposing the contract symbols (ACTIVITY_COLORS
and the extended TranscriptState fields). While transcript.py is being edited in
parallel those may be absent — in that case the whole module is skipped rather
than failing, so this test never blocks the shelf work.

Run with `python -m pytest tests/ -q`, or directly:
`python tests/test_session_shelf.py`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

# Comfortably longer than the enter/leave/resize animations so they settle.
SETTLE_MS = 400

# The shelf codes to the locked contract; if transcript.py hasn't landed the
# new symbols yet, skip cleanly instead of erroring the suite.
transcript = pytest.importorskip("transcript")
if not hasattr(transcript, "ACTIVITY_COLORS"):
    pytest.skip("transcript.ACTIVITY_COLORS not present yet", allow_module_level=True)

from transcript import Activity, AgentState, TranscriptState  # noqa: E402
import session_shelf  # noqa: E402
from session_shelf import (  # noqa: E402
    CompactRow,
    CompactView,
    SessionShelf,
    SessionTile,
)
from transcript import TokenUsage  # noqa: E402


_app = QApplication.instance() or QApplication([])


def _state(session_id: str, activity: Activity, *, project: str = "proj",
           tool: str | None = None, stale: bool = False) -> TranscriptState:
    return TranscriptState(
        activity=activity,
        tool_name=tool,
        transcript_path=None,
        last_event_ts=1000.0,
        session_id=session_id,
        cwd=None,
        project_name=project,
        is_stale=stale,
    )


def test_sprite_size_scales_with_count():
    f = SessionShelf._sprite_size_for
    assert f(0) == 200
    assert f(1) == 200
    assert f(2) == 160
    assert f(3) == 130
    assert f(4) == 110
    assert f(6) == 110


def test_add_update_remove_diffs_by_id():
    shelf = SessionShelf()

    shelf.set_sessions([
        _state("a", Activity.CODING, project="alpha"),
        _state("b", Activity.THINKING, project="beta"),
    ])
    assert set(shelf._tiles) == {"a", "b"}
    assert shelf.header.text() == "ACTIVE SESSIONS — 2"

    # Same ids again -> tiles reused, not rebuilt (diffing, no flicker).
    tile_a = shelf._tiles["a"]
    shelf.set_sessions([
        _state("a", Activity.READING, project="alpha"),
        _state("b", Activity.THINKING, project="beta"),
    ])
    assert shelf._tiles["a"] is tile_a
    assert tile_a.project_label.text() == "alpha"

    # Drop one, add another -> count and membership track the diff.
    shelf.set_sessions([
        _state("a", Activity.CODING, project="alpha"),
        _state("c", Activity.PLANNING, project="gamma"),
    ])
    assert set(shelf._tiles) == {"a", "c"}
    assert shelf.header.text() == "ACTIVE SESSIONS — 2"

    shelf.set_sessions([])
    assert shelf._tiles == {}
    assert shelf.header.text() == "ACTIVE SESSIONS — 0"

    shelf.stop_all()


def test_tile_idle_shows_last_active_and_calm_anim():
    tile = SessionTile("x", sprite_size=120)
    tile.update_state(_state("x", Activity.IDLE, project="notes-cli", stale=True))
    assert tile.activity_label.text() == "IDLE"
    assert "last active" in tile.sub_label.text() or tile.sub_label.text() == "idle"
    tile.stop()


def test_tile_live_shows_tool_and_activity():
    tile = SessionTile("y", sprite_size=120)
    tile.update_state(_state("y", Activity.CODING, project="api", tool="Edit"))
    assert tile.activity_label.text() == "CODING"
    assert tile.sub_label.text() == "Edit"
    tile.stop()


def test_tile_child_agents_add_update_remove():
    tile = SessionTile("p", sprite_size=130)
    assert tile.has_agents() is False
    assert tile._agents_box.isVisibleTo(tile) is False

    def ag(i, act):
        return AgentState(agent_id=f"agent-{i}", activity=act, tool_name=None)

    tile.update_agents([ag(0, Activity.CODING), ag(1, Activity.READING)])
    assert set(tile._agents) == {"agent-0", "agent-1"}
    assert tile.has_agents() is True

    # Reused, not rebuilt, and diffed by id.
    m0 = tile._agents["agent-0"]
    tile.update_agents([ag(0, Activity.SEARCHING), ag(2, Activity.PLANNING)])
    assert tile._agents["agent-0"] is m0
    assert set(tile._agents) == {"agent-0", "agent-2"}

    tile.update_agents([])
    assert tile.has_agents() is False
    assert tile._agents_box.isVisibleTo(tile) is False  # row hidden again
    tile.stop()


def _layout_order(shelf: SessionShelf) -> list[str]:
    """Session ids in left-to-right layout order (not first-seen order)."""
    by_widget = {tile: sid for sid, tile in shelf._tiles.items()}
    order = []
    for i in range(shelf._row.count()):
        w = shelf._row.itemAt(i).widget()
        if w in by_widget:
            order.append(by_widget[w])
    return order


def test_tiles_reorder_to_match_newest_first():
    # Regression: the shelf must render in the watcher's newest-first order, not
    # the order ids were first seen. Sessions take turns being most-recent.
    shelf = SessionShelf()
    shelf.set_sessions([
        _state("a", Activity.CODING),
        _state("b", Activity.THINKING),
        _state("c", Activity.READING),
    ])
    assert _layout_order(shelf) == ["a", "b", "c"]

    # c is now newest -> it must move to the front.
    shelf.set_sessions([
        _state("c", Activity.READING),
        _state("a", Activity.CODING),
        _state("b", Activity.THINKING),
    ])
    assert _layout_order(shelf) == ["c", "a", "b"]

    # A brand-new session promoted to front while one drops off.
    shelf.set_sessions([
        _state("d", Activity.PLANNING),
        _state("c", Activity.READING),
        _state("b", Activity.THINKING),
    ])
    assert _layout_order(shelf) == ["d", "c", "b"]
    assert set(shelf._tiles) == {"b", "c", "d"}
    shelf.stop_all()


def test_survivor_mascot_holds_its_size_when_the_count_grows():
    # Regression (the horizontal-scroll rework): adding sessions must NOT shrink
    # the existing mascots. Every mascot keeps its height-driven, COUNT-
    # INDEPENDENT size and the row scrolls sideways instead — the exact opposite
    # of the old viewport//count sizing, which squeezed every mascot thinner
    # with each new session (unreadable past ~4, gone past ~7). Needs real
    # geometry, so the shelf lives in a sized host.
    from PySide6.QtWidgets import QVBoxLayout, QWidget
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    shelf = SessionShelf()
    lay.addWidget(shelf, 1)
    host.resize(460, 320)
    host.show()

    shelf.set_sessions([_state("a", Activity.CODING)])
    QTest.qWait(SETTLE_MS)
    solo = shelf._tiles["a"].sprite._render_size()
    assert solo > 0

    shelf.set_sessions([
        _state("a", Activity.CODING), _state("b", Activity.THINKING),
        _state("c", Activity.READING), _state("d", Activity.PLANNING),
        _state("e", Activity.SEARCHING),
    ])
    QTest.qWait(SETTLE_MS)
    sizes = [t.sprite._render_size() for t in shelf._tiles.values()]
    assert len(set(sizes)) == 1, f"mascots came out uneven: {sizes}"
    # The mascots did not shrink — five sessions render at the same size one did.
    assert sizes[0] >= solo, f"5 sessions shrank the mascot: {solo} -> {sizes[0]}"
    # …and because they held their size, the row overflows and scrolls.
    assert shelf._scroll.horizontalScrollBar().isVisible()

    # Drop back to one: still fine, still the same size.
    shelf.set_sessions([_state("a", Activity.CODING)])
    QTest.qWait(SETTLE_MS)
    assert shelf._tiles["a"].sprite._render_size() == solo
    shelf.stop_all()
    host.deleteLater()


def test_enter_starts_collapsed_and_leave_defers_removal():
    # Timing-free check of the transition lifecycle: a new tile starts collapsed
    # (width animation engaged) and a removed tile animates out (parked in
    # _leaving, still in the layout) rather than being deleted instantly.
    shelf = SessionShelf()
    shelf.set_sessions([_state("a", Activity.CODING)])
    shelf.set_sessions([_state("a", Activity.CODING), _state("b", Activity.THINKING)])
    tile_b = shelf._tiles["b"]
    assert tile_b.maximumWidth() == 0          # enter begins fully collapsed
    assert tile_b in shelf._anims              # its enter animation is tracked

    tile_a = shelf._tiles["a"]
    shelf.set_sessions([_state("b", Activity.THINKING)])
    assert "a" not in shelf._tiles             # removed from the live set...
    assert shelf._leaving.get("a") is tile_a   # ...but parked, animating out
    assert tile_a in shelf._anims
    # Still parented to the row (visible while it collapses), not yet deleted.
    assert tile_a.parent() is not None
    shelf.stop_all()


def test_long_title_keeps_full_text_and_tooltips_it():
    # A session title (much longer than a cwd leaf) must not stretch the tile:
    # ScrollingLabel keeps the full text (scrolls/elides only when painting) and
    # surfaces the whole title as a tooltip when it overflows.
    tile = SessionTile("s", sprite_size=110)
    long = "Review clawdmeter UI design and implementation"
    tile.update_state(_state("s", Activity.READING, project=long))
    assert tile.project_label.text() == long          # full text retained
    assert tile.project_label.toolTip() == long       # full title on hover
    assert tile.project_label._overflows() is True    # would scroll on hover


def test_short_label_has_no_tooltip_and_does_not_overflow():
    tile = SessionTile("s", sprite_size=200)
    tile.update_state(_state("s", Activity.CODING, project="api-gateway"))
    assert tile.project_label.text() == "api-gateway"
    assert tile.project_label.toolTip() == ""
    assert tile.project_label._overflows() is False


def test_label_cap_tracks_sprite_size():
    # Growing the session count shrinks tiles; the title cap follows the mascot
    # width (floored at _LABEL_MIN_W) so a long title can't widen a small tile.
    tile = SessionTile("s", sprite_size=200)
    label = "a-fairly-long-project-directory-name"
    tile.update_state(_state("s", Activity.CODING, project=label))
    assert tile.project_label.maximumWidth() == max(200, session_shelf._LABEL_MIN_W)
    tile.set_sprite_size(110)
    assert tile._label_max_w == max(110, session_shelf._LABEL_MIN_W)
    assert tile.project_label.maximumWidth() == max(110, session_shelf._LABEL_MIN_W)


def test_ago_text_and_abs_time():
    import time as _t
    assert session_shelf._ago_text(None) == "idle"
    now = _t.time()
    assert session_shelf._ago_text(now - 5) == "last active 5s ago"
    assert session_shelf._ago_text(now - 90) == "last active 1m ago"
    assert session_shelf._ago_text(now - 3700) == "last active 1h 01m ago"
    assert session_shelf._abs_time_text(None) == ""
    assert session_shelf._abs_time_text(now).startswith("Last active ")


def test_compact_row_live_then_idle():
    row = CompactRow("s")
    live = TranscriptState(
        activity=Activity.CODING, tool_name="Edit", target="x.py",
        transcript_path=None, last_event_ts=1000.0, session_id="s",
        project_name="proj", tokens=TokenUsage(input=1000, output=2000))
    row.update_state(live)
    row.update_agents([AgentState("g1", Activity.CODING, None),
                       AgentState("g2", Activity.READING, None)])
    assert row.activity.text() == "CODING"
    assert row.target.text() == "x.py"
    assert row.tokens.text() == "3.0K"          # work = input+output = 3000
    assert row.sep.isVisibleTo(row) is True
    assert row.agents.text() == "+2"

    idle = TranscriptState(
        activity=Activity.IDLE, tool_name=None, transcript_path=None,
        last_event_ts=1000.0, session_id="s", project_name="proj", is_stale=True)
    row.update_state(idle)
    row.update_agents([])
    assert row.activity.text() == "IDLE"
    assert "last active" in row.target.text()
    assert row.sep.isVisibleTo(row) is False
    assert row.agents.text() == ""


def test_compact_view_overage_overflow_bar():
    import types
    cv = CompactView()
    # weekly window past 100% (120) -> overage is derived from the window itself.
    s = types.SimpleNamespace(
        session_pct=10, weekly_pct=120,
        session_reset_minutes=120, weekly_reset_minutes=1000,
        tokens_5h=1000, tokens_7d=2000)
    cv.update_usage(s, 120, 1000, True)
    # weekly bar restarts red (value emptied, overage = pct-100), red OVERAGE tag,
    # and the % reads the full figure (120%).
    assert "WEEKLY 7d" in cv.w_label.text()
    assert "OVERAGE" in cv.w_label.text()
    assert cv.w_pct.text() == "120%"
    assert cv.w_bar._overage == 20 and cv.w_bar._value == 0
    # session under 100% stays a plain heat bar with no tag
    assert cv.s_label.text() == "SESSION 5h"
    assert cv.s_bar._overage == 0 and cv.s_bar._value == 10
    # weekly drops back under 100% -> plain bar, no tag, no overflow
    s.weekly_pct = 50
    cv.update_usage(s, 120, 1000, True)
    assert cv.w_label.text() == "WEEKLY 7d"
    assert cv.w_pct.text() == "50%"
    assert cv.w_bar._overage == 0 and cv.w_bar._value == 50


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
