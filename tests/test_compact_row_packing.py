"""A compact-view session row keeps its status line packed from the left.

Regression: the row's target label is capped at 170px, so a wide row had slack
left over and Qt handed it to the status labels — the dot's box grew to ~40px
and 'IDLE' sat ~30px away from its dot (seen on macOS 26 in every row, where
the narrower font leaves slack even beside a long activity name). The fix must
not buy that by shrinking the target, which would elide file names sooner.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import session_shelf  # noqa: E402
from transcript import Activity, TranscriptState  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _row(activity, target, idle):
    row = session_shelf.CompactRow("s1")
    row.setFixedWidth(session_shelf.CompactView.WIDTH - 20)
    row.update_state(TranscriptState(
        activity=activity, tool_name=None, transcript_path=None,
        last_event_ts=time.time() - 240, project_name="Review clawdmeter UI",
        target=target, is_stale=idle))
    row.show()
    _app.processEvents()
    return row


def _gap(left, right):
    return right.geometry().x() - left.geometry().right() - 1


def test_idle_dot_sits_next_to_its_label():
    row = _row(Activity.IDLE, None, True)
    try:
        assert row.dot.width() == row.dot.sizeHint().width(), (
            f"dot box is {row.dot.width()}px for a "
            f"{row.dot.sizeHint().width()}px glyph — it swallowed the slack")
        assert _gap(row.dot, row.activity) <= 5, (   # the line's setSpacing(5)
            f"{_gap(row.dot, row.activity)}px between the dot and IDLE")
    finally:
        row.close()


def test_a_long_target_keeps_its_full_width():
    row = _row(Activity.READING, "src/a_rather_long_module_name_here.py", False)
    try:
        assert row.target.width() == row.target.maximumWidth(), (
            f"target squeezed to {row.target.width()}px — names elide sooner")
        assert row.dot.width() == row.dot.sizeHint().width()
    finally:
        row.close()
