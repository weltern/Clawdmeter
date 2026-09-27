"""Tests for the K1 OAuth usage/profile parsing (poller.usage_fields_from_json).

Pure parsing — no network, no Qt. Fixtures mirror the real /api/oauth/usage and
/api/oauth/profile shapes (anonymized; no account PII).

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from poller import usage_fields_from_json  # noqa: E402

USAGE = {
    "five_hour": {"utilization": 26.0, "resets_at": "2026-06-17T05:59:59+00:00"},
    "seven_day": {"utilization": 33.0, "resets_at": "2026-06-18T20:59:59+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 0.0, "resets_at": None},
    "extra_usage": {
        "is_enabled": True, "monthly_limit": None, "used_credits": 1793.0,
        "utilization": None, "currency": "USD", "decimal_places": 2,
    },
    "limits": [
        {"kind": "session", "group": "session", "percent": 26, "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 33, "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 12,
         "scope": {"model": {"id": None, "display_name": "Sonnet"}}},
    ],
    "spend": {
        "used": {"amount_minor": 1793, "currency": "USD", "exponent": 2},
        "limit": None, "percent": 0, "severity": "normal", "enabled": True,
    },
}

PROFILE = {
    "account": {"has_claude_max": True, "has_claude_pro": False},
    "organization": {
        "organization_type": "claude_max",
        "rate_limit_tier": "default_claude_max_5x",
        "has_extra_usage_enabled": True,
        "subscription_status": "active",
    },
}


def test_full_response():
    f = usage_fields_from_json(USAGE, PROFILE)
    assert f["plan_tier"] == "default_claude_max_5x"
    assert f["extra_usage_enabled"] is True
    assert f["extra_usage_used_usd"] == 17.93     # amount_minor 1793 / 10**2 — NOT 1793
    assert f["extra_usage_limit_usd"] is None      # uncapped
    assert [(w.label, w.pct) for w in f["scoped_windows"]] == [("Weekly · Sonnet", 12)]


def test_empty_and_none_are_safe():
    for usage, profile in (({}, {}), (None, None)):
        f = usage_fields_from_json(usage, profile)
        assert f["plan_tier"] is None
        assert f["extra_usage_enabled"] is False
        assert f["extra_usage_used_usd"] == 0.0
        assert f["extra_usage_limit_usd"] is None
        # A parsed response with no limits is "none", not "unknown" (None).
        assert f["scoped_windows"] == []


def test_missing_spend_defaults_to_zero():
    f = usage_fields_from_json({"limits": []}, PROFILE)
    assert f["extra_usage_used_usd"] == 0.0
    assert f["plan_tier"] == "default_claude_max_5x"


def test_limit_as_minor_object():
    usage = {"spend": {"used": {"amount_minor": 500, "exponent": 2}, "enabled": True,
                       "limit": {"amount_minor": 5000, "exponent": 2}}}
    f = usage_fields_from_json(usage, {})
    assert f["extra_usage_used_usd"] == 5.0
    assert f["extra_usage_limit_usd"] == 50.0


def test_limit_as_plain_number():
    usage = {"spend": {"used": {"amount_minor": 500, "exponent": 2}, "limit": 5000}}
    f = usage_fields_from_json(usage, {})
    assert f["extra_usage_limit_usd"] == 50.0


def test_model_window_skips_unscoped_and_nonnumeric():
    usage = {"limits": [
        {"percent": 50, "scope": None},
        {"percent": None, "scope": {"model": {"display_name": "Opus"}}},
        {"percent": 7, "scope": {"model": {"display_name": "Haiku"}}},
    ]}
    windows = usage_fields_from_json(usage, {})["scoped_windows"]
    assert [(w.name, w.pct) for w in windows] == [("Haiku", 7)]


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
