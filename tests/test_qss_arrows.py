"""The spin-box and drop-down arrows render as triangles, in the theme's colour.

Regression: the stylesheet drew them with the CSS border-triangle trick, which
Qt's stylesheet engine does not support — it fills the whole border box, so
every arrow was a solid 8x5 block (seen on macOS 26 and Windows 11). These
tests render the real widgets under the real stylesheet and measure the arrow.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QHBoxLayout, QSpinBox, QWidget,
)

import theme  # noqa: E402

_app = QApplication.instance() or QApplication([])


def _render(p):
    host = QWidget()
    host.setStyleSheet(theme.build_qss(p))
    lay = QHBoxLayout(host)
    combo = QComboBox()
    combo.addItems(["Nord", "Daybreak"])
    combo.setFixedWidth(200)
    spin = QSpinBox()
    spin.setFixedWidth(80)
    lay.addWidget(combo)
    lay.addWidget(spin)
    host.resize(320, 60)
    host.show()
    _app.processEvents()
    images = combo.grab().toImage(), spin.grab().toImage()
    host.close()
    return images


def _arrow_rows(img, x_from, colour, y_range=None):
    """Width of the arrow on each row: pixels close to ``colour`` in the
    right-hand strip starting at ``x_from``. Anti-aliased edges are excluded by
    the tolerance, so only the arrow's solid core is counted."""
    want = QColor(colour)
    rows = []
    ys = y_range if y_range is not None else range(img.height())
    for y in ys:
        n = 0
        for x in range(x_from, img.width()):
            c = img.pixelColor(x, y)
            if (abs(c.red() - want.red()) + abs(c.green() - want.green())
                    + abs(c.blue() - want.blue())) < 60:
                n += 1
        if n:
            rows.append(n)
    return rows


def _assert_triangle(rows, pointing, what):
    assert rows, f"{what}: no arrow drawn at all"
    assert len(set(rows)) > 1, (
        f"{what}: every row is {rows[0]}px wide — a block, not a triangle")
    if pointing == "down":
        assert rows[0] > rows[-1], f"{what}: widest row is not the top: {rows}"
    else:
        assert rows[0] < rows[-1], f"{what}: widest row is not the bottom: {rows}"


def test_combo_arrow_is_a_downward_triangle():
    p = theme.MIDNIGHT_SALMON
    combo, _ = _render(p)
    _assert_triangle(_arrow_rows(combo, combo.width() - 30, p.text_dim),
                     "down", "combo arrow")


def test_spin_arrows_are_triangles_pointing_up_and_down():
    p = theme.MIDNIGHT_SALMON
    _, spin = _render(p)
    mid = spin.height() // 2
    x = spin.width() - 15
    _assert_triangle(_arrow_rows(spin, x, p.text_dim, range(0, mid)),
                     "up", "spin up arrow")
    _assert_triangle(_arrow_rows(spin, x, p.text_dim, range(mid, spin.height())),
                     "down", "spin down arrow")


def test_arrows_follow_the_theme():
    # Daybreak is light: a leftover dark-theme arrow would be invisible on it.
    p = theme.get("Daybreak")
    assert p.text_dim.lower() != theme.MIDNIGHT_SALMON.text_dim.lower()
    combo, _ = _render(p)
    _assert_triangle(_arrow_rows(combo, combo.width() - 30, p.text_dim),
                     "down", "Daybreak combo arrow in Daybreak's text_dim")


def test_every_arrow_placeholder_is_resolved():
    for name in ("Midnight Salmon", "Nord", "Daybreak"):
        qss = theme.build_qss(theme.get(name))
        assert "{arrow:" not in qss, f"{name}: an arrow placeholder leaked"


def test_an_unwritable_cache_drops_the_arrow_instead_of_crashing(monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(theme, "_arrow_url", boom)
    qss = theme.build_qss(theme.MIDNIGHT_SALMON)
    assert "url()" in qss and "{arrow:" not in qss
