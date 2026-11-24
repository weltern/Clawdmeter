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

# Status values for a poll that FAILED. The success path carries Anthropic's own
# `anthropic-ratelimit-unified-5h-status` instead ("allowed", "allowed_warning",
# "rejecting", ...), so these are deliberately distinct from those words — the
# dashboard matches the rate-limit ones by substring.
#
# These exist because every failure used to collapse into a bare "error", which
# the dashboard badge had no branch for and therefore rendered as *nothing*: an
# expired token looked identical to a dashboard that had quietly stopped
# updating. The failure kind is the whole difference between "re-authenticate"
# and "wait for your network to come back".
STATUS_NO_TOKEN = "no-token"
STATUS_AUTH_EXPIRED = "auth-expired"
STATUS_HTTP_ERROR = "http-error"
STATUS_OFFLINE = "offline"
# Expired AND the refresh token was refused, so no automatic recovery exists.
# Split from STATUS_AUTH_EXPIRED because the remedy is different: that one
# resolves itself on the next refresh, this one needs the user to sign in.
STATUS_REAUTH_NEEDED = "reauth-needed"


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
    # Claude's own explanation when it refused the probe, e.g. "OAuth access
    # token has been revoked." Kept apart from `error` (which also carries the
    # status line) so the UI can quote it verbatim. None when Claude gave none.
    reason: str | None = None
    # Epoch seconds until which the usage/profile details are paused because
    # that endpoint answered 429. None = not paused. The fields above then
    # hold the last values that endpoint DID return.
    details_paused_until: float | None = None


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


# How often the OAuth usage / profile endpoints are asked, at most. The probe
# above runs every poll (as often as every 10 s); these two used to ride along
# on every one, so they were two thirds of the app's API traffic -- and the
# usage endpoint rate-limits on its own, with Retry-After of up to an hour. Its
# data (extra-usage spend, scoped windows) moves slowly, and the profile only
# carries the plan tier, which practically never changes.
USAGE_DETAILS_INTERVAL = 300.0
PROFILE_INTERVAL = 3600.0
# After a failure that is not a 429: retry soon, but not on every poll.
DETAILS_RETRY_AFTER_ERROR = 60.0
# A 429 with no usable Retry-After: back off this long, doubling per repeat.
DETAILS_429_DEFAULT = 300.0
# Never trust a Retry-After beyond this; a garbled header must not silence
# the details for days.
DETAILS_429_MAX = 6 * 3600.0
_REASON_MAX_CHARS = 200


def api_error_reason(response) -> str | None:
    """Claude's own message from an error response, or None.

    Anthropic error bodies are ``{"type": "error", "error": {"type": ...,
    "message": "OAuth access token has been revoked."}}``. That message is
    the only place a revoked token is told apart from an expired one -- both
    are a 401 -- so it is worth showing rather than discarding. Defensive: any
    other body shape (HTML from a proxy, empty, non-JSON) gives None.
    """
    try:
        body = response.json()
    except (ValueError, TypeError):
        return None
    err = body.get("error") if isinstance(body, dict) else None
    msg = err.get("message") if isinstance(err, dict) else None
    if not isinstance(msg, str):
        return None
    msg = " ".join(msg.split())
    if not msg:
        return None
    if len(msg) > _REASON_MAX_CHARS:
        msg = msg[:_REASON_MAX_CHARS - 1].rstrip() + "\u2026"
    return msg


def retry_after_seconds(value, now: float) -> float | None:
    """Parse a Retry-After header (delta-seconds or an HTTP date) to seconds
    from ``now``. None when missing or unparseable."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - now)


class UsageDetails:
    """Throttle and cache for the OAuth usage + profile requests.

    Owned by the poll thread and handed to every _poll_once, so the slow-moving
    details are fetched every few minutes instead of every poll, a 429 is
    honoured for as long as its Retry-After says, and in between the last
    values that WERE returned are re-applied (a quiet hold, not a blank).

    Not thread-safe; only the poll thread touches it.
    """

    def __init__(self) -> None:
        self._usage: dict | None = None
        self._profile: dict | None = None
        self._usage_due = 0.0
        self._profile_due = 0.0
        self._paused_until: float | None = None
        self._backoff = DETAILS_429_DEFAULT
        self._token: str | None = None

    @property
    def paused_until(self) -> float | None:
        return self._paused_until

    def _rate_limited(self, resp, now: float) -> None:
        wait = retry_after_seconds(resp.headers.get("retry-after"), now)
        if wait is None:
            wait = self._backoff
            self._backoff = min(self._backoff * 2, DETAILS_429_MAX)
        wait = min(max(wait, USAGE_DETAILS_INTERVAL), DETAILS_429_MAX)
        self._paused_until = now + wait
        self._usage_due = self._profile_due = self._paused_until

    def fetch(self, http, headers: dict, token: str, now: float) -> None:
        """Ask whichever endpoint is due. Never raises for HTTP trouble."""
        if token != self._token:
            # A new token (a refresh, or a sign-in -- possibly to another
            # account): ask again now rather than wait out the interval, unless
            # Claude has told us to wait. The cache is kept, not dropped, so a
            # pause doesn't blank the plan tier and spend on every refresh.
            self._token = token
            if self._paused_until is None or now >= self._paused_until:
                self._usage_due = self._profile_due = 0.0
        if self._paused_until is not None and now >= self._paused_until:
            self._paused_until = None
        if self._paused_until is not None:
            return
        if now >= self._usage_due:
            try:
                resp = http.get(USAGE_URL, headers=headers)
                if resp.status_code == 429:
                    self._rate_limited(resp, now)
                    return
                # raise_for_status: an error body parses as JSON too, and would
                # read as "no scoped windows" rather than "didn't find out".
                body = resp.raise_for_status().json()
                if not isinstance(body, dict):
                    raise ValueError("usage body is not an object")
                self._usage = body
                self._usage_due = now + USAGE_DETAILS_INTERVAL
                self._backoff = DETAILS_429_DEFAULT
            except (httpx.HTTPError, ValueError, TypeError):
                self._usage_due = now + DETAILS_RETRY_AFTER_ERROR
        if now >= self._profile_due:
            try:
                resp = http.get(PROFILE_URL, headers=headers)
                if resp.status_code == 429:
                    self._rate_limited(resp, now)
                    return
                body = resp.raise_for_status().json()
                if isinstance(body, dict):
                    self._profile = body
                self._profile_due = now + PROFILE_INTERVAL
            except (httpx.HTTPError, ValueError, TypeError):
                # Only the plan tier comes from here; a failure must not cost
                # the usage fields, and is retried at the usage cadence.
                self._profile_due = now + USAGE_DETAILS_INTERVAL

    def apply(self, sample: UsageSample) -> None:
        """Copy the last-known details onto a sample. With no usage body yet,
        scoped_windows stays None ("didn't find out"), never an empty list."""
        if self._usage is None and self._profile is None:
            sample.details_paused_until = self._paused_until
            return
        fields = usage_fields_from_json(self._usage, self._profile)
        if self._usage is None:
            fields = {"plan_tier": fields["plan_tier"]}
        for k, v in fields.items():
            setattr(sample, k, v)
        sample.details_paused_until = self._paused_until


def _poll_once(token: str, details: UsageDetails | None = None) -> UsageSample:
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
            # extra-usage spend, per-model windows). Throttled and cached by
            # UsageDetails; a caller without one (tests, one-off probes) gets a
            # fresh one, which simply asks both endpoints now. Non-fatal: any
            # failure leaves the header-derived sample intact.
            if details is None:
                details = UsageDetails()
            details.fetch(http, headers, token, now)
            details.apply(sample)
    except httpx.HTTPStatusError as exc:
        # The server answered and refused. Caught before HTTPError below:
        # HTTPStatusError is a subclass of it.
        #
        # 401 and 403 are NOT the same thing and must not claim to be. 401 is
        # the token having expired, which a refresh fixes by itself. 403 is
        # forbidden — a revoked token, or an org policy — where asserting an
        # expiry would state something unestablished and send the user to fix
        # the wrong thing. "Sign in again" is true under either reading of a
        # 403, and it is the only remedy this app can offer for one.
        code = exc.response.status_code
        if code == 401:
            status = STATUS_AUTH_EXPIRED
        elif code == 403:
            status = STATUS_REAUTH_NEEDED
        else:
            status = STATUS_HTTP_ERROR
        reason = api_error_reason(exc.response)
        error = f"HTTP {code}: {reason}" if reason else str(exc)
        return UsageSample(0, 0, 0, 0, status, False, error, now, reason=reason)
    except httpx.HTTPError as exc:
        # Transport-level: timeout, DNS failure, connection refused. Nothing is
        # wrong with the token, so it must not read as an auth problem.
        return UsageSample(0, 0, 0, 0, STATUS_OFFLINE, False, str(exc), now)
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
    REFRESH_COOLDOWN_MAX = 900.0   # backoff ceiling for transient trouble
    # A 429 ceiling of 15 minutes means ~4 attempts an hour against an endpoint
    # that throttles hard — over a day and a half of a dead token that is well
    # over a hundred requests, which plausibly keeps its own lockout open.
    REFRESH_THROTTLED_MAX = 4 * 3600.0

    def __init__(self, interval_seconds: int = POLL_INTERVAL_SECONDS, parent=None) -> None:
        super().__init__(parent)
        self._interval = interval_seconds
        self._stop = False
        self._wake = False
        self._auto_refresh = app_settings.get_auto_refresh()
        self._manual_refresh = False
        self._last_refresh_attempt = 0.0
        self._cooldown = self.REFRESH_COOLDOWN_MIN
        # Whether a REJECTED refresh has switched automatic refreshes off: the
        # stored refresh token is dead and retrying it can only fail. Its own
        # boolean, NOT inferred from the expiry below, because that expiry is
        # legitimately None for a credentials file with no `expiresAt` — which
        # is exactly the malformed-file case that produces a REJECTED. Inferring
        # the state from a nullable value made the block silently not engage.
        self._refresh_blocked = False
        # The expiry the block was taken against (may be None when unknown).
        # A CHANGE here means somebody signed in again, which clears the block
        # without anything having to notify us.
        self._blocked_expiry_ms: int | None = None
        # The usage/profile throttle + cache, kept across polls.
        self._details = UsageDetails()

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

    def _do_refresh(self, manual: bool, seen_access: str | None = None) -> None:
        self._last_refresh_attempt = time.time()
        result = token_refresh.refresh(credentials_path(), seen_access=seen_access)
        outcome = result.outcome
        Outcome = token_refresh.RefreshOutcome

        if outcome is Outcome.BUSY and not manual:
            # Claude Code holds its refresh lock and is renewing this same file;
            # the result shows up on a later read. Not an error, no backoff.
            return
        if outcome is Outcome.REFRESHED:
            self._cooldown = self.REFRESH_COOLDOWN_MIN
            self._refresh_blocked = False
            self._blocked_expiry_ms = None
        elif outcome is Outcome.REJECTED:
            # Dead refresh token. Stop attempting: it cannot succeed, and each
            # try is another request against a rate limiter we may already be
            # behind. A manual attempt still gets through _do_refresh directly.
            self._refresh_blocked = True
            self._blocked_expiry_ms = token_refresh.token_expiry_ms(credentials_path())
        elif manual:
            pass                       # the user asked; don't punish them with backoff
        elif outcome is Outcome.THROTTLED:
            self._cooldown = min(self._cooldown * 2, self.REFRESH_THROTTLED_MAX)
        else:
            self._cooldown = min(self._cooldown * 2, self.REFRESH_COOLDOWN_MAX)

        self.refresh_status.emit(result)

    def _mark_reauth(self, sample: UsageSample) -> UsageSample:
        """Upgrade a plain expiry to "sign in again" when nothing can recover it.

        An expired token the next refresh will fix and one whose refresh token
        is dead look identical from the probe — both are a 401 — but only one
        of them needs the user to do something. The badge reads this field, so
        the distinction has to be made before the sample is emitted.

        Lives out here rather than inline in run() so it can be tested; the
        thread loop is not reachable from a test.
        """
        # Two ways there is no automatic recovery: the refresh token was
        # refused, or this install cannot refresh at all. The second is macOS,
        # where it is true of EVERY expiry — so the badge there said "Token
        # expired", implying a refresh that was never coming. That is the same
        # false promise the Settings line was fixed for, on the one platform
        # where it is always wrong.
        if sample.status == STATUS_AUTH_EXPIRED and (
                self._reauth_needed() or not token_refresh.auto_refresh_supported()):
            sample.status = STATUS_REAUTH_NEEDED
        return sample

    def _reauth_needed(self) -> bool:
        """True while a rejected refresh token is still the one on disk.

        Re-reading the expiry each time is what makes the block self-clearing:
        signing in again writes a new expiry, and the next poll resumes without
        anything having to notify us.
        """
        if not self._refresh_blocked:
            return False
        if token_refresh.token_expiry_ms(credentials_path()) != self._blocked_expiry_ms:
            # New credentials arrived. Note this is correct when BOTH are None
            # (None != None is False), so a file that never had an expiry stays
            # blocked rather than silently unblocking on every poll.
            self._refresh_blocked = False
            self._blocked_expiry_ms = None
            return False
        return True

    def _maybe_auto_refresh(self) -> None:
        if not self._auto_refresh:
            return
        # macOS has no Keychain write-back path yet, so there's nothing to auto
        # refresh into — re-authing in Claude Code updates the Keychain and the
        # next poll re-reads it. Skip here so we don't emit a refresh failure on
        # every cooldown while a token is expired. (An explicit file override on
        # Mac still takes the normal file path.)
        if not token_refresh.auto_refresh_supported():
            return
        if self._reauth_needed():
            return
        # needs_refresh, not is_expired: renew while there is still a working
        # token, so a throttled endpoint has room to be retried before the
        # dashboard can break.
        if not token_refresh.needs_refresh(credentials_path()):
            return
        if time.time() - self._last_refresh_attempt < self._cooldown:
            return
        self._do_refresh(manual=False)

    def _recover_from_401(self, used_token: str) -> UsageSample | None:
        """The API rejected a token its expiresAt called valid. Re-poll with a
        newer token if one is on disk, else refresh now (subject to the same
        cooldown as auto-refresh) and re-poll with its result. None = no luck;
        the caller keeps the failed sample."""
        fresh = read_token()
        if fresh and fresh != used_token:
            return _poll_once(fresh, self._details)
        if (not self._auto_refresh
                or not token_refresh.auto_refresh_supported()
                or self._reauth_needed()
                or time.time() - self._last_refresh_attempt < self._cooldown):
            return None
        self._do_refresh(manual=False, seen_access=used_token)
        fresh = read_token()
        if fresh and fresh != used_token:
            return _poll_once(fresh, self._details)
        return None

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
                    0, 0, 0, 0, STATUS_NO_TOKEN, False,
                    f"No token in {token_source_description()}", time.time(),
                ))
            else:
                sample = _poll_once(token, self._details)
                if sample.status == STATUS_AUTH_EXPIRED:
                    sample = self._recover_from_401(token) or sample
                self.sample.emit(self._mark_reauth(sample))

            for _ in range(self._interval):
                if self._stop:
                    return
                if self._manual_refresh or self._wake:
                    break  # service the request promptly at the top of the loop
                self.msleep(1000)
