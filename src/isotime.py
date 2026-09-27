"""ISO-8601 timestamp parsing shared by Qt and Qt-free modules.

Lives on its own so a Qt-free module (scoped_windows) can use the same parser
as the transcript reader, instead of a second one that disagrees about which
timestamps it accepts. Python 3.10 -- the Linux release build -- rejects a
trailing 'Z' and some fractional-second widths in datetime.fromisoformat, and
both appear in the wild; this handles them in one place.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_iso_ts(value: str | None) -> float | None:
    """Parse a Claude Code event ``timestamp`` (ISO-8601 UTC, e.g.
    ``"2026-06-14T03:26:31.977Z"``) into an epoch float.

    Returns None when the value is absent or unparseable so callers can fall
    back to wall-clock time. UTC throughout, so ``time.time() - parse_iso_ts(...)``
    is the true elapsed seconds regardless of local timezone.
    """
    if not isinstance(value, str) or not value:
        return None
    txt = value.strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"

    def _epoch(s: str) -> float:
        dt = datetime.fromisoformat(s)
        # A tz-less timestamp would otherwise be read as LOCAL by .timestamp(),
        # shifting it by the UTC offset. Claude Code always writes a 'Z', but be
        # safe: treat a naive value as UTC.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()

    try:
        return _epoch(txt)
    except ValueError:
        pass
    # Some fromisoformat variants reject odd fractional-second digit counts;
    # strip the ".<digits>" fraction and retry (tz suffix, if any, is kept).
    dot = txt.find(".")
    if dot != -1:
        end = dot + 1
        while end < len(txt) and txt[end].isdigit():
            end += 1
        try:
            return _epoch(txt[:dot] + txt[end:])
        except ValueError:
            return None
    return None
