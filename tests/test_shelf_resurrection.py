"""The shelf judges a session's recency by its last real ACTIVITY, not the
file's mtime — so a maintenance pass that rewrites a long-dead transcript
(bumping the mtime, adding no content) cannot resurrect it onto the shelf.

Regression for the observed bug: sessions last active 9-41h ago, their files
rewritten minutes earlier, popping back onto the shelf as IDLE tiles.

Run with `python -m pytest tests/ -q`.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import transcript as T  # noqa: E402


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def _write_session(path, last_event_epoch):
    """A minimal but realistic parent transcript whose last real event (an
    assistant tool_use) is at `last_event_epoch`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "timestamp": _iso(last_event_epoch - 5),
         "cwd": "/home/u/proj", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "timestamp": _iso(last_event_epoch),
         "cwd": "/home/u/proj",
         "message": {"role": "assistant",
                     "content": [{"type": "tool_use", "name": "Read",
                                  "input": {"file_path": "x.py"}}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
    ]
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n",
                    encoding="utf-8")


def test_last_activity_ts_reads_the_last_event_not_mtime(tmp_path):
    now = time.time()
    f = tmp_path / "C--proj" / "sess.jsonl"
    _write_session(f, now - 40 * 3600)          # last active 40h ago
    st = f.stat()
    ts = T.last_activity_ts(f, st.st_size, st.st_mtime)
    assert abs(ts - (now - 40 * 3600)) < 5, "did not read the last event's own time"


def test_last_activity_ts_is_immune_to_a_pure_mtime_touch(tmp_path):
    """The resurrection itself: content is old, but the file was just touched.
    The reported activity time must stay OLD (from the content), not follow the
    fresh mtime."""
    now = time.time()
    f = tmp_path / "C--proj" / "sess.jsonl"
    _write_session(f, now - 40 * 3600)
    os.utime(f, (now, now))                      # touched now, no new content
    st = f.stat()
    assert now - st.st_mtime < 5, "precondition: mtime is fresh"
    ts = T.last_activity_ts(f, st.st_size, st.st_mtime)
    assert now - ts > 39 * 3600, (
        f"followed the mtime touch: reported {now - ts:.0f}s ago, should be ~40h")


def test_last_activity_ts_falls_back_to_mtime_when_unparseable(tmp_path):
    # A file with no parseable timestamp must not be hidden — fall back to mtime.
    f = tmp_path / "C--proj" / "junk.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("not json\n{}\n", encoding="utf-8")
    st = f.stat()
    assert T.last_activity_ts(f, st.st_size, st.st_mtime) == st.st_mtime


def test_a_touched_but_dead_session_is_kept_off_the_shelf(tmp_path, monkeypatch):
    """End-to-end through the watcher's scan: an active session shows; a dead
    session whose file was just touched does NOT (the old code, keying off
    mtime, would have resurrected it)."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(T, "TRANSCRIPTS_DIR", tmp_path)
    now = time.time()

    live = tmp_path / "C--proj" / "live.jsonl"
    _write_session(live, now - 3)               # active right now

    dead = tmp_path / "C--proj" / "dead.jsonl"
    _write_session(dead, now - 40 * 3600)       # last active 40h ago
    os.utime(dead, (now, now))                  # ...but its file was just touched

    watcher = T.TranscriptWatcher()
    entries = watcher._scan_entries()
    groups = T.group_sessions(entries, now)
    shown = {p.stem for p, _ in groups}
    assert "live" in shown, "the genuinely active session must show"
    assert "dead" not in shown, (
        "a touched-but-dead session resurrected onto the shelf")


def test_scan_entries_still_uses_mtime_for_genuinely_stale_files(tmp_path,
                                                                 monkeypatch):
    # A file with an OLD mtime is out of the window regardless — the scan must
    # not waste a content read on it, and it simply carries its (old) mtime.
    monkeypatch.setattr(T, "TRANSCRIPTS_DIR", tmp_path)
    now = time.time()
    old = tmp_path / "C--proj" / "old.jsonl"
    _write_session(old, now - 40 * 3600)
    os.utime(old, (now - 40 * 3600, now - 40 * 3600))  # old content AND old mtime

    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    entries = dict((p.stem, r) for p, r in T.TranscriptWatcher()._scan_entries())
    assert now - entries["old"] > 39 * 3600  # excluded by recency either way
