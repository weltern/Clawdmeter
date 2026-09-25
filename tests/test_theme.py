"""Unit tests for the central colour palette + stylesheet builder (theme.py).

Guards the Phase-1 theming invariant that matters most: the default palette
reproduces the app's historical hardcoded stylesheet byte-for-byte, so wiring
build_qss() in is a provable no-op. Also checks that a non-default palette
actually swaps colours, and that the single-pass swap can't alias.

No Qt needed — theme.py is pure Python. Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import dataclasses
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import theme  # noqa: E402
from theme import MIDNIGHT_SALMON, Palette, build_qss  # noqa: E402


def test_default_palette_reproduces_base_qss_exactly():
    # The default theme swaps every hex for itself -> byte-identical output.
    assert build_qss(MIDNIGHT_SALMON) == theme._BASE_QSS


def test_default_output_carries_the_shipped_colours():
    qss = build_qss(MIDNIGHT_SALMON)
    assert "#CE7D6B" in qss            # salmon accent
    assert "#0e1116" in qss            # base background
    assert "#ffffff" in qss            # the literal white kept out of the palette
    assert len(qss) > 5000             # sanity: it's the full sheet, not a stub


def test_active_is_the_default_for_now():
    assert theme.active() is MIDNIGHT_SALMON


def test_every_qss_field_is_a_real_palette_attribute():
    fields = {f.name for f in dataclasses.fields(Palette)}
    for field in theme._QSS_HEX_TO_FIELD.values():
        assert field in fields, f"{field} is not a Palette field"


def test_a_changed_role_actually_swaps_in_the_output():
    custom = MIDNIGHT_SALMON.with_overrides(bg="#123456")
    qss = build_qss(custom)
    assert "#123456" in qss
    # Every base-background occurrence became the new colour.
    assert "#0e1116" not in qss


def test_tooltip_is_themed_and_follows_the_palette():
    # Tooltips (mascot hover, long-title reveal, stats-graph hover) must follow
    # the theme, not the pale system default. The rule is present, and its
    # colours swap with the palette's surface / text / border roles.
    assert "QToolTip {" in build_qss(MIDNIGHT_SALMON)
    custom = MIDNIGHT_SALMON.with_overrides(
        surface="#111111", text="#eeeeee", border="#333333")
    block = build_qss(custom).split("QToolTip {", 1)[1].split("}", 1)[0]
    assert "background-color: #111111;" in block   # surface role
    assert "color: #eeeeee;" in block              # text role
    assert "border: 1px solid #333333;" in block   # border role


def test_swap_is_single_pass_no_aliasing():
    # Swap bg <-> surface. A naive sequential .replace() would collapse both to
    # one colour; a single regex pass keeps them distinct.
    old_bg, old_surface = MIDNIGHT_SALMON.bg, MIDNIGHT_SALMON.surface
    swapped = MIDNIGHT_SALMON.with_overrides(bg=old_surface, surface=old_bg)
    qss = build_qss(swapped)
    # The root rule: background-color is bg, border is surface. After the swap
    # they trade values rather than both becoming the same colour.
    assert f"background-color: {old_surface};" in qss   # bg slot now holds surface's hex
    assert f"border: 1px solid {old_bg};" in qss         # surface slot now holds bg's hex
    # Both distinct colours still present (no collapse).
    assert old_bg in qss and old_surface in qss


def test_presets_are_full_valid_palettes():
    import re
    fields = [f.name for f in dataclasses.fields(Palette)]
    for name, pal in theme.PRESETS.items():
        for fld in fields:
            val = getattr(pal, fld)
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", val), f"{name}.{fld}={val!r}"


def test_default_is_first_preset_and_present():
    assert theme.names()[0] == theme.DEFAULT_NAME
    assert theme.DEFAULT_NAME in theme.PRESETS


def test_set_active_switches_and_active_reflects_it():
    try:
        theme.set_active("Dracula")
        assert theme.active_name() == "Dracula"
        assert theme.active() is theme.PRESETS["Dracula"]
        assert "#bd93f9" in build_qss(theme.active())  # Dracula's purple accent
    finally:
        theme.set_active(theme.DEFAULT_NAME)


def test_unknown_theme_falls_back_to_default():
    try:
        theme.set_active("No Such Theme")
        assert theme.active_name() == theme.DEFAULT_NAME
        assert theme.active() is MIDNIGHT_SALMON
    finally:
        theme.set_active(theme.DEFAULT_NAME)


def test_get_unknown_returns_default():
    assert theme.get("nope") is MIDNIGHT_SALMON


def test_light_presets_are_light_and_darks_are_dark():
    for name in ("Daybreak", "Sepia"):
        assert theme.is_light(theme.get(name)), name
    for name in ("Midnight Salmon", "Nord", "Dracula", "Amber CRT"):
        assert not theme.is_light(theme.get(name)), name


def test_system_target_maps_scheme_to_a_real_preset():
    assert theme.system_target(True) == theme.SYSTEM_DARK
    assert theme.system_target(False) == theme.SYSTEM_LIGHT
    assert theme.SYSTEM_DARK in theme.PRESETS
    assert theme.SYSTEM_LIGHT in theme.PRESETS
    assert not theme.is_light(theme.get(theme.SYSTEM_DARK))
    assert theme.is_light(theme.get(theme.SYSTEM_LIGHT))


def test_system_targets_are_selectable():
    try:
        theme.set_system_targets("Dracula", "Sepia")
        assert theme.system_targets() == ("Dracula", "Sepia")
        assert theme.system_target(True) == "Dracula"    # OS dark  -> dark target
        assert theme.system_target(False) == "Sepia"     # OS light -> light target
        theme.set_system_targets("nope", "Nord Light")   # unknown dark ignored
        assert theme.system_target(True) == "Dracula"
        assert theme.system_target(False) == "Nord Light"
    finally:
        theme.set_system_targets(theme.SYSTEM_DARK, theme.SYSTEM_LIGHT)


def test_apply_selection_records_selection_and_resolves():
    try:
        theme.apply_selection(theme.SYSTEM, "Daybreak")
        assert theme.selected() == theme.SYSTEM      # user's pick remembered
        assert theme.active_name() == "Daybreak"     # resolved concrete palette
        assert theme.active() is theme.PRESETS["Daybreak"]
    finally:
        theme.set_active(theme.DEFAULT_NAME)


def test_derive_palette_is_full_and_secondary_text_clears_aa():
    import re
    fields = [f.name for f in dataclasses.fields(Palette)]
    for seed in ("Midnight Salmon", "Riptide Light", "Dracula", "Sepia"):
        p = theme.derive_palette(theme.custom_base_from(theme.get(seed)))
        for f in fields:
            assert re.fullmatch(r"#[0-9a-fA-F]{6}", getattr(p, f)), f"{seed}.{f}"
        # Derived secondary text is clamped to AA on bg so custom themes stay
        # readable even from an odd base.
        assert theme.contrast(p.text_dim, p.bg) >= 4.4
        assert theme.contrast(p.text_muted, p.bg) >= 4.4


def test_ensure_contrast_lifts_low_pairs_leaves_good_ones():
    fixed = theme.ensure_contrast("#999999", "#ffffff", 4.5)  # mid-grey on white fails
    assert theme.contrast(fixed, "#ffffff") >= 4.5
    assert theme.ensure_contrast("#000000", "#ffffff", 4.5) == "#000000"  # already clear


def test_custom_base_edit_flows_into_derived_palette():
    try:
        base = theme.custom_base_from(theme.get("Nord"))
        base["accent"] = "#123456"
        theme.set_custom_base(base)
        assert theme.custom_base()["accent"] == "#123456"
        assert theme.custom_palette().accent == "#123456"   # base flows through
    finally:
        theme.set_custom_base(theme.custom_base_from(MIDNIGHT_SALMON))


def test_custom_theme_serialize_parse_roundtrip():
    base = theme.custom_base_from(theme.get("Dracula"))
    assert theme.parse_custom(theme.serialize_custom(base)) == base


def test_parse_custom_rejects_bad_input():
    import json
    assert theme.parse_custom("not json") is None
    assert theme.parse_custom("{}") is None                       # no roles
    assert theme.parse_custom('{"base": {"bg": "red"}}') is None   # bad colour
    base = theme.custom_base_from(MIDNIGHT_SALMON)
    assert theme.parse_custom(json.dumps(base)) == base            # bare map accepted
    partial = {k: v for k, v in base.items() if k != "accent"}
    assert theme.parse_custom(json.dumps(partial)) is None         # missing a role


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
