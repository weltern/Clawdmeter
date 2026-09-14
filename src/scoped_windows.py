"""Scoped usage windows — the limits the usage API reports beside the overall
5h / 7d windows, e.g. a weekly cap on one model ("Weekly · Fable").

Qt-free, so every rule here is unit-testable: parsing them out of
/api/oauth/usage, which ones the user chose to show, and holding the last-known
list when a poll's usage request fails. The dashboard's bars (full, compact,
mini) and the approaching-limit alerts all read from here, so they can't
disagree about what is showing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ScopedWindow:
    """One scoped limit at the moment it was polled."""

    key: str    # stable identity for Settings, e.g. "weekly:Fable"
    group: str  # the API's window group: "weekly" / "session"
    name: str   # what the limit is scoped to, e.g. "Fable"
    pct: int    # utilisation %; like the 5h/7d windows it is not clamped at 100
    resets_at: float | None = None  # epoch seconds, None when the API gives none

    @property
    def label(self) -> str:
        """"Weekly · Fable" — the group reads the same way as the main bars."""
        return f"{self.group.capitalize()} · {self.name}"

    def reset_minutes(self, now: float) -> int:
        """Whole minutes from ``now`` until the reset, rounded the same way as
        the header-derived 5h/7d windows so a scoped window that resets with
        the weekly one shows the same countdown. 0 when unknown or past."""
        if self.resets_at is None:
            return 0
        mins = (self.resets_at - now) / 60.0
        return int(round(mins)) if mins > 0 else 0


def _scope_name(scope) -> str | None:
    """What an entry is scoped to. A model scope today; a surface scope is
    accepted too, so a limit on one surface isn't silently dropped."""
    if not isinstance(scope, dict):
        return None
    for part in ("model", "surface"):
        value = scope.get(part)
        if isinstance(value, dict):
            value = value.get("display_name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_resets_at(value) -> float | None:
    """ISO-8601 ``resets_at`` -> whole epoch seconds (the rate-limit headers
    carry whole seconds, so the two countdowns round identically)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return float(int(datetime.fromisoformat(value).timestamp()))
    except ValueError:
        return None


def windows_from_limits(limits) -> list[ScopedWindow]:
    """Scoped windows from the usage response's ``limits[]``, in API order.

    Unscoped entries (the overall 5h / 7d windows, already on the dashboard)
    and entries without a numeric percent are skipped. A repeated key keeps
    the first entry, so one limit can never draw two bars.
    """
    out: list[ScopedWindow] = []
    seen: set[str] = set()
    for entry in limits or []:
        if not isinstance(entry, dict):
            continue
        name = _scope_name(entry.get("scope"))
        pct = entry.get("percent")
        if not name or isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        group = entry.get("group") if isinstance(entry.get("group"), str) else ""
        group = group.strip().lower() or "weekly"
        key = f"{group}:{name}"
        if key in seen:
            continue
        seen.add(key)
        out.append(ScopedWindow(key, group, name, int(pct),
                                _parse_resets_at(entry.get("resets_at"))))
    return out


class ScopedWindowTracker:
    """The last-known scoped windows.

    ``observe`` takes a sample's ``scoped_windows``: a list replaces what is
    held, ``None`` (the usage request failed that poll) keeps it. Without this
    a single failed request would blank every scoped bar for one poll and
    re-arm its alerts.
    """

    def __init__(self) -> None:
        self._windows: list[ScopedWindow] = []

    @property
    def windows(self) -> list[ScopedWindow]:
        return list(self._windows)

    def observe(self, windows: list[ScopedWindow] | None) -> list[ScopedWindow]:
        if windows is not None:
            self._windows = list(windows)
        return self.windows


def shown(windows, shown_keys) -> list[ScopedWindow]:
    """The windows the user ticked, in API order."""
    keys = set(shown_keys)
    return [w for w in windows if w.key in keys]


def merge_seen(seen, windows) -> list[tuple[str, str]]:
    """Fold the windows just reported into the remembered ``(key, label)``
    list. Order is first-seen; a known key keeps its place and takes the
    latest label. A window that stops being reported stays remembered, so its
    Settings checkbox doesn't come and go with the API."""
    merged = [(str(k), str(lbl)) for k, lbl in seen]
    index = {k: i for i, (k, _) in enumerate(merged)}
    for w in windows:
        if w.key in index:
            merged[index[w.key]] = (w.key, w.label)
        else:
            index[w.key] = len(merged)
            merged.append((w.key, w.label))
    return merged


def warn_threshold_for(window: ScopedWindow, session: int, weekly: int) -> int:
    """Which of the user's two thresholds a scoped window follows — by its
    group, so a weekly cap on one model uses the 7d setting. Shared by the bar
    colour and the approaching alert so they agree."""
    return session if window.group == "session" else weekly
