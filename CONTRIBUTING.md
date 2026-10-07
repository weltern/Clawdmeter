Contributions are welcome — this is a small personal project, so a little
process keeps it manageable:

- **Small fixes** (typos, obvious bugs, minor UI tweaks) — open a PR directly.
- **Anything bigger** (new features, mascot moods, behavior changes, refactors)
  — please open an issue first so we can agree on the approach before you spend
  time on it.
- **Test your change** with `--mock` for UI work, or against a live Claude Code
  session for mascot/transcript behavior, and keep each PR focused on one thing.

**Please don't:**

- **Modify, add, or relicense the Clawd mascot art.** The sprites
  (`assets/sprites/`) are © Anthropic under a deliberate carve-out — see
  [NOTICE](NOTICE) and the license section. They are not MIT-licensed.
  `claude-code/clawdmeter/hooks/sprites.ts` is generated from them by
  `tools/build_band_sprites.py`; never edit it by hand.
- **Add new runtime dependencies** without discussing it first. The footprint is
  intentionally tiny (PySide6 + httpx); let's keep it lean.
- **Bundle large refactors or unrelated reformatting** into a feature/fix PR.
  Small, focused diffs get reviewed and merged faster.

**Cutting a release:**

1. Bump `APP_VERSION` in `src/app_settings.py` to match the new tag.
2. `./build.ps1` — produces `dist/Clawdmeter.exe` and `dist/Clawdmeter.exe.sha256`.
3. Tag and publish: `gh release create vX.Y.Z dist/Clawdmeter.exe dist/Clawdmeter.exe.sha256`
   with notes. **Always upload the `.sha256`** (and/or paste the hash into the
   notes) — the in-app update check reads it to verify a download before
   swapping the exe, and shipping it now keeps that path ready.

The app checks GitHub's *latest release* on launch (then ~daily) and surfaces an
"Update available" tray item; it compares the running `APP_VERSION` against the
release tag, so the two must stay in lockstep.

**The Clawdmeter Claude Code plugin (`claude-code/clawdmeter/`)** is a Claude Code plugin
that ships from this repo; `.claude-plugin/marketplace.json` makes the repo its
plugin source. It has its own `version` in
`claude-code/clawdmeter/.claude-plugin/plugin.json`, and users get a new
version through `claude plugin update`.

- **Never publish a GitHub Release for the plugin.** The app's update check reads
  *latest release*. A plugin tag such as `claude-plugin-v0.3.0` does not parse as
  an app version, so while a plugin release was "latest", every app user would
  silently stop being offered real app updates. Plugin versions get a plain git
  tag, and their notes go in `claude-code/clawdmeter/CHANGELOG.md`.
- Check it with `claude plugin validate claude-code/clawdmeter` and
  `claude plugin test claude-code/clawdmeter` (needs the `claude` CLI).
- After changing which animations it plays, or the sprites themselves, run
  `tools/build_band_sprites.py`; `tests/test_band_sprites.py` fails while the
  generated file is stale.

**Releasing the plugin.** Claude Code installs a plugin from this repo's default
branch, so a plugin release is the merge that puts it on `main`:

1. Work lands on `develop`, as for the app.
2. Review the plugin's changes since its last tag, bump `version` in
   `claude-code/clawdmeter/.claude-plugin/plugin.json`, and add the version to
   `claude-code/clawdmeter/CHANGELOG.md`.
3. Merge `develop` into `main` by PR. Anything else waiting on `develop` ships to
   `main` with it, but app users only get app changes through an app release.
4. Tag the merge commit `claude-plugin-vX.Y.Z` and push the tag.
5. Check the public install on a clean Claude Code profile:
   `claude plugin marketplace add weltern/Clawdmeter`, then
   `claude plugin install clawdmeter@clawdmeter`; confirm the version and that
   the band loads.

Users update with `claude plugin update clawdmeter@clawdmeter`.

**Cross-platform (Linux/macOS):** this is intentionally a Windows-focused app,
and that's the current scope. The UI is Qt (PySide6) so a port isn't far-fetched,
but several pieces are Windows-specific (tray and Start-menu integration, the
PyInstaller build, some font choices) and I'm not set up to test other platforms.
If you're interested, open an issue to discuss before starting — I'm open to it in
principle, but it can't come at the expense of the Windows experience, and a port
would realistically need someone willing to help maintain it.
