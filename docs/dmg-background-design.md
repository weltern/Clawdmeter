# DMG background — design is LOCKED

**Status: final. Nick signed off 2026-07-26. Do not redesign, "improve," or
revert this without his explicit say-so.**

The macOS drag-to-install background is the **"Light · THINKING"** design,
settled after a nine-round mockup pass (full history in the session artifact:
<https://claude.ai/code/artifact/32b7244e-7e2f-4b45-9c5f-2b7eb9b672d8>).
Landed in commit `52034f4` on `feat/macos-native-window`.

## What it is

- Flat warm-paper field `#f6f1ea` with subtle deterministic grain
- Clawd's session-shelf treatment baked around the app icon slot:
  `● THINKING` + `PONDERING ITS NEW HOME` beneath where Finder places the
  icon. The slot itself is left **bare** — no glow or silhouette behind it
  (the original blue glow was dropped in `80a9745`, 2026-07-29, because its
  mascot shape clashed with the rounded tile macOS 26 wraps every app icon in)
- Four salmon (`#CE7D6B`) chevrons stepping toward Applications
- One instruction line: *Drag Clawdmeter to Applications*

## Files (keep together)

| File | Role |
|---|---|
| `packaging/make_dmg_background.py` | Renders the art (source of truth) |
| `packaging/dmg-background.png` / `@2x.png` | Checked-in output — regenerate only via the script |
| `packaging/dmg_settings.py` | Icon slots (150,190)/(450,190) @128 — geometry mirrored in the art script; change both or neither |

Regenerate with `python packaging/make_dmg_background.py` — output is
byte-reproducible (grain is seeded), so a clean regen produces no diff.

## If this conflicts in a merge

Keep the `52034f4` version (or whatever descendant of it carries this doc).
The pre-2026-07-26 art (dark field, dotted-arrow) is obsolete — a conflict
resolution that resurrects it is a regression.

## Why the light field (platform constraints, verified)

These are facts, not taste — rediscovering them costs a session:

1. A DMG background is **one baked image**; it cannot follow system dark/light.
2. With any custom background, **Finder draws the filename labels in dark
   light-mode style in every system theme** (verified via the DropDMG
   developer and by extracting Slack/Discord/Arc's shipped DMGs — all light).
   A dark field makes "Clawdmeter"/"Applications" unreadable for everyone.
3. The titlebar is Finder chrome and follows the *user's* theme — art/chrome
   mismatch is unavoidable for half the audience and is normal (VS Code,
   Slack, et al. all live with it).

With the slot bare there is no icon alignment left to verify. The mounted DMG
was eyeballed on an Apple Silicon Mac running macOS 26.6.1 (2026-09-27): the
icon sits cleanly in its Tahoe tile, both labels read, and the chevrons point at
Applications. Still eyeball it before any release that changes `packaging/`
art or `assets/icon.*`.
