"""OAuth access-token refresh for Clawdmeter  (BETA).

Claude Code OAuth access tokens live ~8 hours. When one expires the usage API
returns 401 and the dashboard goes blank. This module refreshes the access
token using the stored refresh token and writes the rotated tokens back into
the credentials file (the same `~/.claude/.credentials.json` Claude Code uses).

SAFETY (the failsafe):
  * A refresh is only attempted when the token is actually expired (+ a small
    skew), so we never hammer the endpoint.
  * Before writing, the current credentials are copied to a `.clawdmeter-bak`
    backup; it is deleted once the write is verified good (so the old plaintext
    tokens don't linger on disk) and kept only if the write/revert failed.
  * The write is atomic: temp file + os.replace (no half-written file).
  * After writing we re-read and validate; if anything is wrong we restore the
    backup automatically.
  * The OAuth token endpoint throttles hard (HTTP 429) — callers must back off.

Limitation: revert restores the *file*. A refresh that already succeeded has
rotated the token server-side, so revert protects file integrity, not the
server rotation. The backup + `claude /login` remain the ultimate recovery.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import macos_keychain

OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
# Public Claude Code OAuth client id (the same value Claude Code itself uses).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_HEADERS = {"Content-Type": "application/json", "User-Agent": "anthropic"}

EXPIRY_SKEW_SECONDS = 120  # treat the token as expired this many seconds early
# How far AHEAD of expiry the poller refreshes. Deliberately much larger than
# the skew above: that one answers "is it broken now", this one answers "renew
# it while we still can". 30 minutes leaves room for a throttled endpoint to
# be retried several times before anything the user can see breaks.
REFRESH_LEAD_SECONDS = 1800
BACKUP_SUFFIX = ".clawdmeter-bak"


class RefreshOutcome(str, Enum):
    """WHY a refresh ended the way it did — the bare ok/not-ok is not enough.

    The distinction that matters is THROTTLED vs REJECTED. Both look like "the
    refresh failed", but only one can ever succeed on a retry: a rejected
    refresh token is dead, and retrying it forever both never works and keeps
    feeding the endpoint's rate limiter. Callers key their backoff off this.
    """

    REFRESHED = "refreshed"
    THROTTLED = "throttled"      # 429 — the endpoint is rate-limiting us
    REJECTED = "rejected"        # 400/401 — the refresh token itself is dead
    UNREACHABLE = "unreachable"  # transport: timeout, DNS, connection refused
    ERROR = "error"              # anything else, including local file problems


@dataclass
class RefreshResult:
    ok: bool
    status: str                    # human-readable, for the UI
    http_status: int | None = None
    new_expiry_ms: int | None = None
    reverted: bool = False
    # Last so every existing positional construction still works.
    outcome: RefreshOutcome = RefreshOutcome.ERROR


def _oauth_block(data: dict) -> dict | None:
    """Return the dict holding the Claude Code accessToken/refreshToken."""
    if isinstance(data.get("claudeAiOauth"), dict):
        return data["claudeAiOauth"]
    if isinstance(data.get("refreshToken"), str):
        return data
    for v in data.values():
        if isinstance(v, dict) and isinstance(v.get("refreshToken"), str):
            return v
    return None


def _macos_keychain_active() -> bool:
    """True when credentials come from the macOS Keychain, not a file.

    An explicit CLAUDE_CREDENTIALS_PATH override always means a real file, even
    on macOS, so it opts back into the file path.
    """
    return macos_keychain.is_macos() and not os.environ.get("CLAUDE_CREDENTIALS_PATH")


def _read_credentials_raw(path: Path, *, blocking: bool = True) -> str | None:
    """Raw credentials JSON — from the macOS Keychain, else the file at ``path``.

    Read-only: this never writes. Used by the expiry helpers so the Settings
    token-status line shows the real expiry on macOS too.

    ``blocking=False`` is for callers on the UI thread. The macOS Keychain read
    can hang indefinitely on an authorisation dialog, so those callers take the
    last blob a worker cached and accept None until the poller has run once. It
    is a keyword argument with a safe default so a new caller has to opt into
    the blocking behaviour deliberately.
    """
    if _macos_keychain_active():
        blob = (macos_keychain.read_credentials() if blocking
                else macos_keychain.cached_credentials())
        if blob is not None:
            return blob
        # Fall through to the file on the off chance one exists.
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def token_expiry_ms(path: Path, *, blocking: bool = True) -> int | None:
    raw = _read_credentials_raw(path, blocking=blocking)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    blk = _oauth_block(data)
    if blk and isinstance(blk.get("expiresAt"), (int, float)):
        return int(blk["expiresAt"])
    return None


def _seconds_until_expiry(path: Path, *, now: float | None = None,
                          blocking: bool = True) -> float | None:
    """Seconds left on the stored token, or None when the expiry is unknown.

    ``now`` is injectable so boundary tests can pin the clock. Reading the clock
    twice around a comparison is how a test that sits exactly on the boundary
    fails one run in twenty-five.

    ``blocking`` is passed straight through to token_expiry_ms, so a UI-thread
    caller can refuse to wait on the macOS Keychain. See is_expired().
    """
    exp = token_expiry_ms(path, blocking=blocking)
    if exp is None:
        return None
    return exp / 1000.0 - (time.time() if now is None else now)


def is_expired(path: Path, skew_seconds: int = EXPIRY_SKEW_SECONDS,
               *, now: float | None = None, blocking: bool = True) -> bool:
    """Is the token dead RIGHT NOW (within a small skew)?

    This answers the Settings question — "is the thing currently broken" — and
    must not be widened, or the panel starts calling a healthy token expired.
    The poller asks a different question; see needs_refresh().

    Pass ``blocking=False`` from the UI thread. The macOS Keychain read this
    reaches can hang indefinitely on an authorisation dialog, and this function
    is called while SettingsPanel is being constructed — the case that once hung
    the app before it drew anything. Non-blocking returns None until the poller
    has cached a read on its worker, which reads here as "not expired": the
    conservative answer, since it only leaves a remedy button disabled for a
    moment rather than offering one for a token that is fine.
    """
    left = _seconds_until_expiry(path, now=now, blocking=blocking)
    if left is None:
        return False  # unknown expiry -> don't trigger a refresh
    return left <= skew_seconds


def needs_refresh(path: Path, lead_seconds: int = REFRESH_LEAD_SECONDS,
                  *, now: float | None = None) -> bool:
    """Should the poller refresh YET? True well BEFORE the token dies.

    Refreshing only once the token had already expired left a window where
    every poll 401'd and recovery depended on the token endpoint answering
    right then — an endpoint that throttles hard. Refreshing early costs
    nothing extra: it is the same one refresh per token lifetime, just taken
    while there is still a working token to fall back on.
    """
    left = _seconds_until_expiry(path, now=now)
    if left is None:
        return False
    return left <= lead_seconds


def _backup_path(path: Path) -> Path:
    return Path(str(path) + BACKUP_SUFFIX)


def _atomic_write(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, str(path))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _write_tokens_safely(path: Path, original_raw: str, data: dict,
                         expected_access: str) -> RefreshResult:
    """Backup -> atomic write -> verify -> revert on any failure."""
    backup = _backup_path(path)
    try:
        backup.write_text(original_raw, encoding="utf-8")
    except OSError as exc:
        return RefreshResult(False, f"Could not write backup, aborting: {exc}", 200)

    try:
        _atomic_write(path, data)
        check = json.loads(path.read_text(encoding="utf-8"))
        blk = _oauth_block(check)
        if not blk or blk.get("accessToken") != expected_access:
            raise ValueError("post-write verification mismatch")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        try:
            path.write_text(original_raw, encoding="utf-8")
            return RefreshResult(
                False, f"Write failed, reverted from backup ({exc})", 200, reverted=True
            )
        except OSError as exc2:
            return RefreshResult(
                False, f"Write failed AND revert failed ({exc2}) — backup at {backup}", 200
            )
    # Verified-good write — drop the plaintext backup of the now-stale old tokens.
    try:
        backup.unlink()
    except OSError:
        pass
    return RefreshResult(True, "Token refreshed", 200,
                         outcome=RefreshOutcome.REFRESHED)


def refresh(path: Path, *, timeout: float = 20.0) -> RefreshResult:
    """Refresh the access token in `path`. Safe: backs up + reverts on failure."""
    # macOS: the token lives in the login Keychain and writing the rotated token
    # back there isn't implemented yet (deliberate — a Keychain write mutates the
    # user's real Claude Code auth). Re-authenticating in Claude Code updates the
    # Keychain and Clawdmeter re-reads it on the next poll, so guide the user
    # there instead of failing on a credentials file that doesn't exist on Mac.
    if _macos_keychain_active():
        return RefreshResult(
            False,
            "Token auto-refresh isn't supported on macOS yet — run `claude` to "
            "re-authenticate. Clawdmeter reads the refreshed token from the "
            "Keychain automatically.",
            None,
        )
    try:
        original_raw = path.read_text(encoding="utf-8")
        data = json.loads(original_raw)
    except (OSError, json.JSONDecodeError) as exc:
        return RefreshResult(False, f"Cannot read credentials: {exc}")

    blk = _oauth_block(data)
    if not blk or not isinstance(blk.get("refreshToken"), str):
        # Nothing to refresh WITH. Only signing in again produces one, so this
        # is the same dead end as a server-side rejection, not a retryable slip.
        return RefreshResult(False, "No refresh token found in credentials",
                             outcome=RefreshOutcome.REJECTED)
    refresh_tok = blk["refreshToken"]

    # httpx imported lazily so this module stays importable/testable without it.
    import httpx

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": CLIENT_ID,
    }
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = http.post(OAUTH_TOKEN_URL, headers=REFRESH_HEADERS, json=body)
    except httpx.HTTPError as exc:
        return RefreshResult(False, f"Refresh request failed: {exc}",
                             outcome=RefreshOutcome.UNREACHABLE)

    if resp.status_code == 429:
        return RefreshResult(False, "Rate limited by token endpoint — backing off", 429,
                             outcome=RefreshOutcome.THROTTLED)
    if resp.status_code in (400, 401):
        # The server read the refresh token and refused it: expired, or already
        # rotated by whoever refreshed last. No amount of retrying fixes that.
        return RefreshResult(
            False, f"Refresh token rejected (HTTP {resp.status_code}) — sign in again",
            resp.status_code, outcome=RefreshOutcome.REJECTED,
        )
    if resp.status_code != 200:
        return RefreshResult(
            False, f"Refresh failed (HTTP {resp.status_code})",
            resp.status_code, outcome=RefreshOutcome.ERROR,
        )

    try:
        tok = resp.json()
        new_access = tok["access_token"]
        expires_in = int(tok["expires_in"])
    except (ValueError, KeyError, TypeError) as exc:
        return RefreshResult(False, f"Unexpected refresh response: {exc}", 200)

    blk["accessToken"] = new_access
    blk["refreshToken"] = tok.get("refresh_token") or refresh_tok
    new_expiry_ms = int(time.time() * 1000) + expires_in * 1000
    blk["expiresAt"] = new_expiry_ms

    result = _write_tokens_safely(path, original_raw, data, new_access)
    if result.ok:
        result.new_expiry_ms = new_expiry_ms
    return result
