"""The Claude Code band's sprite module is generated from assets/sprites and must match it."""
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _builder():
    spec = importlib.util.spec_from_file_location("build_band_sprites", ROOT / "tools" / "build_band_sprites.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_committed_sprites_match_assets():
    mod = _builder()
    committed = mod.OUT.read_text(encoding="utf-8")
    assert committed == mod.build(), "claude-code/clawd-band/hooks/sprites.ts is stale: run tools/build_band_sprites.py"


def test_band_plays_only_animations_the_app_ships():
    mod = _builder()
    clawd_ts = (ROOT / "claude-code" / "clawd-band" / "hooks" / "clawd.ts").read_text(encoding="utf-8")
    import json
    manifest = json.loads((ROOT / "assets" / "sprites" / "manifest.json").read_text(encoding="utf-8"))
    slugs = {manifest["animations"][name]["slug"] for name in mod.ANIMATIONS}
    for slug in ("work_coding", "work_think", "idle_look_around", "idle_blink", "expression_surprise", "idle_breathe"):
        assert f"'{slug}'" in clawd_ts, f"clawd.ts no longer plays {slug}"
        assert slug in slugs, f"{slug} is played by the band but not generated"
