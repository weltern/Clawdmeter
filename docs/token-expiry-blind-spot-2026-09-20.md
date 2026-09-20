# Fixed: an expired token stopped the 5h/7d windows updating, silently, for 34 hours

> Reported 2026-09-20 ("it's seeing the sessions but it's not grabbing the 5h,
> 7d info"). Root cause confirmed the same day against `usage_history.jsonl`,
> the credentials file's own `expiresAt`, and a live probe. Fixed the same day.

## Symptom

The session shelf kept working — mascots, activity, token counts — while the
SESSION (5h) and WEEKLY (7d) bars sat frozen on values from the previous night.
Nothing in the window said anything was wrong.

## Root cause

The OAuth access token in `~/.claude/.credentials.json` expired and nothing
renewed it. A live probe with the stored token:

```
HTTP 401 {"type":"error","error":{"type":"authentication_error",
          "message":"OAuth access token has expired. Re-authenticate to continue."}}
```

The timeline is unambiguous:

| when | what |
|---|---|
| 2026-09-18 18:42:10 | credentials file last written (by a Claude Code CLI session) |
| 2026-09-19 02:37:31 | last sample recorded in `usage_history.jsonl` |
| 2026-09-19 02:42:10 | `claudeAiOauth.expiresAt` |
| 2026-09-20 12:52 | reported; 21,113 samples before the gap, none after |

Polling stopped five minutes before the expiry stamp and never resumed.

### Why the window still looked alive

`TranscriptWatcher` reads `~/.claude/projects/**/*.jsonl` off disk and needs no
credentials at all — four transcripts had been touched in the preceding 24
hours. Only the half that needs a token was dead. This asymmetry is the whole
reason it went unnoticed: nothing was broken enough to see from across the room.

### Why nothing renewed the token

The user had moved from the Claude Code CLI to the **desktop app**. The desktop
app does not use `~/.claude/.credentials.json` — it authenticates through its
own Electron session store (`%APPDATA%\Claude\Network\Cookies`, written
continuously throughout). When the CLI was the daily driver, every session
refreshed that file and Clawdmeter always found a live token. On the desktop
app, nothing does.

Note that `claude auth status` reports `"loggedIn": true` in this state. It
reads presence, not validity, so it cannot be used as a health check.

### Why Clawdmeter's own auto-refresh didn't save it

Auto-refresh was on (defaults true, no registry override) and autostart was on
(`HKCU\...\Run` → `Clawdmeter.exe --startup`), so the app was almost certainly
running and retrying the whole time. Three separate faults kept it stuck:

1. **It only refreshed once the token was already dead.** `is_expired()` fired
   at expiry + a 120s skew, so there was always a window where every poll 401'd
   and recovery depended on the token endpoint answering at that exact moment.
2. **A failing refresh retried forever at four attempts an hour.** The cooldown
   doubled to a 900s ceiling and stayed there — roughly 136 attempts over 34
   hours against an endpoint that throttles hard, which plausibly helped hold
   its own lockout open.
3. **It could not tell "throttled" from "this refresh token is dead."** Both
   surfaced as a failed refresh; only the second needs a re-login.

Two probes to `console.anthropic.com/v1/oauth/token` with a deliberately
invalid refresh token, 25 minutes apart, both returned **429 with no
`Retry-After`**. That is consistent with throttling but does not prove it:
the endpoint may simply answer 429 to invalid tokens. Distinguishing the two
requires attempting a refresh with the real token, which is exactly what the
app now reports rather than swallowing.

### Why the app said nothing

`_apply_status_badge()` surfaced only `reject`/`block` and `warn` — the
rate-limit statuses. Every probe failure arrived as the status string
`"error"`, which matched none of them and fell through to the `else` branch,
which **clears** the badge. The only trace anywhere was a tray tooltip reading
`Clawdmeter - error`.

Settings → Connection had correct wording for this the whole time ("Token
expired — refresh now, or wait for auto-refresh"). The main window was the one
surface that stayed silent, and it is the one that is actually on screen.

## Fix applied (2026-09-20)

**Failures now name themselves.** `_poll_once()` classifies instead of
flattening: 401/403 → `auth-expired`, another non-2xx → `http-error`, a
transport failure → `offline`, plus `reauth-needed` for an expiry whose refresh
token has been refused. The badge and the tray tooltip render from one table
(`dashboard.FAILURE_BADGES`) so they cannot disagree, and the wording splits on
what the user should do: "Token expired" names an action, "Offline" says to
wait.

**The token is renewed before it dies.** `token_refresh.needs_refresh()` opens a
30-minute window ahead of expiry. It is the same one refresh per token lifetime,
just taken while there is still a working token to fall back on. `is_expired()`
keeps its old 120s skew and its old meaning — Settings asks whether the thing is
broken *now*, and widening that would have the panel calling a healthy token
expired.

**Backoff keys off the cause.** `RefreshOutcome` splits `THROTTLED` (429, ceiling
raised to four hours) from `REJECTED` (400/401, stop entirely — a dead refresh
token cannot be retried into working) from `UNREACHABLE` (transport, short
ceiling). The block self-clears when the credentials file's `expiresAt` changes,
so signing in again resumes polling with nothing having to notify the poller.

**There is a way to sign in again.** `reauth.py` hands off to
`claude auth login --claudeai` rather than reimplementing the flow — its
authorize endpoint, redirect URI and scopes are documented nowhere verifiable,
and this is the worst place in the app to be guessing. It adds no dependency:
the credentials file Clawdmeter reads is Claude Code's, so with no CLI there was
never a token to read. The Connection tab gets a **Sign in again** button,
always present and disabled with a reason when the token is healthy. On macOS it
stays enabled — it is the one remedy that works there, and the note above it
already said so.

**And the app says when it goes blind.** `AuthNotifier` fires once on the
OK → auth-failed transition and once on recovery, through the same channels as
the reset alert. Edge-triggering is the requirement, not a nicety: at a
30-second poll, "tell me every time" would have been about four thousand
notifications over this outage. Only auth failures count — a dropped network
clears itself, and alerting on it would train the user to ignore the channel.
Recovery must be a genuine OK sample, so an offline poll during an outage cannot
read as "all better now".

## Verification

- `tests/test_failure_status_badge.py` (14), `tests/test_refresh_resilience.py`
  (34), `tests/test_auth_notify.py` (17). Full suite: 682 passed.
- Mutation-checked, 19/19 killed across two runs. Two survivors were found and
  fixed rather than reported: nothing asserted that the *poller* calls
  `needs_refresh()` (swapping it back for `is_expired()` left the suite green),
  and the auth-status test parametrised over the very set it was checking, so
  dropping a status just removed a case.
- The badge fix was revert-validated in both directions: blanking
  `FAILURE_BADGES` fails 5 tests, reverting the poller classification fails 5
  others, and the restored tree goes green.

## Source references

- `src/poller.py` — `_poll_once()`, `_do_refresh()`, `_reauth_needed()`, `_mark_reauth()`
- `src/token_refresh.py` — `RefreshOutcome`, `needs_refresh()`, `is_expired()`
- `src/reauth.py` — the CLI handoff
- `src/auth_notify.py` — `AuthNotifier`
- `src/dashboard.py` — `FAILURE_BADGES`, `_apply_status_badge()`, `_dispatch_auth_alert()`
