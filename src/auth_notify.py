"""Decide when to say that Clawdmeter has gone blind — and when it can see again.

The Qt-free core behind the "usage can't be read" alert. It watches consecutive
UsageSamples and fires edge-triggered, so the user hears about an auth failure
once rather than on every poll for however long it lasts.

The gap this closes: the session shelf reads transcripts off disk and needs no
token at all, so the window goes on looking alive while the half that needs a
token is dead. That is how a token which expired at 02:42 went unnoticed for 34
hours — nothing was broken enough to notice from across the room.

Only AUTH failures count. A dropped network or a 500 is not something the user
can act on and clears itself, so pushing a phone notification for one would
train them to ignore the channel.

All side effects (toast, sound, push) live in the dashboard; this module only
decides, mirroring reset_notify and approaching_notify.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # type-only — keeps this module free of Qt/httpx at runtime
    from poller import UsageSample

# Statuses that mean "we cannot read usage and only you can fix it".
#
# Written as literals rather than imported from poller ON PURPOSE. Importing
# poller for three strings would drag PySide6 and httpx into a module that
# says, two paragraphs up, that it has neither — and would make this module
# unimportable from poller, which is where the statuses live and the natural
# place for a future caller to be. `test_the_statuses_match_the_pollers_own_names`
# fails if either side is renamed, which is the guarantee the import was for.
AUTH_FAILURE_STATUSES = frozenset({
    "auth-expired", "reauth-needed", "no-token",
})

_LOST_BODIES = {
    "auth-expired":
        "The Claude token expired. Clawdmeter is trying to refresh it.",
    "reauth-needed":
        "The Claude token expired and can't be refreshed — sign in again "
        "from Settings, or run: claude auth login",
    "no-token":
        "No Claude credentials were found, so usage can't be read.",
}

LOST_TITLE = "Clawdmeter can't read your usage"
REAUTH_TITLE = "Clawdmeter needs you to sign in"
RESTORED_TITLE ="Clawdmeter is reading your usage again"
RESTORED_BODY = "The connection recovered — the 5h and 7d windows are live again."


@dataclass(frozen=True)
class AuthAlert:
    kind: str      # "lost" | "restored"
    title: str
    body: str
    status: str    # the poller status that triggered it ("" when restored)


class AuthNotifier:
    """Edge-triggered auth-health detector.

    Feed every UsageSample to observe(); it returns an AuthAlert on the
    transitions that matter — going blind, an expiry becoming "sign in again",
    and seeing again — and None the rest of the time.

    Deliberately NOT primed on first sight, unlike ApproachingNotifier: if the
    app starts up already unable to authenticate, that is exactly the moment
    worth saying so. Starting silent would reproduce the original failure for
    anyone who launches the app after the token has already died.
    """

    def __init__(self) -> None:
        self._blind = False
        # Whether the user has already been told that only signing in helps.
        # The first alert for an expiry says Clawdmeter is refreshing; if that
        # refresh is then rejected, staying silent would leave that promise
        # standing while nothing is coming — so that one change alerts again.
        self._told_reauth = False

    def observe(self, s: UsageSample, *, enabled: bool = True) -> AuthAlert | None:
        status = getattr(s, "status", "") or ""
        ok = bool(getattr(s, "ok", False))
        failing = (not ok) and status in AUTH_FAILURE_STATUSES

        if failing and not self._blind:
            self._blind = True
            self._told_reauth = status == "reauth-needed"
            # State advances even when alerts are off, so turning them on later
            # doesn't immediately fire about a condition that began long ago.
            if not enabled:
                return None
            return AuthAlert("lost", LOST_TITLE,
                             _LOST_BODIES.get(status, "Usage can't be read."), status)

        # Already blind, and the failure has just become one only the user can
        # fix. Once per outage: flipping back and forth must not repeat it.
        if failing and status == "reauth-needed" and not self._told_reauth:
            self._told_reauth = True
            if not enabled:
                return None
            return AuthAlert("lost", REAUTH_TITLE, _LOST_BODIES[status], status)

        # Recovery is a real OK sample, never merely "a different failure".
        # An offline sample after an expiry must not read as "all better now".
        if ok and self._blind:
            self._blind = False
            self._told_reauth = False
            if not enabled:
                return None
            return AuthAlert("restored", RESTORED_TITLE, RESTORED_BODY, "")

        return None
