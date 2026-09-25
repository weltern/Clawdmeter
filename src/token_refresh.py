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

Coexisting with Claude Code (why the desktop app changed things):
  * In a terminal, the `claude` CLI refreshes the token itself (5 min before
    expiry) and writes it back to this same file, so while you use the CLI the
    file is almost always fresh and Clawdmeter rarely has to refresh anything.
  * The Claude desktop app keeps its own login and hands Claude Code the token
    directly, so the CLI never touches the file. It goes stale after ~8h and
    Clawdmeter becomes the only thing refreshing it -- which is when the gaps
    showed up.
  * So refresh like the CLI does: early (EXPIRY_SKEW_SECONDS), against the
    current token endpoint, with the stored scopes, and under the CLI's own
    refresh lock (`~/.claude/.oauth_refresh.lock`). Refresh tokens rotate on
    use; two processes spending the same one at once leaves one of them with a
    dead token, so after taking the lock we re-read the file and stand down if
    someone else already refreshed it.
  * A refresh token the endpoint rejects (400/401) is remembered and not sent
    again, so a dead login shows "re-login needed" instead of retrying forever.

Limitation: revert restores the *file*. A refresh that already succeeded has
rotated the token server-side, so revert protects file integrity, not the
server rotation. The backup + `claude /login` remain the ultimate recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import macos_keychain

# Current Claude Code token endpoint first; the legacy console host is only a
# fallback for when the first can't be reached or doesn't route the request.
OAUTH_TOKEN_URLS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
OAUTH_TOKEN_URL = OAUTH_TOKEN_URLS[0]
# Public Claude Code OAuth client id (the same value Claude Code itself uses).
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_HEADERS = {"Content-Type": "application/json", "User-Agent": "anthropic"}

# Refresh this many seconds before expiry -- the same window Claude Code uses,
# so the dashboard never sees the token actually lapse.
EXPIRY_SKEW_SECONDS = 300
BACKUP_SUFFIX = ".clawdmeter-bak"

# Claude Code's refresh lock (a proper-lockfile directory lock) inside the
# credentials directory, plus the legacy `<dir>.lock` older CLIs used. A lock
# whose mtime is older than LOCK_STALE_SECONDS belongs to a dead holder.
LOCK_NAME = ".oauth_refresh.lock"
LOCK_STALE_SECONDS = 60

# Hashes of refresh tokens the endpoint rejected; never re-sent this run.
_dead_refresh_tokens: set[str] = set()


@dataclass
class RefreshResult:
    ok: bool
    status: str                    # human-readable, for the UI
    http_status: int | None = None
    new_expiry_ms: int | None = None
    reverted: bool = False
    busy: bool = False             # another process holds the refresh lock


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


def is_expired(path: Path, skew_seconds: int = EXPIRY_SKEW_SECONDS) -> bool:
    exp = token_expiry_ms(path)
    if exp is None:
        return False  # unknown expiry -> don't trigger a refresh
    return time.time() * 1000 >= (exp - skew_seconds * 1000)


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
    return RefreshResult(True, "Token refreshed", 200)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _lock_paths(cred_dir: Path) -> tuple[Path, Path]:
    try:
        legacy = Path(os.path.realpath(cred_dir))
    except OSError:
        legacy = cred_dir
    return cred_dir / LOCK_NAME, Path(str(legacy) + ".lock")


def _take_dir_lock(lock: Path) -> bool:
    """mkdir-based lock, compatible with proper-lockfile. False if held."""
    for _ in range(2):
        try:
            os.mkdir(lock)
            return True
        except FileExistsError:
            try:
                age = time.time() - os.stat(lock).st_mtime
            except OSError:
                continue            # vanished between mkdir and stat -> retry
            if age < LOCK_STALE_SECONDS:
                return False
            try:                    # stale: holder died, take it over
                os.rmdir(lock)
            except OSError:
                return False
    return False


@contextmanager
def refresh_lock(cred_dir: Path):
    """Hold Claude Code's refresh lock. Yields False if another process has it.

    The primary lock is required (an unwritable directory raises OSError); the
    legacy one is best-effort, as it is for the CLI.
    """
    primary, legacy = _lock_paths(cred_dir)
    if not _take_dir_lock(primary):
        yield False
        return
    try:
        have_legacy = _take_dir_lock(legacy)
        legacy_busy = not have_legacy and legacy.is_dir()
    except OSError:
        have_legacy = legacy_busy = False
    try:
        yield not legacy_busy       # an older CLI may be mid-refresh
    finally:
        for lock, held in ((legacy, have_legacy), (primary, True)):
            if held:
                try:
                    os.rmdir(lock)
                except OSError:
                    pass


def _post_refresh(http, body: dict):
    """POST to the token endpoint, falling back to the legacy host only when the
    current one is unreachable or doesn't route the request."""
    import httpx

    last_exc: Exception | None = None
    for i, url in enumerate(OAUTH_TOKEN_URLS):
        last = i == len(OAUTH_TOKEN_URLS) - 1
        try:
            resp = http.post(url, headers=REFRESH_HEADERS, json=body)
        except httpx.HTTPError as exc:
            last_exc = exc
            continue
        if not last and (resp.status_code in (404, 405) or 300 <= resp.status_code < 400):
            continue
        return resp
    raise last_exc if last_exc else httpx.HTTPError("no token endpoint reachable")


def refresh(path: Path, *, timeout: float = 20.0, force: bool = False,
            seen_access: str | None = None) -> RefreshResult:
    """Refresh the access token in `path`. Safe: backs up + reverts on failure.

    ``force`` refreshes even when the stored expiry says the token is fine (the
    API just answered 401). ``seen_access`` is the access token the caller was
    using: if the file holds a different one by the time we have the lock,
    someone else already refreshed and there is nothing to do.
    """
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
        path.read_text(encoding="utf-8")
    except OSError as exc:
        return RefreshResult(False, f"Cannot read credentials: {exc}")

    try:
        with refresh_lock(path.parent) as held:
            if not held:
                return RefreshResult(
                    False, "Claude Code is refreshing the token — will re-check shortly",
                    busy=True)
            return _refresh_locked(path, timeout=timeout, force=force,
                                   seen_access=seen_access)
    except OSError as exc:
        return RefreshResult(False, f"Cannot take the refresh lock: {exc}")


def _refresh_locked(path: Path, *, timeout: float, force: bool,
                    seen_access: str | None) -> RefreshResult:
    # Re-read under the lock: Claude Code may have rotated the tokens while we
    # waited, and spending the old refresh token now would kill one of them.
    try:
        original_raw = path.read_text(encoding="utf-8")
        data = json.loads(original_raw)
    except (OSError, json.JSONDecodeError) as exc:
        return RefreshResult(False, f"Cannot read credentials: {exc}")

    blk = _oauth_block(data)
    if not blk or not isinstance(blk.get("refreshToken"), str):
        return RefreshResult(False, "No refresh token found in credentials")
    if seen_access is not None and blk.get("accessToken") != seen_access:
        return RefreshResult(True, "Token already refreshed by Claude Code",
                             new_expiry_ms=blk.get("expiresAt"))
    if not force:
        exp = blk.get("expiresAt")
        if (isinstance(exp, (int, float))
                and time.time() * 1000 < exp - EXPIRY_SKEW_SECONDS * 1000):
            return RefreshResult(True, "Token still valid", new_expiry_ms=int(exp))
    refresh_tok = blk["refreshToken"]
    if _token_hash(refresh_tok) in _dead_refresh_tokens:
        return RefreshResult(
            False, "Login expired — run `claude` and /login to sign in again", 400)

    # httpx imported lazily so this module stays importable/testable without it.
    import httpx

    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_tok,
        "client_id": blk.get("clientId") if isinstance(blk.get("clientId"), str) else CLIENT_ID,
    }
    scopes = blk.get("scopes")
    if isinstance(scopes, list) and scopes and all(isinstance(x, str) for x in scopes):
        body["scope"] = " ".join(scopes)
    try:
        with httpx.Client(timeout=timeout) as http:
            resp = _post_refresh(http, body)
    except httpx.HTTPError as exc:
        return RefreshResult(False, f"Refresh request failed: {exc}")

    if resp.status_code == 429:
        return RefreshResult(False, "Rate limited by token endpoint — backing off", 429)
    if resp.status_code in (400, 401):
        _dead_refresh_tokens.add(_token_hash(refresh_tok))
        return RefreshResult(
            False, f"Refresh rejected (HTTP {resp.status_code}) — run `claude` and "
            "/login to sign in again", resp.status_code)
    if resp.status_code != 200:
        return RefreshResult(
            False, f"Refresh rejected (HTTP {resp.status_code}) — re-login may be needed",
            resp.status_code,
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
    if isinstance(tok.get("scope"), str) and tok["scope"].strip():
        blk["scopes"] = tok["scope"].split()

    result = _write_tokens_safely(path, original_raw, data, new_access)
    if result.ok:
        result.new_expiry_ms = new_expiry_ms
    return result
