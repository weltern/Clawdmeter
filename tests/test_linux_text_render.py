"""Two text-rendering defects first seen on Ubuntu 22.04.

1. ScrollingLabel was exactly ``fm.height()`` tall, and DejaVu Sans (Ubuntu's
   default UI font) draws '_' below its own descent — so the underscore's
   bottom row was clipped and "list_issues" read as "list issues". Fonts that
   leave room (Segoe UI, SF) never showed it, so the render check below only
   bites where the font actually overflows; it compares the label's ink with an
   unclipped reference of the same text, so it can't pass by measuring nothing.

2. The compact row's "·" separator had no colour rule, so it fell back to the
   SYSTEM palette — near-black on a light-mode desktop.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout  # noqa: E402

import session_shelf  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _ink(img: QImage) -> int:
    return sum(1 for y in range(img.height()) for x in range(img.width())
               if img.pixelColor(x, y).alpha() > 60)


def test_label_is_tall_enough_for_the_underscore():
    text = "_____"
    label = session_shelf.ScrollingLabel(px=10, bold=False, role="muted",
                                         letter_spacing=0, max_w=170,
                                         align=Qt.AlignLeft)
    label.setText(text)
    label.resize(170, label.height())
    label.setAttribute(Qt.WA_TranslucentBackground, True)

    # What the label actually shows: render onto a transparent image of its
    # own size, so anything below its bottom edge is lost exactly as on screen.
    shown = QImage(label.width(), label.height(), QImage.Format_ARGB32)
    shown.fill(Qt.transparent)
    label.render(shown)

    # Reference: the same text on a canvas with plenty of room below.
    fm = label._fm
    ref = QImage(label.width(), fm.height() + 12, QImage.Format_ARGB32)
    ref.fill(Qt.transparent)
    p = QPainter(ref)
    p.setFont(label._font)
    p.setPen(QColor("#ffffff"))
    p.drawText(0, fm.ascent(), text)
    p.end()

    want = _ink(ref)
    if want == 0:
        pytest.skip("no usable font on this platform plugin")
    got = _ink(shown)
    assert got >= want * 0.9, (
        f"only {got}/{want} underscore pixels visible — the label is "
        f"{label.height()}px tall and clips below its baseline")

