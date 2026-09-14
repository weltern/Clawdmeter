"""Claude usage polling for Clawdmeter.

Ported from HermannBjorgvin/Clawdmeter daemon. The BLE/asyncio plumbing is
gone; this is a QThread that posts UsageSample objects via a Qt signal.

Token resolution order:
  1. CLAUDE_CREDENTIALS_PATH env var (explicit override — always a file)
  2. macOS only: the login Keychain (where Claude Code stores the token on Mac,
     instead of a credentials file)
  3. ~/.claude/.credentials.json (Claude Code default on Windows/Linux, and a
     fallback on macOS if a file happens to exist)
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PySide6.QtCore import QThread, Signal

import app_settings
import macos_keychain
import token_refresh
from scoped_windows import ScopedWindow, windows_from_limits
from transcript import account_window_tokens

API_URL = "https://api.anthropic.com/v1/messages"
# OAuth usage/profile endpoints (the desktop Settings -> Usage page uses these).
# Same auth + anthropic-beta header as the messages probe.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

DEFAULT_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
POLL_INTERVAL_SECONDS = 60


@dataclass
class UsageSample:
    """One snapshot of Claude rate-limit state. Mirrors the BLE payload."""

    # session_pct / weekly_pct are the 5h / 7d window utilisation as a percent.
    # They are NOT clamped at 100: once a window is maxed and you keep working on
    # paid extra usage, the percent climbs past 100 (e.g. 120 = 20% into overage)
    # — that overflow is the overage signal, surfaced per-window in the UI.
    session_pct: int
    session_reset_minutes: int
    weekly_pct: int
    weekly_reset_minutes: int
    status: str
    ok: bool
    error: str | None = None
    timestamp: float = 0.0
    # Account-wide input+output token totals over the 5h / 7d windows, summed
    # from the local transcripts (0 when the token-usage display is off).
    tokens_5h: int = 0
    tokens_7d: int = 0
    # K1: OAuth usage/profile endpoint data. Defaults apply when those endpoints
    # weren't reached (the header-derived fields above still populate).
    plan_tier: str | None = None            # e.g. "default_claude_max_5x"
    extra_usage_enabled: bool = False
    extra_usage_used_usd: float = 0.0       # real extra-usage spend, in dollars
    extra_usage_limit_usd: float | None = None  # monthly cap in $, None = uncapped
    # Scoped limits beside the 5h/7d windows (e.g. Weekly · Fable). None means
    # the usage request didn't succeed this poll — NOT "no scoped limits",
    # which is an empty list — so the dashboard can keep the last-known ones.
    scoped_windows: list[ScopedWindow] | None = None


def credentials_path() -> Path:
    override = os.environ.get("CLAUDE_CREDENTIALS_PATH")
    return Path(override) if override else DEFAULT_CREDENTIALS_PATH


def token_source_description() -> str:
    """Human-readable description of where the token is being read from.

    Used for the Settings "credentials" line and the no-token error, so both
    tell the truth on macOS (the Keychain) instead of naming a file that isn't
    where the token actually lives.
    """
    override = os.environ.get("CLAUDE_CREDENTIALS_PATH")
    if override:
        return override
    if macos_keychain.is_macos():
        return f"the macOS login Keychain (service \"{macos_keychain.service_name()}\")"
    return str(DEFAULT_CREDENTIALS_PATH)


def _extract_access_token(blob: str) -> str | None:
    """Pull accessToken from a credentials blob — JSON, nested JSON, or raw."""
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def read_token() -> str | None:
    # An explicit CLAUDE_CREDENTIALS_PATH override always points at a file and
    # wins on every platform.
    override = os.environ.get("CLAUDE_CREDENTIALS_PATH")
    # macOS keeps the token in the login Keychain, not a file. Try it before the
    # default file path (which normally doesn't exist on Mac).
    if macos_keychain.is_macos() and not override:
        blob = macos_keychain.read_credentials()
        if blob:
            token = _extract_access_token(blob)
            if token:
                return token
        # Fall through to the file path in the rare case a credentials file also
        # exists on this Mac.
    path = credentials_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return _extract_access_token(raw)


def sample_from_headers(headers, now: float) -> UsageSample:
    """Build a UsageSample from the API's rate-limit response headers.

    Pure (no network) so it's unit-testable. ``headers`` is any mapping with a
    ``.get(name, default)`` method (httpx ``Headers`` or a plain dict).
    """
    def hdr(name: str, default: str = "0") -> str:
        return headers.get(name, default)

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except (TypeError, ValueError):
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        # Fraction (0.0..) -> percent. Deliberately NOT clamped: a window in
        # overage reports utilisation > 1.0, which we want to surface as >100%.
        try:
            return int(round(float(util) * 100))
        except (TypeError, ValueError):
            return 0

    # Overage is derived per-window from the 5h / 7d utilisation crossing 100%
    # (not the separate unified-overage-* bucket, which tracks the extra-usage
    # *credit cap* — null/0 for accounts with uncapped extra usage, so it never
    # reflects an actual session/weekly overage).
    return UsageSample(
        session_pct=pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
        session_reset_minutes=reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
        weekly_pct=pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
        weekly_reset_minutes=reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
        status=hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
        ok=True,
        error=None,
        timestamp=now,
    )


def usage_fields_from_json(usage: dict | None, profile: dict | None) -> dict:
    """Extract the K1 fields from /api/oauth/usage + /api/oauth/profile JSON.

    Pure (no network) and defensive: every field falls back to a sane default on
    missing/null input, so a partial or changed response can't crash a poll.
    Money comes from `spend.used` (amount_minor / 10**exponent) — never read the
    minor-unit integer as dollars. Scoped windows come from the `limits[]`
    array's scoped entries (see scoped_windows.windows_from_limits).
    """
    usage = usage or {}
    profile = profile or {}
    org = profile.get("organization") or {}

    spend = usage.get("spend") or {}
    used = spend.get("used") or {}
    exp = used.get("exponent", 2) if isinstance(used.get("exponent"), int) else 2
    amt = used.get("amount_minor")
    used_usd = amt / (10 ** exp) if isinstance(amt, (int, float)) else 0.0

    raw_limit = spend.get("limit")
    if isinstance(raw_limit, dict):
        la, le = raw_limit.get("amount_minor"), raw_limit.get("exponent", exp)
        limit_usd = la / (10 ** le) if isinstance(la, (int, float)) else None
    elif isinstance(raw_limit, (int, float)):
        limit_usd = raw_limit / (10 ** exp)
    else:
        limit_usd = None

    return {
        "plan_tier": org.get("rate_limit_tier"),
        "extra_usage_enabled": bool(spend.get("enabled")),
        "extra_usage_used_usd": round(float(used_usd), 2),
        "extra_usage_limit_usd": round(float(limit_usd), 2) if limit_usd is not None else None,
        "scoped_windows": windows_from_limits(usage.get("limits")),
    }


def _poll_once(token: str) -> UsageSample:
    """Make one rate-limit probe. Returns a sample with ok=False on failure."""
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    now = time.time()
    try:
        with httpx.Client(timeout=20.0) as http:
            resp = http.post(API_URL, headers=headers, json=API_BODY)
            # A non-2xx has no rate-limit headers; without this, sample_from_headers()
            # misreads that as a genuine 0% (it always returns ok=True) -> false reset alert.
            resp.raise_for_status()
            sample = sample_from_headers(resp.headers, now)
            # K1: enrich with the OAuth usage + profile endpoints (plan tier,
            # extra-usage spend, per-model windows) on the same client/cadence.
            # Non-fatal: any failure leaves the header-derived sample intact.
            # raise_for_status on usage only: its error body parses as JSON too,
            # and would read as "no scoped windows" rather than "didn't find
            # out". A profile error body just leaves the plan tier unknown.
            try:
                usage = http.get(USAGE_URL, headers=headers).raise_for_status().json()
                profile = http.get(PROFILE_URL, headers=headers).json()
                for k, v in usage_fields_from_json(usage, profile).items():
                    setattr(sample, k, v)
            except (httpx.HTTPError, ValueError, TypeError):
                pass
    except httpx.HTTPError as exc:
        return UsageSample(0, 0, 0, 0, "error", False, str(exc), now)
    # Sum the local transcripts' input+output over the 5h/7d windows — only when
    # the token display is on, so we don't scan files for nothing.
    if app_settings.get_show_token_usage():
        try:
            sample.tokens_5h, sample.tokens_7d = account_window_tokens(now)
        except OSError:
            pass
    return sample


class UsagePoller(QThread):
    """Background polling thread. Emits sample(UsageSample) on every poll.

    Also keeps the OAuth access token fresh (BETA): when the stored token is
    expired it refreshes it via `token_refresh` before polling, so the
    dashboard doesn't go blank every ~8h. A manual refresh can be requested
    with request_manual_refresh(); outcomes are reported on `refresh_status`.
    All refresh work runs on this thread (never the GUI thread).
    """

    sample = Signal(UsageSample)
    refresh_status = Signal(object)  # token_refresh.RefreshResult

    REFRESH_COOLDOWN_MIN = 60.0    # seconds between auto attempts
    REFRESH_COOLDOWN_MAX = 900.0   # backoff ceiling after a 429

    def __init__(self, interval_seconds: int = POLL_INTERVAL_SECONDS, parent=None) -> None:
        super().__init__(parent)
        self._interval = interval_seconds
        self._stop = False
        self._wake = False
        self._auto_refresh = app_settings.get_auto_refresh()
        self._manual_refresh = False
        self._last_refresh_attempt = 0.0
        self._cooldown = self.REFRESH_COOLDOWN_MIN

    def stop(self) -> None:
        self._stop = True

    def wake(self) -> None:
        """Cut the current sleep short and poll now. Used to snap back to a fresh
        sample after the idle back-off has slowed the cadence and activity
        resumes, instead of waiting out the long idle sleep."""
        self._wake = True

    def set_auto_refresh(self, on: bool) -> None:
        self._auto_refresh = bool(on)

    def set_interval(self, seconds: int) -> None:
        """Change the poll cadence. Takes effect on the next cycle: the current
        sleep already snapshotted the old interval via range(), so a mid-sleep
        change won't wake it early (instant apply would need a wake flag like
        _manual_refresh — deferred for now)."""
        self._interval = max(1, int(seconds))

    def request_manual_refresh(self) -> None:
        """Ask the poll thread to refresh the token ASAP (bypasses cooldown)."""
        self._manual_refresh = True

    def _do_refresh(self, manual: bool) -> None:
        self._last_refresh_attempt = time.time()
        result = token_refresh.refresh(credentials_path())
        if result.ok:
            self._cooldown = self.REFRESH_COOLDOWN_MIN
        elif result.http_status == 429 and not manual:
            self._cooldown = min(self._cooldown * 2, self.REFRESH_COOLDOWN_MAX)
        self.refresh_status.emit(result)

    def _maybe_auto_refresh(self) -> None:
        if not self._auto_refresh:
            return
        # macOS has no Keychain write-back path yet, so there's nothing to auto
        # refresh into — re-authing in Claude Code updates the Keychain and the
        # next poll re-reads it. Skip here so we don't emit a refresh failure on
        # every cooldown while a token is expired. (An explicit file override on
        # Mac still takes the normal file path.)
        if macos_keychain.is_macos() and not os.environ.get("CLAUDE_CREDENTIALS_PATH"):
            return
        if not token_refresh.is_expired(credentials_path()):
            return
        if time.time() - self._last_refresh_attempt < self._cooldown:
            return
        self._do_refresh(manual=False)

    def run(self) -> None:  # QThread entry
        while not self._stop:
            self._wake = False  # cleared each cycle; a wake during this poll re-sets it
            if self._manual_refresh:
                self._manual_refresh = False
                self._do_refresh(manual=True)
            self._maybe_auto_refresh()

            token = read_token()
            if not token:
                self.sample.emit(UsageSample(
                    0, 0, 0, 0, "no-token", False,
                    f"No token in {token_source_description()}", time.time(),
                ))
            else:
                self.sample.emit(_poll_once(token))

            for _ in range(self._interval):
                if self._stop:
                    return
                if self._manual_refresh or self._wake:
                    break  # service the request promptly at the top of the loop
                self.msleep(1000)
