"""Saying — once — that Clawdmeter has gone blind.

The shelf reads transcripts off disk and needs no token, so the window goes on
looking alive while the half that needs one is dead. Nothing ever said so, and
a token that expired at 02:42 went unnoticed for 34 hours.

Edge-triggered is the requirement, not a nicety: at a 30-second poll interval,
"tell me every time" would have been 4,000 notifications over that stretch.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

import auth_notify  # noqa: E402
import poller  # noqa: E402
from auth_notify import AuthNotifier  # noqa: E402


def _bad(status):
    return poller.UsageSample(0, 0, 0, 0, status, False, "boom", 0.0)


def _good():
    return poller.UsageSample(10, 60, 20, 600, "allowed", True, None, 0.0)


# Written out by hand, NOT derived from AUTH_FAILURE_STATUSES. A test that
# parametrises over the set it is checking cannot notice the set shrinking —
# dropping a status just removes a case and the suite still passes green. A
# mutation run caught exactly that.
WATCHED_STATUSES = ["auth-expired", "reauth-needed", "no-token"]


@pytest.mark.parametrize("status", WATCHED_STATUSES)
def test_every_auth_failure_raises_the_alarm(status):
    assert AuthNotifier().observe(_bad(status)).kind == "lost"


def test_the_watched_set_is_exactly_these_three():
    assert auth_notify.AUTH_FAILURE_STATUSES == set(WATCHED_STATUSES)


def test_it_fires_once_not_on_every_poll():
    n = AuthNotifier()

    first = n.observe(_bad(poller.STATUS_AUTH_EXPIRED))
    rest = [n.observe(_bad(poller.STATUS_AUTH_EXPIRED)) for _ in range(50)]

    assert first is not None
    assert rest == [None] * 50


def test_recovery_is_announced_once():
    n = AuthNotifier()
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED))

    assert n.observe(_good()).kind == "restored"
    assert n.observe(_good()) is None


def test_it_can_go_blind_again_after_recovering():
    n = AuthNotifier()
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED))
    n.observe(_good())

    assert n.observe(_bad(poller.STATUS_AUTH_EXPIRED)).kind == "lost"


@pytest.mark.parametrize("status", [poller.STATUS_OFFLINE, poller.STATUS_HTTP_ERROR])
def test_transient_trouble_is_not_worth_a_notification(status):
    """A dropped network clears itself; pushing for it trains you to ignore them."""
    assert AuthNotifier().observe(_bad(status)) is None


def test_a_network_blip_during_an_outage_is_not_a_recovery():
    """The dangerous false positive: 'all better now' when nothing improved."""
    n = AuthNotifier()
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED))

    assert n.observe(_bad(poller.STATUS_OFFLINE)) is None
    # Still blind, so a later genuine recovery is still worth announcing.
    assert n.observe(_good()).kind == "restored"


def test_a_dead_refresh_token_says_what_to_do_about_it():
    alert = AuthNotifier().observe(_bad(poller.STATUS_REAUTH_NEEDED))

    assert "sign in again" in alert.body.lower()


def test_a_plain_expiry_does_not_tell_you_to_sign_in():
    """It may well fix itself on the next refresh; don't send anyone to a terminal."""
    alert = AuthNotifier().observe(_bad(poller.STATUS_AUTH_EXPIRED))

    assert "sign in again" not in alert.body.lower()


def test_an_expiry_that_turns_into_sign_in_again_says_so():
    """The first alert promised a refresh. When that refresh is rejected the
    promise is dead, and staying quiet would leave it standing."""
    n = AuthNotifier()
    seq = [_bad(poller.STATUS_AUTH_EXPIRED), _bad(poller.STATUS_AUTH_EXPIRED),
           _bad(poller.STATUS_REAUTH_NEEDED), _bad(poller.STATUS_REAUTH_NEEDED),
           _good()]

    alerts = [a for a in (n.observe(s) for s in seq) if a is not None]

    assert [a.kind for a in alerts] == ["lost", "lost", "restored"]
    assert "sign in again" in alerts[1].body.lower()
    assert alerts[1].title == auth_notify.REAUTH_TITLE


def test_sign_in_again_is_said_once_per_outage():
    """Flapping between the two statuses must not repeat it, and an outage that
    STARTED as sign-in-again has nothing further to escalate to."""
    n = AuthNotifier()
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED))
    n.observe(_bad(poller.STATUS_REAUTH_NEEDED))
    later = [n.observe(_bad(s)) for s in (poller.STATUS_AUTH_EXPIRED,
                                           poller.STATUS_REAUTH_NEEDED)]
    assert later == [None, None]

    m = AuthNotifier()
    m.observe(_bad(poller.STATUS_REAUTH_NEEDED))
    assert m.observe(_bad(poller.STATUS_REAUTH_NEEDED)) is None


def test_the_escalation_re_arms_after_a_recovery():
    n = AuthNotifier()
    for s in (poller.STATUS_AUTH_EXPIRED, poller.STATUS_REAUTH_NEEDED):
        n.observe(_bad(s))
    n.observe(_good())
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED))

    assert n.observe(_bad(poller.STATUS_REAUTH_NEEDED)) is not None


def test_starting_up_already_broken_still_alerts():
    """Deliberately unprimed: launching after the token died is the worst case."""
    assert AuthNotifier().observe(_bad(poller.STATUS_AUTH_EXPIRED)) is not None


def test_turning_alerts_off_silences_them():
    assert AuthNotifier().observe(_bad(poller.STATUS_AUTH_EXPIRED), enabled=False) is None


def test_turning_alerts_on_does_not_re_announce_an_old_outage():
    """State advances while off, so enabling later isn't a surprise alert."""
    n = AuthNotifier()
    n.observe(_bad(poller.STATUS_AUTH_EXPIRED), enabled=False)

    assert n.observe(_bad(poller.STATUS_AUTH_EXPIRED), enabled=True) is None


def test_every_auth_failure_status_has_its_own_wording():
    """A new one with no entry would fall back to a message that says nothing."""
    assert set(WATCHED_STATUSES) <= set(auth_notify._LOST_BODIES)


def test_the_statuses_match_the_pollers_own_names():
    """Guards a rename on either side silently unwatching a failure."""
    assert auth_notify.AUTH_FAILURE_STATUSES == {
        poller.STATUS_AUTH_EXPIRED, poller.STATUS_REAUTH_NEEDED, poller.STATUS_NO_TOKEN,
    }


def test_auth_notify_does_not_import_qt_or_httpx():
    """The module docstring promises this; a `from poller import ...` broke it.

    Importing poller for three string constants dragged PySide6 and httpx into
    a module that claims to need neither, and made this module unimportable
    from poller — where the statuses live and where a future caller belongs.
    """
    import subprocess
    import sys as _sys

    src = os.path.join(os.path.dirname(__file__), "..", "src")
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "import auth_notify;"
        "loaded = [m for m in ('PySide6', 'httpx', 'poller') if m in sys.modules];"
        "print(','.join(loaded))" % src
    )
    out = subprocess.run([_sys.executable, "-c", probe],
                         capture_output=True, text=True, check=True)

    assert out.stdout.strip() == "", f"auth_notify pulled in {out.stdout.strip()}"
