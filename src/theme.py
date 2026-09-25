"""Central colour palette + stylesheet builder for Clawdmeter.

Phase 1 of the v3 theming work. Every *chrome* colour the app draws — window
fills, surfaces, borders, text, the salmon accent and the semantic status
colours — now lives in one `Palette`. The main-window QSS and the
custom-painted widgets (statviz, session_shelf) build their colours from it.

This module is a *faithful* extraction: ``MIDNIGHT_SALMON`` holds the exact
hexes the app shipped with, and ``build_qss(MIDNIGHT_SALMON)`` reproduces the
old hardcoded stylesheet byte-for-byte, so wiring it in is a provable no-op.
Presets, light mode and custom themes build on top of this in later phases.

Deliberately NOT here (these encode *data*, not chrome, and stay fixed across
every theme): the per-language colours and per-activity mascot glows
(``transcript.ACTIVITY_COLORS`` / ``LANGUAGE_COLORS``), the usage heat ramp and
the overage red (``session_shelf._BAR_HEAT`` / ``_BAR_OVERAGE``), and the model
breakdown palette.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace as _replace

__all__ = ["Palette", "MIDNIGHT_SALMON", "build_qss", "active"]


@dataclass(frozen=True)
class Palette:
    """The app's chrome colours, one field per role. Values are ``#rrggbb``.

    The custom-theme editor (Phase 4) will expose only the core roles and
    *derive* the shade variants (``bg_deep`` etc.), but the presets specify
    every field explicitly so they can be hand-tuned.
    """

    # Backgrounds — window fills, base to deepest.
    bg: str             # base window background
    bg_deep: str        # title bar / settings panel (a shade under bg)
    bg_deepest: str     # compact-view title bar
    # Surfaces — raised fills: cards, buttons, hovers, dividers.
    surface: str        # cards, buttons, button:hover, dividers, bar track
    surface_dim: str    # disabled fills, compact-row hover, empty cells
    surface_sunken: str # disabled-control border
    # Lines.
    border: str         # default 1px borders, input/checkbox outlines
    border_dim: str     # disabled text, scrollbar-handle hover
    # Text — three legibility levels.
    text: str           # primary text
    text_dim: str       # secondary labels
    text_muted: str     # faint captions / section labels
    # Accent — the brand salmon (locked on presets; editable only in custom).
    accent: str         # brand accent
    accent_hover: str   # accent hover/active (a lightened accent)
    # Status — semantic; always reinforced by an icon/label elsewhere so they
    # read even for colour-blind users.
    warn: str           # approaching-limit amber
    danger: str         # over-limit / error text
    danger_strong: str  # destructive-hover fill, negative delta
    positive: str       # good / positive delta
    # State — the idle-session indicator (its label, status dot and mascot
    # glow). A theme role, NOT a fixed activity hue, so it stays readable on
    # every background. Other activity colours (reading/writing/…) are true
    # semantic hues and remain fixed in transcript.ACTIVITY_COLORS.
    idle: str

    def with_overrides(self, **changes: str) -> "Palette":
        """Return a copy with some fields replaced (used by later phases)."""
        return _replace(self, **changes)


# The default theme — the exact colours the app shipped with pre-v3.
MIDNIGHT_SALMON = Palette(
    bg="#0e1116",
    bg_deep="#0a0d12",
    bg_deepest="#0b0e13",
    surface="#1f2937",
    surface_dim="#161b22",
    surface_sunken="#21262d",
    border="#374151",
    border_dim="#4b5563",
    text="#e6edf3",
    text_dim="#9ca3af",
    text_muted="#6b7280",
    accent="#CE7D6B",
    accent_hover="#d98f7e",
    warn="#f59e0b",
    danger="#dc2626",
    danger_strong="#c13434",
    positive="#5FB3A1",
    idle="#3a3f4b",
)


# ── Preset catalogue ─────────────────────────────────────────────────────────
# Every preset is a full Palette. `accent` is an ordinary per-preset field, so
# nothing is hard-locked to salmon: to pull any preset's accent back to the
# salmon default (or push a neutral one to its own colour) is a ONE-LINE edit
# here. The only truly fixed salmon in the app is the mascot sprite (a PNG).
#
# Status colours (warn/danger/danger_strong/positive) are kept semantic and
# only lightly tuned per palette — they're always reinforced by an icon/label
# so they read for colour-blind users regardless of theme.

# Near-black OLED-friendly dark; keeps the salmon accent.
OBSIDIAN = Palette(
    bg="#0a0a0c", bg_deep="#060608", bg_deepest="#050506",
    surface="#17181c", surface_dim="#121317", surface_sunken="#1c1e23",
    border="#2c2e35", border_dim="#45474f",
    text="#eceef2", text_dim="#9a9da6", text_muted="#777a82",
    accent="#CE7D6B", accent_hover="#d98f7e",
    warn="#f59e0b", danger="#dc2626", danger_strong="#c13434", positive="#5FB3A1",
    idle="#7b7d85",
)

# Accessibility-first: pure-black canvas, high-luma text, stronger borders,
# brightened salmon so the accent clears AA on black.
HIGH_CONTRAST = Palette(
    bg="#000000", bg_deep="#000000", bg_deepest="#000000",
    surface="#161616", surface_dim="#0e0e0e", surface_sunken="#1e1e1e",
    border="#4a4a4a", border_dim="#6a6a6a",
    text="#ffffff", text_dim="#d4d4d4", text_muted="#a6a6a6",
    accent="#f2a58f", accent_hover="#ffb8a3",
    warn="#ffb020", danger="#ff5c5c", danger_strong="#ff3b3b", positive="#4ad991",
    idle="#a6a6a6",
)

# Nord — cool polar-night slate. Signature frost accent.
NORD = Palette(
    bg="#2e3440", bg_deep="#272c36", bg_deepest="#242933",
    surface="#3b4252", surface_dim="#353b48", surface_sunken="#414a5c",
    border="#4c566a", border_dim="#616e88",
    text="#eceff4", text_dim="#d8dee9", text_muted="#9aa2b1",
    accent="#88c0d0", accent_hover="#8fbcbb",
    warn="#ebcb8b", danger="#bf616a", danger_strong="#a5545c", positive="#a3be8c",
    idle="#9aa2b1",
)

# Dracula — signature purple accent.
DRACULA = Palette(
    bg="#282a36", bg_deep="#21222c", bg_deepest="#1e1f28",
    surface="#343746", surface_dim="#2b2e3b", surface_sunken="#3c4052",
    border="#44475a", border_dim="#565a71",
    text="#f8f8f2", text_dim="#c8c9da", text_muted="#8692bf",
    accent="#bd93f9", accent_hover="#cba6fa",
    warn="#ffb86c", danger="#ff5555", danger_strong="#e04a4a", positive="#50fa7b",
    idle="#8995c1",
)

# Gruvbox — warm retro. Signature orange accent.
GRUVBOX = Palette(
    bg="#282828", bg_deep="#1d2021", bg_deepest="#1b1b1b",
    surface="#3c3836", surface_dim="#32302f", surface_sunken="#45403d",
    border="#504945", border_dim="#665c54",
    text="#ebdbb2", text_dim="#d5c4a1", text_muted="#a89984",
    accent="#fe8019", accent_hover="#ff9642",
    warn="#fabd2f", danger="#fb4934", danger_strong="#cc2f26", positive="#b8bb26",
    idle="#a89984",
)

# Terminal Green — phosphor CRT. Signature green accent.
TERMINAL_GREEN = Palette(
    bg="#0c0f0c", bg_deep="#080a08", bg_deepest="#060806",
    surface="#141814", surface_dim="#101410", surface_sunken="#1a1f1a",
    border="#253025", border_dim="#3a463a",
    text="#d6f5d6", text_dim="#86c586", text_muted="#608560",
    accent="#3fdd6a", accent_hover="#5fe986",
    warn="#e0c040", danger="#ff6a5a", danger_strong="#d84545", positive="#3fdd6a",
    idle="#648864",
)

# Amber CRT — amber monochrome. Signature amber accent.
AMBER_CRT = Palette(
    bg="#100b04", bg_deep="#0b0803", bg_deepest="#090602",
    surface="#1a1206", surface_dim="#150e05", surface_sunken="#201708",
    border="#3a2a10", border_dim="#55401a",
    text="#ffcf8f", text_dim="#d99a55", text_muted="#9f723e",
    accent="#ffb000", accent_hover="#ffc233",
    warn="#ffd24d", danger="#ff6a44", danger_strong="#d84428", positive="#c0c04a",
    idle="#a27540",
)


# ── Light presets ────────────────────────────────────────────────────────────
# Light palettes invert the neutral ramp (bg is the lightest, text the darkest),
# and their status/accent colours are DARKENED versions so they clear WCAG AA on
# a light background. Values were contrast-audited (text roles >=4.5, idle >=4.2).

# Daybreak — clean cool-white daytime theme; deepened salmon accent.
DAYBREAK = Palette(
    bg="#f7f8fa", bg_deep="#eef1f5", bg_deepest="#e8ecf1",
    surface="#e6eaf0", surface_dim="#dde2e8", surface_sunken="#ccd2da",
    border="#d5dae1", border_dim="#aab2bd",
    text="#1b1f27", text_dim="#454c59", text_muted="#626a77",
    accent="#b65239", accent_hover="#d0654a",
    warn="#b45309", danger="#cf2020", danger_strong="#b31818", positive="#1c8146",
    idle="#707784",
)

# Sepia — warm paper / e-reader theme; sienna accent.
SEPIA = Palette(
    bg="#f4ecd8", bg_deep="#ece2c9", bg_deepest="#e6dcc0",
    surface="#e6dbbe", surface_dim="#e2d7ba", surface_sunken="#d0c09b",
    border="#cdbb92", border_dim="#bda57f",
    text="#3f3527", text_dim="#6a5c43", text_muted="#786749",
    accent="#a3532a", accent_hover="#b8632f",
    warn="#906008", danger="#a02b25", danger_strong="#86241f", positive="#5a7220",
    idle="#7f6d4d",
)

# Solarized Light — the classic ethanol-cream palette (AA-tuned for readability).
SOLARIZED_LIGHT = Palette(
    bg="#fdf6e3", bg_deep="#f2ecda", bg_deepest="#ede7d3",
    surface="#eee8d5", surface_dim="#e6dfc8", surface_sunken="#dcd4bb",
    border="#d9d0b8", border_dim="#b8ae95",
    text="#46565c", text_dim="#5e727a", text_muted="#647171",
    accent="#2073ae", accent_hover="#3a9bde",
    warn="#8b6900", danger="#d3302d", danger_strong="#c02824", positive="#687700",
    idle="#6d7777",
)

# Nord Light — Nord's "Snow Storm" light variant; darkened frost accent.
NORD_LIGHT = Palette(
    bg="#eceff4", bg_deep="#e2e7ee", bg_deepest="#dce3ec",
    surface="#e5e9f0", surface_dim="#dbe1ea", surface_sunken="#cfd6e2",
    border="#c3ccdb", border_dim="#a5b0c4",
    text="#2e3440", text_dim="#3b4252", text_muted="#4c566a",
    accent="#4f6c90", accent_hover="#6b8fbb",
    warn="#806816", danger="#a2525a", danger_strong="#a84850", positive="#597145",
    idle="#677183",
)

# Gruvbox Light — the warm cream Gruvbox light variant; orange accent.
GRUVBOX_LIGHT = Palette(
    bg="#fbf1c7", bg_deep="#f2e8bd", bg_deepest="#ece2b5",
    surface="#ebdbb2", surface_dim="#e3d3a8", surface_sunken="#d5c4a1",
    border="#d5c4a1", border_dim="#bdae93",
    text="#3c3836", text_dim="#504945", text_muted="#6f6553",
    accent="#af3a03", accent_hover="#c24610",
    warn="#946110", danger="#9d0006", danger_strong="#820005", positive="#746f0d",
    idle="#7c6f64",
)

# High Contrast Light — accessibility-first white; black text, deep accent.
HIGH_CONTRAST_LIGHT = Palette(
    bg="#ffffff", bg_deep="#ffffff", bg_deepest="#f4f4f4",
    surface="#f0f0f0", surface_dim="#e8e8e8", surface_sunken="#dcdcdc",
    border="#bcbcbc", border_dim="#8a8a8a",
    text="#000000", text_dim="#2a2a2a", text_muted="#4a4a4a",
    accent="#b03a1f", accent_hover="#c4472a",
    warn="#8a5a00", danger="#c11414", danger_strong="#a30f0f", positive="#0f7a3d",
    idle="#565656",
)

# Riptide — cool grey ocean with a teal accent that shifts to cyan on hover.
RIPTIDE = Palette(
    bg="#16191d", bg_deep="#101216", bg_deepest="#0c0e11",
    surface="#22262c", surface_dim="#1b1f24", surface_sunken="#2a2f36",
    border="#333a42", border_dim="#4a545d",
    text="#e6ebef", text_dim="#9aa4ad", text_muted="#7b848c",
    accent="#2dd4bf", accent_hover="#22d3ee",
    warn="#eab308", danger="#ef4444", danger_strong="#d13636", positive="#4ade80",
    idle="#747d86",
)

# Riptide Light — soft light grey (a touch deeper than near-white) with deep
# teal / cyan accents.
RIPTIDE_LIGHT = Palette(
    bg="#dcdfe1", bg_deep="#d4d8da", bg_deepest="#ced3d7",
    surface="#d2d6d8", surface_dim="#c9cdd1", surface_sunken="#bcc2c8",
    border="#bfc5ca", border_dim="#9ba4ab",
    text="#1a1f24", text_dim="#444d55", text_muted="#59626a",
    accent="#0e6d65", accent_hover="#0891b2",
    warn="#9c4808", danger="#be1c1c", danger_strong="#a81717", positive="#127035",
    idle="#5f6870",
)


# Ordered catalogue. First entry is the default. To reorder or add a preset,
# edit this dict — the Appearance picker and persistence read it directly.
PRESETS = {
    # Dark
    "Midnight Salmon": MIDNIGHT_SALMON,
    "Obsidian": OBSIDIAN,
    "High Contrast": HIGH_CONTRAST,
    "Nord": NORD,
    "Dracula": DRACULA,
    "Gruvbox": GRUVBOX,
    "Terminal Green": TERMINAL_GREEN,
    "Amber CRT": AMBER_CRT,
    "Riptide": RIPTIDE,
    # Light
    "Daybreak": DAYBREAK,
    "Sepia": SEPIA,
    "Solarized Light": SOLARIZED_LIGHT,
    "Nord Light": NORD_LIGHT,
    "Gruvbox Light": GRUVBOX_LIGHT,
    "High Contrast Light": HIGH_CONTRAST_LIGHT,
    "Riptide Light": RIPTIDE_LIGHT,
}
DEFAULT_NAME = "Midnight Salmon"

# "Follow System" is a selection, not a palette: it tracks the OS light/dark
# scheme and resolves to one of these two presets. The app reads the OS scheme
# (Qt styleHints) and calls apply_selection() with the result.
SYSTEM = "Follow System"
SYSTEM_DARK = "Midnight Salmon"   # default dark target
SYSTEM_LIGHT = "Daybreak"         # default light target
# User-selectable targets (persisted by the app); default to the constants.
_sys_dark = SYSTEM_DARK
_sys_light = SYSTEM_LIGHT


def system_target(os_is_dark: bool) -> str:
    """The preset 'Follow System' resolves to for the given OS scheme."""
    return _sys_dark if os_is_dark else _sys_light


def system_targets() -> tuple:
    """The (dark, light) presets 'Follow System' currently resolves to."""
    return _sys_dark, _sys_light


def set_system_targets(dark: str, light: str) -> None:
    """Choose which presets 'Follow System' uses for OS dark / light. Unknown
    names are ignored (keep the previous value)."""
    global _sys_dark, _sys_light
    if dark in PRESETS:
        _sys_dark = dark
    if light in PRESETS:
        _sys_light = light


# ── Colour math (WCAG) + custom-theme derivation ─────────────────────────────

def _rgb(h: str) -> tuple:
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _hx(t) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in t))


def _blend(a: str, b: str, t: float) -> str:
    """Linear RGB blend: t=0 -> a, t=1 -> b."""
    ra, rb = _rgb(a), _rgb(b)
    return _hx(tuple(ra[i] + (rb[i] - ra[i]) * t for i in range(3)))


def _rel_lum(h: str) -> float:
    def ch(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(h)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    """WCAG contrast ratio between two hex colours (1.0–21.0)."""
    la, lb = _rel_lum(a), _rel_lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def ensure_contrast(fg: str, bg: str, target: float = 4.5) -> str:
    """Return fg unchanged if it already clears `target` against bg, else nudge
    it toward black/white (whichever raises contrast) until it does."""
    if contrast(fg, bg) >= target:
        return fg
    toward = "#000000" if _rel_lum(bg) > 0.5 else "#ffffff"
    out = fg
    for i in range(1, 101):
        out = _blend(fg, toward, i / 100)
        if contrast(out, bg) >= target:
            break
    return out


def _ensure_readable_on(bg_color: str, fg: str, safe_bg: str,
                        target: float = 4.5) -> str:
    """Nudge a *background* colour toward ``safe_bg`` — where ``fg`` is already
    known readable — only as far as needed for ``fg`` to clear ``target`` on it.

    Guarantees text painted on this surface can't go invisible, while keeping
    the user's chosen hue as much as possible. Blending all the way to safe_bg
    always succeeds (fg is AA there), so the loop is bounded and can't fail."""
    if contrast(fg, bg_color) >= target:
        return bg_color
    out = bg_color
    for i in range(1, 101):
        out = _blend(bg_color, safe_bg, i / 100)
        if contrast(fg, out) >= target:
            break
    return out


def is_light(p: "Palette") -> bool:
    """Whether a palette is a light theme (used to set the OS colour-scheme hint
    so native/un-QSS'd surfaces match)."""
    return _rel_lum(p.bg) > 0.5


# The custom theme: the user edits these 8 base roles; the rest are derived.
CUSTOM = "Custom"
CUSTOM_ROLES = ("bg", "surface", "border", "text",
                "accent", "warn", "danger", "positive")


def derive_palette(base: dict) -> Palette:
    """Build a full Palette from the 8 user-edited base roles, deriving the
    shade/variant roles. Works for a light or dark base (direction flips off
    bg luminance). Text roles are clamped to WCAG AA on bg, and surface/border
    are pulled toward bg as needed so text stays AA on the buttons and hover
    fills it is painted on too — so a custom or *imported* theme can't render
    unreadable text (the in-picker contrast warning is advisory; this is the
    hard floor)."""
    bg = base["bg"]
    text = ensure_contrast(base["text"], bg, 4.5)
    # `text` is painted on surface (buttons, dropdowns) and border
    # (button:hover), not only on bg. Clamping it against bg alone let a custom
    # or imported theme pick e.g. a light surface under a dark bg and render
    # invisible button text while every text-vs-bg check still passed. Pull
    # surface and border toward bg — where text is already AA — only as far as
    # needed, so text stays readable everywhere it's drawn while keeping the
    # user's hue as much as possible.
    surface = _ensure_readable_on(base["surface"], text, bg, 4.5)
    border = _ensure_readable_on(base["border"], text, bg, 4.5)
    accent, warn, danger, positive = (base["accent"], base["warn"],
                                      base["danger"], base["positive"])
    black, white = "#000000", "#ffffff"
    hover_toward = black if _rel_lum(bg) > 0.5 else white  # darken light / lighten dark
    return Palette(
        bg=bg,
        bg_deep=_blend(bg, black, 0.16),
        bg_deepest=_blend(bg, black, 0.26),
        surface=surface,
        surface_dim=_blend(surface, bg, 0.55),
        surface_sunken=_blend(surface, border, 0.5),
        border=border,
        border_dim=_blend(border, text, 0.32),
        text=text,
        text_dim=ensure_contrast(_blend(text, bg, 0.30), bg, 4.5),
        text_muted=ensure_contrast(_blend(text, bg, 0.46), bg, 4.5),
        accent=accent,
        accent_hover=_blend(accent, hover_toward, 0.14),
        warn=warn,
        danger=danger,
        danger_strong=_blend(danger, black, 0.20),
        positive=positive,
        idle=ensure_contrast(_blend(text, bg, 0.52), bg, 4.2),
    )


_DEFAULT_CUSTOM = {r: getattr(MIDNIGHT_SALMON, r) for r in CUSTOM_ROLES}
_custom_base = dict(_DEFAULT_CUSTOM)


def custom_base() -> dict:
    """The 8 base hexes of the custom theme (a copy)."""
    return dict(_custom_base)


def _valid_hex(v: object) -> bool:
    return isinstance(v, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", v) is not None


def set_custom_base(base: dict) -> None:
    """Replace the custom theme's base colours. A missing *or malformed* role
    falls back to the default, so a corrupt persisted value or a hand-edited
    theme file can never feed an invalid hex into derive_palette (which would
    crash colour math at startup)."""
    global _custom_base
    if not isinstance(base, dict):
        base = {}
    _custom_base = {r: (base.get(r) if _valid_hex(base.get(r)) else _DEFAULT_CUSTOM[r])
                    for r in CUSTOM_ROLES}


def custom_base_from(p: "Palette") -> dict:
    """Extract the 8 base roles from a palette (used to seed Custom from the
    currently-active theme — 'copy the current theme')."""
    return {r: getattr(p, r) for r in CUSTOM_ROLES}


def custom_palette() -> Palette:
    """The full custom Palette derived from the current base colours."""
    return derive_palette(_custom_base)


def _resolve(name: str) -> Palette:
    """Concrete name -> Palette. CUSTOM builds from the base colours; unknown
    names fall back to the default."""
    if name == CUSTOM:
        return custom_palette()
    return PRESETS.get(name, MIDNIGHT_SALMON)


_THEME_FORMAT = 1

# A real theme is 8 short hex strings (~250 bytes; ~500 pretty-printed with the
# wrapper). This generous cap rejects a hostile/huge file before json.loads --
# and, crucially, before deeply-nested JSON can exhaust the recursion limit.
MAX_THEME_BYTES = 64 * 1024


def serialize_custom(base: dict) -> str:
    """A custom theme's base colours as a shareable, pretty-printed JSON string."""
    return json.dumps(
        {"clawdmeter_theme": _THEME_FORMAT,
         "base": {r: base[r] for r in CUSTOM_ROLES}},
        indent=2) + "\n"


def parse_custom(text: str) -> "dict | None":
    """Parse a theme-file string into a validated base dict, or None if it isn't
    a valid theme (bad JSON, a missing role, or a malformed colour). Accepts
    either the wrapped ``{"base": {...}}`` form or a bare ``{role: hex}`` map.

    A theme file is untrusted, shareable input, so every failure mode -- bad
    JSON, wrong type, an oversized file, or deeply-nested JSON (which makes
    json.loads raise RecursionError, a RuntimeError subclass) -- must return
    None rather than propagate and crash the app on Import."""
    if not isinstance(text, str) or len(text) > MAX_THEME_BYTES:
        return None
    try:
        data = json.loads(text)
    except Exception:   # noqa: BLE001 - untrusted input; incl. RecursionError
        return None
    if not isinstance(data, dict):
        return None
    base = data.get("base", data)
    if not isinstance(base, dict):
        return None
    out = {}
    for role in CUSTOM_ROLES:
        val = base.get(role)
        if not isinstance(val, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", val):
            return None
        out[role] = val
    return out

# Module-level "which theme is live" state. Consumers call active(); the app
# calls apply_selection() (via apply_theme in dashboard) on startup and switches.
# `_selected` is the user's pick (a preset name OR SYSTEM); `_active_*` is the
# concrete palette in force (SYSTEM resolved against the OS scheme by the caller).
_selected = DEFAULT_NAME
_active_name = DEFAULT_NAME
_active_palette = MIDNIGHT_SALMON


def names() -> list:
    """Preset display names, in catalogue order."""
    return list(PRESETS)


def selected() -> str:
    """The user's current selection — a preset name or SYSTEM."""
    return _selected


def apply_selection(selected_name: str, concrete_name: str) -> Palette:
    """Record the user's selection and set the resolved concrete palette.

    `selected_name` is what the user picked (a preset name or SYSTEM);
    `concrete_name` is the preset actually shown (the caller resolves SYSTEM
    against the OS scheme first). Unknown concretes fall back to the default.
    """
    global _selected, _active_name, _active_palette
    _selected = selected_name
    if concrete_name != CUSTOM and concrete_name not in PRESETS:
        concrete_name = DEFAULT_NAME
    _active_name = concrete_name
    _active_palette = _resolve(concrete_name)
    return _active_palette


def get(name: str) -> Palette:
    """The palette for `name`, or the default if unknown."""
    return PRESETS.get(name, MIDNIGHT_SALMON)


def active_name() -> str:
    """Name of the palette currently in force."""
    return _active_name


def set_active(name: str) -> Palette:
    """Make `name` the active palette (falls back to default if unknown).

    Pure state — emits no signal and restyles nothing. The app's apply_theme()
    orchestrates the refresh/restyle after calling this.
    """
    global _active_name, _active_palette, _selected
    if name in PRESETS:
        _active_name = _selected = name
        _active_palette = PRESETS[name]
    else:
        _active_name = _selected = DEFAULT_NAME
        _active_palette = MIDNIGHT_SALMON
    return _active_palette


# Maps every hex that appears in the base stylesheet to the Palette field it
# plays. Pure white (#ffffff — text on the destructive-hover red) is left as a
# literal in the QSS: it's universal across light/dark and isn't a theme role.
_QSS_HEX_TO_FIELD = {
    "#0e1116": "bg",
    "#0a0d12": "bg_deep",
    "#1f2937": "surface",
    "#161b22": "surface_dim",
    "#21262d": "surface_sunken",
    "#374151": "border",
    "#4b5563": "border_dim",
    "#e6edf3": "text",
    "#9ca3af": "text_dim",
    "#6b7280": "text_muted",
    "#CE7D6B": "accent",
    "#d98f7e": "accent_hover",
    "#f59e0b": "warn",
    "#dc2626": "danger",
    "#c13434": "danger_strong",
}

# Case-sensitive, single-pass swap. One regex pass (not sequential .replace)
# so a role whose new value equals another role's original hex can't be
# double-substituted (aliasing).
_HEX_RE = re.compile("|".join(re.escape(h) for h in _QSS_HEX_TO_FIELD))


# The main-window stylesheet, verbatim as it shipped. build_qss() swaps the
# palette hexes; with MIDNIGHT_SALMON every swap is hex->same-hex, i.e. a no-op.
_BASE_QSS = """
QWidget#root {
    background-color: #0e1116;
    border: 1px solid #1f2937;
}

/* Tooltips (mascot hover token breakdown, long-title reveals, the stats-graph
   hover, etc.) follow the theme instead of the pale system default. No
   border-radius: a Qt tooltip is an opaque rectangular window, so a rounded
   border would leave square corners showing through. */
QToolTip {
    background-color: #1f2937;
    color: #e6edf3;
    border: 1px solid #374151;
    padding: 5px 8px;
}

QWidget#titleBar { background-color: #0a0d12; }
QLabel#titleAppName {
    font-size: 12px; color: #e6edf3; font-weight: 600; letter-spacing: 1px;
}
QToolButton#titleBtn, QToolButton#closeBtn, QToolButton#settingsBtn {
    background: transparent; color: #CE7D6B; border: 0;
    min-width: 38px; min-height: 30px;
    font-family: "Font Awesome 6 Free"; font-weight: 900;
}
QToolButton#titleBtn, QToolButton#closeBtn { font-size: 13px; }
QToolButton#settingsBtn { font-size: 15px; }
QToolButton#titleBtn:hover, QToolButton#settingsBtn:hover { background-color: #1f2937; color: #CE7D6B; }
QToolButton#closeBtn:hover { background-color: #c13434; color: #ffffff; }

QLabel#title { font-size: 22px; font-weight: 700; letter-spacing: 1px; color: #e6edf3; }
QLabel#group { font-size: 13px; font-weight: 600; color: #9ca3af; letter-spacing: 2px; }
QLabel#rowLabel { font-size: 14px; color: #9ca3af; }
QLabel#pct { font-size: 40px; font-weight: 700; color: #e6edf3; }
QLabel#reset { font-size: 12px; color: #9ca3af; }
QLabel#statusText { font-size: 12px; font-weight: 600; }
QLabel#statusText[level="warn"] { color: #f59e0b; }
QLabel#statusText[level="block"] { color: #dc2626; }
QLabel#statusIcon { font-size: 14px; font-family: "Segoe UI Emoji", "Apple Color Emoji", "Noto Color Emoji", sans-serif; }

QPushButton {
    background-color: #1f2937; color: #e6edf3; border: 1px solid #374151;
    padding: 6px 12px; border-radius: 6px;
}
QPushButton:hover { background-color: #374151; }
QPushButton:disabled { background-color: #161b22; color: #4b5563; border-color: #21262d; }

QWidget#settingsPanel {
    background-color: #0a0d12;
}
/* Left tab rail in the settings page (sits right of the app nav rail). */
QWidget#settingsNav {
    background-color: #0e1116;
    border-right: 1px solid #1f2937;
}
/* QPushButton (not QToolButton) so QSS text-align actually left-aligns the
   glyph+label. Segoe UI is primary so the Latin label stays crisp — FA Free
   ships its own (ugly) Latin, so listing it first would hijack the words. The
   leading FA glyph isn't in Segoe UI, so Qt falls back to Font Awesome for it.
   FA is registered at startup in main.py via QFontDatabase. */
QPushButton#navBtn {
    background: transparent; color: #9ca3af; border: 0;
    border-radius: 6px; padding: 9px 14px;
    text-align: left; font-size: 13px; font-weight: 600;
    font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", "DejaVu Sans", "Font Awesome 6 Free", sans-serif;
}
QPushButton#navBtn:hover { background-color: #1f2937; color: #e6edf3; }
QPushButton#navBtn:checked { background-color: #1f2937; color: #CE7D6B; }
QScrollArea#settingsScroll, QWidget#settingsBody { background: transparent; border: none; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 2px 0; }
QScrollBar::handle:vertical { background: #374151; border-radius: 4px; min-height: 24px; }
QScrollBar::handle:vertical:hover { background: #4b5563; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QLabel#settingsTitle {
    font-size: 16px; font-weight: 700; color: #e6edf3; letter-spacing: 2px;
}
QLabel#sectionLabel {
    font-size: 10px; color: #6b7280; letter-spacing: 2px; font-weight: 600;
}
QLabel#pathDisplay {
    font-size: 10px; color: #9ca3af;
    background: #0e1116; border: 1px solid #1f2937; border-radius: 4px;
    padding: 8px;
}
QLabel#credStatus { font-size: 10px; color: #6b7280; }
QLabel#sectionHint { font-size: 10px; color: #6b7280; }
QLabel#pollNote { font-size: 10px; color: #f59e0b; font-weight: 600; }

/* Stats page cards */
QFrame#statCard {
    background-color: #0e1116; border: 1px solid #1f2937; border-radius: 8px;
}
QLabel#statLabel { font-size: 10px; color: #6b7280; letter-spacing: 2px; font-weight: 600; }
QLabel#statBig { font-size: 32px; font-weight: 700; color: #e6edf3; }
QLabel#statMid { font-size: 18px; font-weight: 700; color: #e6edf3; }
QLabel#statPlan { font-size: 12px; color: #CE7D6B; font-weight: 600; letter-spacing: 1px; }
QLabel#statDelta { font-size: 12px; font-weight: 700; }
QLabel#statCount { font-size: 11px; color: #6b7280; font-weight: 600; }
QFrame#statDivider { background: #1f2937; max-height: 1px; min-height: 1px; border: 0; }
QPushButton#resetLink {
    background: transparent; color: #9ca3af; border: 0; padding: 2px 4px;
    text-decoration: underline; font-size: 10px;
}
QPushButton#resetLink:hover { color: #e6edf3; }
QCheckBox { color: #e6edf3; font-size: 12px; spacing: 8px; padding: 3px 0; }
QCheckBox::indicator {
    width: 16px; height: 16px; border: 1px solid #374151;
    background-color: #1f2937; border-radius: 2px;
}
QCheckBox::indicator:hover { border-color: #6b7280; }
QCheckBox::indicator:checked {
    background-color: #CE7D6B; border-color: #CE7D6B;
    image: none;
}

/* Text/number inputs — the app had no input styling, so these fell back to the
   native light Windows look. Theme them to match the dark surface: poll-interval
   field, push-channel editors, and the threshold / idle spinners. */
QLineEdit, QSpinBox {
    background-color: #0e1116; color: #e6edf3;
    border: 1px solid #374151; border-radius: 6px;
    padding: 4px 8px;
    selection-background-color: #CE7D6B; selection-color: #0a0a0a;
}
QLineEdit:focus, QSpinBox:focus { border-color: #CE7D6B; }
QLineEdit:disabled, QSpinBox:disabled {
    color: #4b5563; background-color: #161b22; border-color: #21262d;
}
QSpinBox::up-button, QSpinBox::down-button {
    subcontrol-origin: border; width: 15px; background: #1f2937;
    border-left: 1px solid #374151;
}
QSpinBox::up-button { subcontrol-position: top right; border-top-right-radius: 6px; }
QSpinBox::down-button { subcontrol-position: bottom right; border-bottom-right-radius: 6px; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #374151; }
QSpinBox::up-arrow {
    width: 0; height: 0; image: none;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-bottom: 5px solid #9ca3af;
}
QSpinBox::down-arrow {
    width: 0; height: 0; image: none;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #9ca3af;
}
QSpinBox::up-arrow:hover { border-bottom-color: #e6edf3; }
QSpinBox::down-arrow:hover { border-top-color: #e6edf3; }

/* Preset dropdown (Appearance page) — swatch icon + name per item. */
QComboBox {
    background-color: #1f2937; color: #e6edf3; border: 1px solid #374151;
    border-radius: 6px; padding: 4px 8px 4px 10px; min-width: 96px;
}
QComboBox:hover { border-color: #4b5563; }
QComboBox:focus, QComboBox:on { border-color: #CE7D6B; }
QComboBox::drop-down { border: 0; width: 22px; }
QComboBox::down-arrow {
    image: none; width: 0; height: 0; margin-right: 8px;
    border-left: 4px solid transparent; border-right: 4px solid transparent;
    border-top: 5px solid #9ca3af;
}
QComboBox QAbstractItemView {
    background-color: #161b22; color: #e6edf3;
    border: 1px solid #374151; border-radius: 8px; padding: 5px; outline: none;
}
QComboBox QAbstractItemView::item {
    padding: 5px 8px; min-height: 22px; border-radius: 5px; color: #e6edf3;
}
QComboBox QAbstractItemView::item:hover { background-color: #1f2937; }
QComboBox QAbstractItemView::item:selected { background-color: #1f2937; color: #CE7D6B; }

/* Approaching-limit threshold sliders: dark groove, salmon fill up to the
   handle, salmon handle, with a value pill beside it. */
QSlider#threshold::groove:horizontal { height: 4px; border-radius: 2px; background: #1f2937; }
QSlider#threshold::add-page:horizontal { background: #1f2937; border-radius: 2px; }
QSlider#threshold::sub-page:horizontal { background: #CE7D6B; border-radius: 2px; }
QSlider#threshold::handle:horizontal {
    width: 13px; height: 13px; margin: -5px 0; border-radius: 7px;
    background: #CE7D6B; border: 2px solid #0a0d12;
}
QSlider#threshold::handle:horizontal:hover { background: #d98f7e; }
/* Editable value field beside the slider — looks like a pill, but click + type.
   It lights up with the salmon focus border both when focused and while its
   slider is being dragged ([sliding="true"]). */
QSpinBox#thresholdField { padding: 3px 4px; font-weight: 600; }
QSpinBox#thresholdField[sliding="true"] { border-color: #CE7D6B; }

/* Slim left nav rail (overlay). Same icon+label language as the settings tabs:
   Segoe UI primary so labels stay crisp; the leading FA glyph falls back to FA.
   Labels are clipped while the rail is collapsed and revealed as it expands. */
QWidget#navRail {
    background-color: #0e1116;
    border-right: 1px solid #1f2937;
}
QPushButton#railBtn {
    background: transparent; color: #9ca3af; border: 0;
    border-radius: 6px; padding: 9px 0px 9px 8px;  /* no right pad: icon never clips,
                                                       and stays put as the rail widens */
    text-align: left; font-size: 15px; font-weight: 600;
    font-family: "Segoe UI", "Helvetica Neue", "Noto Sans", "DejaVu Sans", "Font Awesome 6 Free", sans-serif;
}
QPushButton#railBtn:hover { background-color: #1f2937; color: #e6edf3; }
QPushButton#railBtn:checked { background-color: #1f2937; color: #CE7D6B; }

/* Push-notification channel cards (Settings -> Notifications).
   Defined by the BORDER, not the fill. The card used to be `bg` on a `bg_deep`
   panel, which is a 1.03-1.08 contrast ratio in every theme — invisible. And no
   fill fixes it: in the light presets bg_deep/bg/surface all sit within 1.07 of
   each other, so a configured channel looked washed out with nothing to
   separate it from the panel. border_dim is the one slot that stays visible
   either way (1.94 dark, 1.77 light). */
QWidget#pushCard {
    background-color: #1f2937; border: 1px solid #4b5563; border-radius: 6px;
}
QLabel#pushSummary { font-size: 12px; }
QToolButton#pushEditBtn {
    background: transparent; color: #9ca3af; border: 0;
    padding: 2px 6px; border-radius: 4px; font-size: 11px;
}
QToolButton#pushEditBtn:hover { color: #CE7D6B; background-color: #1f2937; }
QToolButton#pushRemoveBtn {
    background: transparent; color: #6b7280; border: 0;
    padding: 2px 7px; border-radius: 4px; font-size: 12px;
}
QToolButton#pushRemoveBtn:hover { color: #ffffff; background-color: #c13434; }
QToolButton#addChannelBtn {
    background: transparent; color: #CE7D6B; border: 1px dashed #374151;
    padding: 5px 12px; border-radius: 6px; font-size: 11px;
}
QToolButton#addChannelBtn:hover { background-color: #1f2937; border-color: #CE7D6B; }
QToolButton#addChannelBtn:disabled { color: #4b5563; border-color: #21262d; }
QToolButton#addChannelBtn::menu-indicator { image: none; width: 0; }

QWidget#miniRoot {
    background-color: #0e1116;
    border: 1px solid #CE7D6B;
}
QLabel#miniPct { font-size: 17px; font-weight: 700; color: #e6edf3; }
QLabel#miniPctSub { font-size: 13px; font-weight: 700; color: #9ca3af; }
QLabel#miniReset { font-size: 12px; color: #9ca3af; }

/* Popup menus. Qt gives an unstyled QMenu no background of its own, which on
   macOS renders as a translucent panel you can read the window through — the
   "+ Add a channel" list was see-through over the button behind it. Styling it
   here (palette hexes, so it re-themes) covers every QMenu in the app: the
   channel picker, the title-bar menu and the session-tile context menu. The
   tray menu is a native NSMenu on macOS and is unaffected either way. */
/* ThemedPopup — the drop-down that replaces QMenu on macOS, where a menu's
   panel is painted by the platform with a vibrancy material and cannot be made
   opaque. Built like the toast (a card carrying this stylesheet), which does
   render solid there. */
QWidget#popupRoot {
    background-color: #1f2937;
    border: 1px solid #374151;
    border-radius: 8px;
}
QPushButton#popupItem {
    background: transparent;
    border: none;
    color: #e6edf3;
    text-align: left;
    padding: 6px 18px 6px 10px;
    border-radius: 5px;
}
QPushButton#popupItem:hover { background-color: #CE7D6B; color: #0e1116; }

QMenu {
    background-color: #1f2937;
    border: 1px solid #374151;
    border-radius: 8px;
    padding: 4px;
}
QMenu::item {
    background: transparent;
    color: #e6edf3;
    padding: 6px 22px 6px 12px;
    border-radius: 5px;
}
QMenu::item:selected { background-color: #CE7D6B; color: #0e1116; }
QMenu::item:disabled { color: #6b7280; }
QMenu::separator { height: 1px; background: #374151; margin: 4px 8px; }

QWidget#toastRoot {
    background-color: #0e1116;
    border: 1px solid #CE7D6B;
}
QLabel#toastTitle {
    font-size: 14px; font-weight: 700; color: #e6edf3; letter-spacing: 0.5px;
}
QLabel#toastBody { font-size: 12px; color: #9ca3af; }
/* Dismiss affordance. Revealed on hover, like the system notification centres:
   without it the whole toast is one "open the app" target, so anyone who just
   wants it gone has to either wait out the timer or get a window they didn't
   ask for. Styled here rather than inline so it follows a live theme switch. */
QToolButton#toastClose {
    background: transparent; border: none; color: #9ca3af;
    font-size: 13px; font-weight: 700; padding: 0px;
}
QToolButton#toastClose:hover { color: #e6edf3; }

/* Appearance settings page — theme picker. Rows use palette hexes so the
   picker itself re-themes with the active theme; the per-row swatch chips are
   styled inline with each preset's own colours (they must NOT re-theme). */
QFrame#themeOption {
    background-color: #0e1116; border: 1px solid #1f2937; border-radius: 8px;
}
QFrame#themeOption:hover { border-color: #374151; }
QFrame#themeOption[selected="true"] { border-color: #CE7D6B; }
QLabel#themeName { font-size: 13px; font-weight: 600; color: #e6edf3; }
QLabel#themeTag { font-size: 10px; font-weight: 700; color: #CE7D6B; letter-spacing: 1px; }

/* Custom-theme editor — the role list on the left of the Studio editor. */
QFrame#editorRow { background: transparent; border: 0; border-radius: 6px; }
QFrame#editorRow:hover { background: #161b22; }
QFrame#editorRow[selected="true"] { background: #1f2937; }
QLabel#roleName { font-size: 13px; color: #e6edf3; }
QFrame#editorRow[selected="true"] QLabel#roleName { color: #CE7D6B; font-weight: 600; }
QPushButton#eyedropBtn { font-size: 13px; padding: 6px 0; }
QPushButton#applyBtn {
    background-color: #CE7D6B; border-color: #CE7D6B; color: #0a0d12; font-weight: 600;
}
QPushButton#applyBtn:hover { background-color: #d98f7e; border-color: #d98f7e; }
"""


def build_qss(p: Palette) -> str:
    """Return the main-window stylesheet rendered from palette ``p``.

    With ``MIDNIGHT_SALMON`` the output is byte-identical to the historical
    hardcoded stylesheet (guarded by ``tests/test_theme.py``)."""
    return _HEX_RE.sub(lambda m: getattr(p, _QSS_HEX_TO_FIELD[m.group(0)]), _BASE_QSS)


def active() -> Palette:
    """The palette currently in force (see set_active())."""
    return _active_palette
