"""Decide when usage is *approaching* (or has crossed) a limit.

The Qt-free core behind the "you're nearing your limit" and "you've crossed
into overage" alerts. It watches consecutive UsageSamples and fires
edge-triggered events when the session (5h) or weekly (7d) utilization crosses
a configured threshold upward, and again when it crosses 100% into paid credits.
Scoped windows the user chose to show (e.g. Weekly · Fable) are watched the
same way, against the threshold of their group.

Edge-triggering is the whole point: each axis warns *once* per cycle and re-arms
only after utilization falls back below the level (which a reset does sharply),
so an alert can't repeat on every poll while you sit above the threshold. A
small re-arm margin keeps noise around the boundary from re-firing it.

All Qt side effects (toast, sound, push) live in the dashboard; this module
only decides, mirroring reset_notify.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scoped_windows import warn_threshold_for

if TYPE_CHECKING:  # type-only — keeps this module free of Qt/httpx at runtime
    from poller import UsageSample
    from scoped_windows import ScopedWindow

# How far below a level utilization must fall before that level can fire again.
# Guards against re-alerting when the number jitters around the boundary.
REARM_MARGIN = 5
OVERAGE_PCT = 100


@dataclass(frozen=True)
class LimitEvent:
    window: str    # "Session (5h)" / "Weekly (7d)" / "Weekly · Fable"
    # "approaching" / "overage" / "reached". A scoped window crossing 100% is
    # "reached", not "overage": whether a model's own cap spills onto paid
    # credits (as the 5h/7d windows do) or just stops that model is unverified,
    # so its alert must not claim either.
    kind: str
    pct: int       # utilization at the crossing
    threshold: int # the level crossed (the configured % or 100 for overage)


class ApproachingNotifier:
    """Edge-triggered threshold/overage detector for the usage windows.

    Feed every UsageSample to observe() along with the live settings and the
    scoped windows being shown; it returns the list of LimitEvents that just
    fired (usually empty). State advances only on OK samples. Each window's
    baseline is primed silently the first time it is seen — the 5h/7d windows
    on the first OK sample, a scoped window whenever it starts being watched —
    so launching the app (or ticking a window) while already above a threshold
    doesn't nag; only an actual upward crossing alerts.
    """

    def __init__(self, rearm_margin: int = REARM_MARGIN) -> None:
        self._margin = rearm_margin
        # (axis, level) -> already-warned-this-cycle. level: "thr" | "ovr".
        # An axis with no entries hasn't been seen yet and primes on sight.
        self._warned: dict[tuple[str, str], bool] = {}

    def observe(
        self,
        s: UsageSample,
        *,
        enabled: bool,
        session_threshold: int,
        weekly_threshold: int,
        overage_enabled: bool,
        scoped: "tuple[ScopedWindow, ...] | list[ScopedWindow]" = (),
    ) -> list[LimitEvent]:
        if not getattr(s, "ok", False):
            return []  # ignore error/no-token samples; don't disturb state

        session_threshold, weekly_threshold = int(session_threshold), int(weekly_threshold)
        # (axis, label, pct, threshold, kind used at 100%)
        axes = [
            ("session", "Session (5h)", int(s.session_pct), session_threshold, "overage"),
            ("weekly", "Weekly (7d)", int(s.weekly_pct), weekly_threshold, "overage"),
        ]
        axes += [
            (f"scoped:{w.key}", w.label, int(w.pct),
             warn_threshold_for(w, session_threshold, weekly_threshold), "reached")
            for w in scoped
        ]

        # A scoped window no longer watched (unticked, or no longer reported)
        # forgets its state, so watching it again primes silently.
        live = {axis for axis, *_ in axes}
        for key in [k for k in self._warned if k[0] not in live]:
            del self._warned[key]

        events: list[LimitEvent] = []
        for axis, label, pct, thr, full_kind in axes:
            # First sighting: seed "already warned" from the current level so we
            # don't fire for a threshold that was already exceeded before.
            if (axis, "thr") not in self._warned:
                self._warned[(axis, "thr")] = pct >= thr
                self._warned[(axis, "ovr")] = pct >= OVERAGE_PCT
                continue

            # Re-arm when utilization drops clear of a level (covers resets).
            if pct < thr - self._margin:
                self._warned[(axis, "thr")] = False
            if pct < OVERAGE_PCT - self._margin:
                self._warned[(axis, "ovr")] = False

            if not enabled:
                continue

            # Approaching: at/above the threshold but not yet into overage.
            if thr <= pct < OVERAGE_PCT and not self._warned[(axis, "thr")]:
                self._warned[(axis, "thr")] = True
                events.append(LimitEvent(label, "approaching", pct, thr))

            # Crossed 100%: overage for the 5h/7d windows, "reached" for a
            # scoped one (see LimitEvent.kind).
            if overage_enabled and pct >= OVERAGE_PCT and not self._warned[(axis, "ovr")]:
                self._warned[(axis, "ovr")] = True
                # A jump straight past the threshold into overage shouldn't also
                # queue an approaching alert for the same cycle.
                self._warned[(axis, "thr")] = True
                events.append(LimitEvent(label, full_kind, pct, OVERAGE_PCT))

        return events
