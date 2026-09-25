"""Watch Claude Code's local JSONL transcripts to drive the mascots.

Claude Code writes an append-only transcript per session at
    ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
Each line is a JSON object representing one event. Assistant events have
`message.content` as a list of blocks; the interesting ones are:

  * `tool_use`  - {"type":"tool_use","name":"Bash"|"Edit"|...}
  * `thinking`  - model is reasoning
  * `text`      - assistant prose

By tailing each recently-touched transcript and looking at the latest block,
we know in near-real-time what every concurrent Claude Code session is doing.
Schema is undocumented so we parse defensively (unknown blocks => fall through
to "thinking").

Sessions whose transcript hasn't been appended to within ACTIVE_WINDOW_SECONDS
drop off the shelf entirely; those still on the shelf but quiet for more than
STALE_SECONDS are reported IDLE/stale so the dashboard can dim them.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

TRANSCRIPTS_DIR = Path.home() / ".claude" / "projects"
POLL_MS = 500
STALE_SECONDS = 90
# A session stays on the shelf only while its last real ACTIVITY was this
# recent (see last_activity_ts — measured from the transcript's own events, NOT
# the file mtime, so a maintenance pass that rewrites a dead transcript can't
# resurrect it). Beyond this window the session is finished and its tile
# vanishes. Kept to 5 min so a burst of session-switching doesn't leave a long
# trail of idle tiles: a session shows active for STALE_SECONDS, dims to IDLE
# for the remainder, then drops off.
ACTIVE_WINDOW_SECONDS = 300
# A subagent shows as a child mascot only while its file was touched this
# recently; once it goes quiet (the agent finished) its mascot drops off.
AGENT_ACTIVE_SECONDS = 45
# A child mascot dims (idle) once its agent has been quiet this long — shorter
# than AGENT_ACTIVE_SECONDS so a finishing agent winds down before it vanishes.
AGENT_STALE_SECONDS = 20
# Bounds the number of live tails (timers + window width); overflow scrolls.
MAX_SESSIONS = 6
# Bounds the child mascots shown under one session's tile.
MAX_AGENTS_PER_SESSION = 6
RESCAN_DIR_EVERY_N_POLLS = 10


class Activity(Enum):
    IDLE = "idle"               # transcript stale / not found -> rate-based fallback
    CODING = "coding"           # tool that writes/runs (Bash, Edit, Write, ...)
    READING = "reading"         # tool that observes (Read, Grep, Glob, ...)
    SEARCHING = "searching"     # web tools
    PLANNING = "planning"       # task/todo/sub-agent management
    INTEGRATING = "integrating" # MCP server tool (mcp__<server>__<tool>)
    THINKING = "thinking"       # text/thinking block, no tool


# Prefix that Claude Code gives every MCP server tool: mcp__<server>__<tool>.
MCP_TOOL_PREFIX = "mcp__"


# Tool name -> activity. Names are matched case-insensitively. Unknown tools
# fall through to CODING because most undocumented tools do *something*.
TOOL_MAP: dict[str, Activity] = {
    "bash": Activity.CODING,
    "powershell": Activity.CODING,
    "bashoutput": Activity.CODING,
    "edit": Activity.CODING,
    "write": Activity.CODING,
    "multiedit": Activity.CODING,
    "notebookedit": Activity.CODING,
    "killshell": Activity.CODING,
    "senduserfile": Activity.CODING,

    "read": Activity.READING,
    "glob": Activity.READING,
    "grep": Activity.READING,
    "notebookread": Activity.READING,
    "toolsearch": Activity.READING,

    "webfetch": Activity.SEARCHING,
    "websearch": Activity.SEARCHING,

    "taskcreate": Activity.PLANNING,
    "taskupdate": Activity.PLANNING,
    "tasklist": Activity.PLANNING,
    "taskget": Activity.PLANNING,
    "taskoutput": Activity.PLANNING,
    "taskstop": Activity.PLANNING,
    "todowrite": Activity.PLANNING,
    "agent": Activity.PLANNING,
    "task": Activity.PLANNING,
    "sendmessage": Activity.PLANNING,
    "exitplanmode": Activity.PLANNING,
    "askuserquestion": Activity.PLANNING,
    "skill": Activity.PLANNING,
    "slashcommand": Activity.PLANNING,
}


def _activity_for_tool(name: str) -> Activity:
    """Map a raw tool name to an Activity.

    MCP server tools are recognised by their `mcp__` prefix before consulting
    the static map; unknown tools fall through to CODING.
    """
    lower = name.lower()
    if lower.startswith(MCP_TOOL_PREFIX):
        return Activity.INTEGRATING
    return TOOL_MAP.get(lower, Activity.CODING)


def _pretty_tool_name(name: str) -> str:
    """Shorten an MCP tool name for the label: mcp__github__get_pr -> github/get_pr."""
    if name.lower().startswith(MCP_TOOL_PREFIX):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"{parts[1]}/{'__'.join(parts[2:])}"
        if len(parts) == 2 and parts[1]:
            return parts[1]
    return name


# Activity -> animation names (must exist in assets/sprites/manifest.json).
ACTIVITY_ANIMS: dict[Activity, list[str]] = {
    Activity.CODING:      ["work coding"],
    Activity.READING:     ["work think"],
    Activity.SEARCHING:   ["idle look around"],
    Activity.PLANNING:    ["idle blink", "idle look around"],
    Activity.INTEGRATING: ["expression surprise", "idle look around"],
    Activity.THINKING:    ["work think"],
    # IDLE has no entry — dashboard falls back to rate-based group.
}

ACTIVITY_LABELS: dict[Activity, str] = {
    Activity.CODING:      "CODING",
    Activity.READING:     "READING",
    Activity.SEARCHING:   "SEARCHING",
    Activity.PLANNING:    "PLANNING",
    Activity.INTEGRATING: "INTEGRATING",
    Activity.THINKING:    "THINKING",
    Activity.IDLE:        "IDLE",
}

# Activity -> glow color shown behind the mascot tile (and used for its activity
# label). Anchored to the app palette (#CE7D6B / #0e1116); IDLE is a dim grey so
# stale tiles read as "no real glow".
ACTIVITY_COLORS: dict[Activity, str] = {
    Activity.CODING:      "#CE7D6B",  # brand warm — matches mockup's active glow
    Activity.READING:     "#5FB3A1",  # teal
    Activity.SEARCHING:   "#8B7DD8",  # violet
    Activity.PLANNING:    "#E0A458",  # amber
    Activity.INTEGRATING: "#C77DBB",  # magenta
    Activity.THINKING:    "#5B8DEF",  # cool blue — matches mockup's middle mascot
    Activity.IDLE:        "#3a3f4b",  # dim grey — no real glow
}


def usage_counts(usage: dict) -> tuple[int, int, int, int]:
    """The four token buckets from a `message.usage` block, missing/garbage -> 0.

    Order is (input, output, cache_read, cache_write) throughout.
    """
    def n(key: str) -> int:
        try:
            return int(usage.get(key) or 0)
        except (TypeError, ValueError):
            return 0
    return (n("input_tokens"), n("output_tokens"),
            n("cache_read_input_tokens"), n("cache_creation_input_tokens"))


def record_key(ev: dict, fallback: str) -> str:
    """Stable identity for ONE transcript record (one JSONL line).

    Resuming a session replays its records verbatim into a new transcript file,
    reusing the event `uuid`, so this is what tells a replayed copy apart from a
    genuinely new record. Namespaced, and falling back to a caller-supplied
    positional token, so a value arriving as a uuid on one record and as
    something else on another can never merge two different records.
    """
    uid = ev.get("uuid")
    return f"uuid:{uid}" if isinstance(uid, str) and uid else fallback


def message_key(msg: dict, fallback: str) -> str:
    """Stable identity for one assistant MESSAGE.

    Claude Code writes a single assistant message as several records — one per
    content block (thinking / text / tool_use) — and every one of them repeats
    the same `message.id` and its own snapshot of `message.usage`. Usage must
    therefore be collapsed per message id, never summed per record.
    """
    mid = msg.get("id")
    return f"id:{mid}" if isinstance(mid, str) and mid else fallback


def merge_counts(prev: tuple[int, int, int, int] | None,
                 new: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """Fold another record of the SAME message into the running figure.

    Per-bucket max, because the records of one message do not agree:

      * `output_tokens` is a running total — early records carry a partial
        count and the last carries the final one (measured across a ~50-day
        corpus: 3,716 such messages, every one non-decreasing, no exceptions).
      * a replayed copy can carry an all-zero usage block while the original
        carries the real figures.

    Keeping whichever copy is seen first therefore under-counts in both cases,
    badly — a 328-token message would land as 5.
    """
    if prev is None:
        return new
    return (max(prev[0], new[0]), max(prev[1], new[1]),
            max(prev[2], new[2]), max(prev[3], new[3]))


@dataclass
class TokenUsage:
    """Running token tally for a session (collapsed per assistant message from
    `message.usage`). `work` (input+output) is the headline figure; the cache
    buckets are shown in the per-session hover breakdown."""
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def work(self) -> int:
        """Fresh input + output — the meaningful 'work' number (excludes the
        cache reads that dominate and distort raw totals)."""
        return self.input + self.output

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def add_usage(self, usage: dict) -> None:
        """Accumulate one assistant message's `message.usage` block.

        Callers are responsible for collapsing a message's several records
        first (see `merge_counts`) — this only adds what it is given.
        """
        self.add_counts(usage_counts(usage))

    def add_counts(self, counts: tuple[int, int, int, int]) -> None:
        """Accumulate an (input, output, cache_read, cache_write) tuple."""
        self.input += counts[0]
        self.output += counts[1]
        self.cache_read += counts[2]
        self.cache_write += counts[3]


@dataclass
class AgentState:
    """One live subagent of a session — drives a small child mascot on the tile."""
    agent_id: str               # subagent file stem (agent-<hash>) — stable key
    activity: Activity
    tool_name: str | None
    is_stale: bool = False


@dataclass
class TranscriptState:
    activity: Activity
    tool_name: str | None       # raw tool name when CODING/READING/etc
    transcript_path: Path | None
    last_event_ts: float | None
    session_id: str | None = None      # transcript file stem (the uuid) — stable tile key
    cwd: str | None = None
    project_name: str | None = None    # friendly name shown on the tile
    target: str | None = None          # what the tool acts on (file/pattern/…); else tool_name
    is_stale: bool = False             # in the shelf window but quiet > STALE_SECONDS
    agents: list[AgentState] = field(default_factory=list)  # live subagents, newest-first
    tokens: TokenUsage = field(default_factory=TokenUsage)  # whole-session token tally


def is_subagent_path(p: Path) -> bool:
    """True for anything under a session's `subagents/` tree.

    Used to EXCLUDE these from top-level sessions — they fold into their parent.
    Note this is deliberately broad: it also covers the `subagents/workflows/`
    bookkeeping tree (journal.jsonl, nested workflow agents), none of which are
    user-facing sessions.
    """
    return "subagents" in p.parts


def is_agent_transcript(p: Path) -> bool:
    """True only for a real child-agent transcript: an `agent-*.jsonl` sitting
    DIRECTLY in a session's `subagents/` dir.

    This excludes the `subagents/workflows/wf_*/` tree (nested workflow agents
    and `journal.jsonl` bookkeeping, which has no assistant events) — those would
    otherwise crowd out and outrank the genuinely-live subagents on the tile.
    """
    return p.parent.name == "subagents" and p.stem.startswith("agent-")


def project_name_from_cwd(cwd: str | None, transcript_path: Path | None) -> str:
    """Best-effort friendly name for a tile.

    Prefer the working directory's leaf (`Path(cwd).name`) since `cwd` rides the
    event stream verbatim and sidesteps the ambiguous folder slug. Fall back to
    the transcript's parent directory name, then to "unknown".
    """
    if cwd:
        name = Path(cwd).name
        if name:
            return name
    if transcript_path is not None:
        parent = transcript_path.parent.name
        if parent:
            return parent
    return "unknown"


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


def session_label(
    custom_title: str | None,
    ai_title: str | None,
    cwd: str | None,
    transcript_path: Path | None,
) -> str:
    """Friendly tile label, in priority order:

    1. the user's **custom** session title (Claude Code `custom-title` event),
    2. Claude Code's **auto-generated** title (`ai-title` event), else
    3. the working-directory leaf (`project_name_from_cwd`).

    Titles live in the session's own transcript as standalone event lines and
    are absent on subagent transcripts, which simply fall back to the cwd leaf.
    """
    for title in (custom_title, ai_title):
        if title and title.strip():
            return title.strip()
    return project_name_from_cwd(cwd, transcript_path)


def fmt_tokens(n: int) -> str:
    """Humanize a token count for display: 0, 940, 8.1K, 19.4M, 2.2B."""
    n = int(n)
    if n < 1000:
        return str(n)
    for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            val = n / div
            return f"{val:.1f}{suffix}" if val < 100 else f"{val:.0f}{suffix}"
    return str(n)


def sum_token_windows(events, now: float) -> tuple[int, int]:
    """Sum 'work' tokens (input+output) over the 5h and 7d windows ending at
    `now`. `events` is any iterable of ``(timestamp_epoch, work_tokens)``.
    Pure, so it's unit-testable independent of disk I/O."""
    cut5 = now - 5 * 3600
    cut7 = now - 7 * 24 * 3600
    w5 = w7 = 0
    for ts, work in events:
        if ts is None or ts < cut7:
            continue
        w7 += work
        if ts >= cut5:
            w5 += work
    return w5, w7


# Per-file cache of parsed (timestamp, work, record_key, message_key) token
# events, keyed by path and invalidated when (size, mtime) change. Most files
# modified within 7d are NOT being actively appended (only the 1–2 live sessions
# are), so this skips re-reading/parsing dozens of recent-but-idle transcripts on
# every poll. The two keys ride along so the ACCOUNT-WIDE caller can collapse
# duplicates across files, which a per-file parse cannot see.
_TOKEN_EVENT_CACHE: dict[
    str, tuple[int, float, list[tuple[float, int, str, str]]]] = {}
# account_window_tokens() is called from BOTH the poll thread and (on a
# Show-token-usage toggle) the GUI thread; serialise their reads/writes/evicts
# of the plain-dict cache so they can't race into a KeyError mid-eviction.
_TOKEN_CACHE_LOCK = threading.Lock()


def _file_token_events(fp: Path) -> list[tuple[float, int, str, str]]:
    """Parse one transcript into (timestamp, input+output, record_key,
    message_key) for every assistant record that has usage + a timestamp.

    Deliberately does NOT collapse: one message's records can be split across
    two files by a session resume, so the collapse belongs to the account-wide
    caller. The positional fallbacks embed the file path, so a record with no
    uuid / no message id can never merge with one from another file.
    """
    events: list[tuple[float, int, str, str]] = []
    try:
        with fp.open(encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = ev.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                ts = parse_iso_ts(ev.get("timestamp"))
                if ts is None:
                    continue
                inp, out, _cr, _cw = usage_counts(usage)
                rkey = record_key(ev, f"pos:{fp}:{lineno}")
                events.append((ts, inp + out, rkey, message_key(msg, rkey)))
    except OSError:
        return []
    return events


def account_window_tokens(now: float, root: Path | None = None) -> tuple[int, int]:
    """Account-wide input+output token totals for the 5h and 7d windows, summed
    across every transcript.

    Only files modified within 7d are read (an older file can't hold an in-window
    event), and each file's parsed events are cached by (size, mtime) so an
    unchanged file isn't re-parsed. Does disk I/O — call it off the UI thread.

    Records are collapsed per assistant message across the WHOLE scan before
    summing: one message is written as several records, and a resumed session
    replays those records into a second transcript file, so both kinds of
    duplicate have to be resolved here rather than per file.
    """
    if root is None:
        root = Path.home() / ".claude" / "projects"
    cut7 = now - 7 * 24 * 3600

    # message_key -> (earliest timestamp seen, largest work figure seen)
    per_message: dict[str, tuple[float, int]] = {}
    seen: set[str] = set()
    with _TOKEN_CACHE_LOCK:
        for fp in root.rglob("*.jsonl"):
            try:
                st = fp.stat()
            except OSError:
                continue
            if st.st_mtime < cut7:
                continue
            key = str(fp)
            seen.add(key)
            cached = _TOKEN_EVENT_CACHE.get(key)
            if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
                events = cached[2]
            else:
                events = _file_token_events(fp)
                _TOKEN_EVENT_CACHE[key] = (st.st_size, st.st_mtime, events)
            # Deliberately NOT filtered by record key first: a replayed copy of
            # a record can be the RICHER one (the original may carry an all-zero
            # usage block), so dropping it on sight would keep the zeros. Folding
            # every record into its message with a per-bucket max makes a replay
            # contribute nothing new while still letting it win where it is
            # larger.
            for ts, work, _rkey, mkey in events:
                prev = per_message.get(mkey)
                if prev is None:
                    per_message[mkey] = (ts, work)
                else:
                    # Earliest timestamp (the message's start) with the largest
                    # work figure — output_tokens grows across the records.
                    per_message[mkey] = (min(prev[0], ts), max(prev[1], work))

        # Drop cache entries for files that aged out of the 7d window / rotated away.
        for key in [k for k in _TOKEN_EVENT_CACHE if k not in seen]:
            del _TOKEN_EVENT_CACHE[key]

    return sum_token_windows(per_message.values(), now)


# File-mutating tools whose target path feeds the "code by language" breakdown
# (matched lowercase). Reads/searches are deliberately excluded — only files
# Claude actually created or changed count as "assisted".
_FILE_MUTATION_TOOLS = {"write", "edit", "multiedit", "notebookedit"}


# Per-file cache of parsed events for the value/activity scan, keyed/invalidated
# like the window cache above. Each entry holds the per-turn token rows
# (ts, model, project, input, output, cache_read, cache_write), the per-tool
# activity events (ts, activity_str), and the mutated-file events (ts, path) so
# one file read serves every Stats aggregate. Separate cache because this scan
# reaches back further than 7d.
_MODEL_EVENT_CACHE: dict[str, tuple[int, float, list, list, list]] = {}


def _file_model_events(fp: Path) -> tuple[list, list, list]:
    """Parse one transcript once into three parallel streams:

      * rows  — (ts, model, project, input, output, cache_read, cache_write,
                record_key, message_key) for every assistant record with a model
                + usage + timestamp. The project (cwd leaf, captured once per
                file) lets value bucket by project.
      * acts  — (ts, activity_str, record_key) for every tool_use block (one per
                call, so parallel tool calls each count), classified via TOOL_MAP.
                Drives the activity breakdown.
      * files — (ts, file_path, record_key) for every file-mutating tool call
                (Write/Edit/…). Drives the code-by-language breakdown.

    The trailing keys are bookkeeping for `scan_events`, which dedups across the
    whole scan and strips them before returning.
    """
    rows: list = []
    acts: list = []
    files: list = []
    cwd: str | None = None
    try:
        with fp.open(encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if cwd is None:
                    c = ev.get("cwd")
                    if isinstance(c, str) and c:
                        cwd = c
                msg = ev.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                ts = parse_iso_ts(ev.get("timestamp"))
                if ts is None:
                    continue
                rkey = record_key(ev, f"pos:{fp}:{lineno}")

                content = msg.get("content")
                if isinstance(content, list):
                    # Key each call by record AND block position: one record can
                    # carry several tool_use blocks (parallel calls), and each of
                    # them is a genuine, separately-counted call.
                    for pos, block in enumerate(content):
                        if not isinstance(block, dict) or block.get("type") != "tool_use":
                            continue
                        name = block.get("name")
                        if not isinstance(name, str) or not name:
                            continue
                        bkey = f"{rkey}#{pos}"
                        acts.append((ts, _activity_for_tool(name).value, bkey))
                        if name.lower() in _FILE_MUTATION_TOOLS:
                            inp = block.get("input")
                            if isinstance(inp, dict):
                                p = inp.get("file_path") or inp.get("notebook_path")
                                if isinstance(p, str) and p:
                                    files.append((ts, p, bkey))

                usage = msg.get("usage")
                model = msg.get("model")
                if not isinstance(usage, dict) or not model:
                    continue

                i, o, cr, cw = usage_counts(usage)
                rows.append((ts, model, i, o, cr, cw, rkey, message_key(msg, rkey)))
    except OSError:
        return [], [], []
    project = project_name_from_cwd(cwd, fp)
    rows = [(ts, model, project, i, o, cr, cw, rk, mk)
            for (ts, model, i, o, cr, cw, rk, mk) in rows]
    return rows, acts, files


def scan_events(since_ts: float, root: Path | None = None,
                detail_since: float | None = None) -> tuple[list, list, list]:
    """The three event streams (token rows, activity events, mutated files).

    Token `rows` are returned at/after `since_ts`; the activity/file streams are
    returned at/after `detail_since` (defaults to `since_ts`) — the aggregate
    needs token rows for the whole lifetime but only needs activity/file detail
    for the current month, so passing a later `detail_since` keeps those streams
    small. Only files modified at/after `since_ts` are read (an older file can't
    hold an in-window turn), reusing the (size, mtime) per-file cache so one disk
    read feeds every aggregate. Does disk I/O — call it off the UI thread. Pass
    `since_ts=0` for a lifetime scan.

    Duplicates are resolved across the whole scan before returning: a record
    replayed into a second transcript file by a session resume is dropped from
    all three streams, and the several records of one assistant message collapse
    to a single token row. The returned tuples keep their original shapes
    (7 / 2 / 2) — the dedup bookkeeping is stripped here.
    """
    if root is None:
        root = Path.home() / ".claude" / "projects"
    detail = since_ts if detail_since is None else detail_since
    out_acts: list = []
    out_files: list = []
    # message_key -> row, keeping the earliest timestamp and the largest figure
    # per bucket (output_tokens grows across a message's records).
    per_message: dict[str, tuple] = {}
    seen_acts: set[str] = set()
    seen_files: set[str] = set()
    seen: set[str] = set()
    for fp in root.rglob("*.jsonl"):
        try:
            st = fp.stat()
        except OSError:
            continue
        if st.st_mtime < since_ts:
            continue
        key = str(fp)
        seen.add(key)
        cached = _MODEL_EVENT_CACHE.get(key)
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime:
            rows, acts, files = cached[2], cached[3], cached[4]
        else:
            rows, acts, files = _file_model_events(fp)
            _MODEL_EVENT_CACHE[key] = (st.st_size, st.st_mtime, rows, acts, files)
        # Not filtered by record key first — see `account_window_tokens`: a
        # replayed copy can hold the real figures where the original holds zeros,
        # so every record folds into its message under a per-bucket max.
        for ts, model, project, i, o, cr, cw, _rkey, mkey in rows:
            if ts < since_ts:
                continue
            prev = per_message.get(mkey)
            counts = merge_counts(None if prev is None else prev[3:7], (i, o, cr, cw))
            if prev is None:
                per_message[mkey] = (ts, model, project) + counts
            else:
                per_message[mkey] = (min(prev[0], ts), prev[1], prev[2]) + counts
        # A tool call is identified by its record, not its message: two tool_use
        # records under one message are two genuine parallel calls.
        for ts, activity, rkey in acts:
            if ts < detail or rkey in seen_acts:
                continue
            seen_acts.add(rkey)
            out_acts.append((ts, activity))
        for ts, path, rkey in files:
            if ts < detail or rkey in seen_files:
                continue
            seen_files.add(rkey)
            out_files.append((ts, path))
    for key in [k for k in _MODEL_EVENT_CACHE if k not in seen]:
        del _MODEL_EVENT_CACHE[key]
    return list(per_message.values()), out_acts, out_files


def iter_model_events(since_ts: float, root: Path | None = None) -> list:
    """Every (ts, model, project, input, output, cache_read, cache_write)
    assistant turn at/after `since_ts`, across transcripts. Thin wrapper over
    `scan_events` (token rows only)."""
    return scan_events(since_ts, root)[0]


def account_tokens_by_model(since_ts: float, root: Path | None = None) -> dict:
    """Per-model token totals (input, output, cache_read, cache_write) for turns
    at/after `since_ts`. Returns {model_id: {...}}."""
    out: dict[str, dict[str, int]] = {}
    for _ts, model, _project, i, o, cr, cw in iter_model_events(since_ts, root):
        acc = out.setdefault(
            model, {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        acc["input"] += i
        acc["output"] += o
        acc["cache_read"] += cr
        acc["cache_write"] += cw
    return out


# --- last-activity timestamp (immune to mtime touches) ----------------------
# The shelf must decide "is this session active" from the last REAL event inside
# the transcript, not from the file's mtime. A session-maintenance pass can
# rewrite a long-dead transcript — bumping its mtime WITHOUT adding content —
# and keying recency off mtime made those dead sessions resurrect onto the
# shelf (observed: files last active 9-41h ago, rewritten minutes ago, popping
# back as IDLE tiles). Reading the last event's own timestamp is immune to that.
#
# Cache the parsed value by (size, mtime) so an unchanged file is free on the
# next scan; only a file whose stat actually changed re-reads its tail. Keyed by
# path — stale entries for deleted files are a handful of harmless tuples.
_LAST_TS_CACHE: dict[Path, tuple[int, float, float]] = {}


def _read_last_event_ts(path: Path, tail_bytes: int = 262144) -> float | None:
    """Timestamp of the last event line in a transcript, read from its TAIL.

    Transcripts are appended chronologically, so the final timestamped line is
    the most recent activity; only the last chunk is read so a multi-MB file
    costs almost nothing. Returns None when the tail carries no parseable
    timestamp (an empty/partial/rotated file), so the caller can fall back."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)                    # SEEK_END
            fsize = fh.tell()
            fh.seek(max(0, fsize - tail_bytes))
            data = fh.read()
    except OSError:
        return None
    ts = None
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if isinstance(ev, dict):
            t = parse_iso_ts(ev.get("timestamp"))
            if t is not None:
                ts = t
    return ts


def last_activity_ts(path: Path, size: int, mtime: float) -> float:
    """The session's true last-active time: the last event IN the transcript,
    not the file's mtime. Cached by (size, mtime). Falls back to mtime when the
    file has no parseable timestamp, so an unreadable file is never hidden."""
    cached = _LAST_TS_CACHE.get(path)
    if cached is not None and cached[0] == size and cached[1] == mtime:
        return cached[2]
    ts = _read_last_event_ts(path)
    if ts is None:
        ts = mtime
    _LAST_TS_CACHE[path] = (size, mtime, ts)
    return ts


def select_active(
    entries: list[tuple[Path, float]],
    now: float,
    window: float = ACTIVE_WINDOW_SECONDS,
    limit: int = MAX_SESSIONS,
) -> list[Path]:
    """Pick the paths that belong on the shelf, newest-first.

    `entries` is a list of (path, mtime) already gathered by the caller (pure:
    no stat/rglob in here). Drops subagent paths, keeps only files touched within
    `window`, sorts newest-mtime first, and caps to `limit`.
    """
    kept: list[tuple[float, Path]] = []
    for path, mtime in entries:
        if is_subagent_path(path):
            continue
        if now - mtime <= window:
            kept.append((mtime, path))
    kept.sort(key=lambda t: t[0], reverse=True)
    return [path for _, path in kept[:limit]]


def parent_transcript_for_subagent(p: Path) -> Path:
    """Map a subagent path to its parent session transcript.

    …/<proj>/<uuid>/subagents/agent-X.jsonl  ->  …/<proj>/<uuid>.jsonl
    """
    parts = p.parts
    try:
        i = parts.index("subagents")
    except ValueError:
        return p
    return Path(*parts[:i]).with_suffix(".jsonl")


def group_sessions(
    entries: list[tuple[Path, float]],
    now: float,
    window: float = ACTIVE_WINDOW_SECONDS,
    agent_window: float = AGENT_ACTIVE_SECONDS,
    limit: int = MAX_SESSIONS,
    max_agents: int = MAX_AGENTS_PER_SESSION,
) -> list[tuple[Path, list[Path]]]:
    """Group transcripts into sessions for the shelf (pure: no disk I/O).

    Returns [(parent_path, [agent_paths])] for parent sessions active within
    `window`, newest-first, capped at `limit`. A session's recency counts its
    subagents too — the parent transcript freezes while a Task runs — so a
    supervising session stays listed while its agents work. Per session, agents
    whose file was touched within `agent_window` are returned newest-first and
    capped at `max_agents`.
    """
    own_mtime: dict[Path, float] = {}
    agents_by_parent: dict[Path, list[tuple[Path, float]]] = {}
    for path, mtime in entries:
        if is_agent_transcript(path):
            agents_by_parent.setdefault(
                parent_transcript_for_subagent(path), []
            ).append((path, mtime))
        elif not is_subagent_path(path):
            own_mtime[path] = mtime
        # else: subagents/ bookkeeping (journal, workflows tree) — ignored entirely

    # A live agent surfaces its session even if the parent .jsonl wasn't scanned
    # (rotated/removed, or a scan race): synthesize the parent from agent recency.
    for parent, agent_list in agents_by_parent.items():
        own_mtime.setdefault(parent, max(am for _, am in agent_list))

    # Effective recency folds in subagents so a supervising parent stays on top.
    effective: list[tuple[Path, float]] = []
    for parent, mtime in own_mtime.items():
        eff = mtime
        for _, am in agents_by_parent.get(parent, ()):
            if am > eff:
                eff = am
        effective.append((parent, eff))

    groups: list[tuple[Path, list[Path]]] = []
    for parent in select_active(effective, now, window, limit):
        fresh = [
            (ap, am)
            for ap, am in agents_by_parent.get(parent, ())
            if now - am <= agent_window
        ]
        fresh.sort(key=lambda t: t[1], reverse=True)
        groups.append((parent, [ap for ap, _ in fresh[:max_agents]]))
    return groups


def any_agent_active(agents: list[AgentState]) -> bool:
    """True if at least one child agent is doing something (not idle/stale).

    Used to decide whether a session is genuinely *supervising* — only then is a
    frozen/idle parent worth promoting to PLANNING.
    """
    return any(
        not (a.is_stale or a.activity == Activity.IDLE) for a in agents
    )


def _tool_target(name: str, tool_input: dict | None) -> str | None:
    """The 'what' a tool is acting on, from its input — the file being edited,
    the search pattern, the URL host, etc. Returns None when there's no natural
    target, so callers fall back to showing the tool name.
    """
    if not isinstance(tool_input, dict):
        return None
    lower = name.lower()

    def field(key: str) -> str | None:
        v = tool_input.get(key)
        return v.strip() if isinstance(v, str) and v.strip() else None

    if lower in ("read", "edit", "write", "multiedit"):
        p = field("file_path")
        return Path(p).name if p else None
    if lower == "notebookedit":
        p = field("notebook_path") or field("file_path")
        return Path(p).name if p else None
    if lower in ("grep", "glob"):
        return field("pattern")
    if lower in ("bash", "powershell"):
        cmd = field("command")
        return cmd.split()[0] if cmd else None
    if lower == "webfetch":
        url = field("url")
        return url.split("://", 1)[-1].split("/", 1)[0] if url else None
    if lower == "websearch":
        return field("query")
    if lower in ("task", "agent"):
        return field("description") or field("subagent_type")
    return None


def _classify(content: list) -> tuple[Activity, str | None, str | None]:
    """Return (activity, tool_name, target) for the latest assistant content.

    Walks the blocks back-to-front; the last tool_use wins. `target` is what the
    winning tool is acting on (file, pattern, …), or None. If there are no
    tool_use blocks but there are thinking/text blocks, returns THINKING.
    """
    last_tool: str | None = None
    last_input: dict | None = None
    has_thinking_or_text = False
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name")
            if isinstance(name, str):
                last_tool = name
                inp = block.get("input")
                last_input = inp if isinstance(inp, dict) else None
        elif btype in ("thinking", "text"):
            has_thinking_or_text = True
    if last_tool is not None:
        return (_activity_for_tool(last_tool),
                _pretty_tool_name(last_tool),
                _tool_target(last_tool, last_input))
    if has_thinking_or_text:
        return Activity.THINKING, None, None
    return Activity.IDLE, None, None


class _SessionTail:
    """Owns the byte-tailing state + last-known activity for ONE transcript file.

    Keying this per path lets several files tail independently without sharing
    offsets. The tailing logic (offset tracking, partial-last-line hold-back,
    defensive json parsing) is the original single-watcher behaviour, unchanged.
    """

    def __init__(self, path: Path, stale_seconds: float = STALE_SECONDS) -> None:
        self.path = path
        # How long without an append before this tail reports IDLE/stale. Child
        # agents use a shorter threshold so they dim as they wind down.
        self._stale_seconds = stale_seconds
        self.offset = 0
        self.last_activity: Activity | None = None
        self.last_tool: str | None = None
        self.last_target: str | None = None
        self.last_event_ts: float | None = None
        self.cwd: str | None = None
        self.ai_title: str | None = None
        self.custom_title: str | None = None
        self.tokens = TokenUsage()
        # message_key -> the figures already folded into `tokens` for that
        # message, so its later records add only what they add (see
        # `_consume_event`). Reset in lockstep with `tokens`.
        self._counted: dict[str, tuple[int, int, int, int]] = {}
        # Monotonic record counter, used only to give a record with neither a
        # uuid nor a message id an identity that can't collide with another's.
        self._record_seq = 0

    def poll(self, now: float) -> TranscriptState:
        """Read any newly-appended bytes and return this session's current state."""
        path = self.path
        try:
            # One stat() so size and mtime describe the same file state — two
            # calls can straddle an append/rotation and disagree.
            st = path.stat()
            size = st.st_size
            mtime = st.st_mtime
        except OSError:
            # File momentarily unavailable — report what we last knew.
            return self._state(Activity.IDLE, None, is_stale=True)

        if size < self.offset:
            # Truncated/rotated under us; restart from the top. Reset the
            # additive token tally too — without this, re-reading the whole file
            # re-adds every usage block on top of the existing total (the
            # activity/title fields are idempotent on re-read; tokens are not).
            # The per-message bookkeeping MUST reset with it: left behind, every
            # re-read record looks like an already-counted duplicate and the
            # tally stays stuck at zero.
            self.offset = 0
            self.tokens = TokenUsage()
            self._counted = {}
        if size > self.offset:
            self._read_new(size)

        fresh = self.last_event_ts is not None and (now - mtime) <= self._stale_seconds
        if fresh:
            return self._state(self.last_activity or Activity.THINKING, self.last_tool)
        # On the shelf (caller filtered by ACTIVE_WINDOW_SECONDS) but quiet: dim it.
        return self._state(Activity.IDLE, None, is_stale=True)

    def _read_new(self, size: int) -> None:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read(size - self.offset)
        except OSError:
            return

        # If the last byte isn't a newline, that line is still being written.
        # Hold the partial bytes back until the next poll.
        if chunk and not chunk.endswith(b"\n"):
            last_nl = chunk.rfind(b"\n")
            if last_nl < 0:
                return  # incomplete; don't advance offset
            self.offset += last_nl + 1
            chunk = chunk[: last_nl + 1]
        else:
            self.offset = size

        text = chunk.decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            self._consume_event(ev)

    def _consume_event(self, ev: dict) -> None:
        # Title events are their own line types (no message/cwd) and are
        # re-emitted as the title changes, so the latest one seen wins. A
        # user-set custom title can also be cleared, so an empty value resets
        # it (falling back to the auto title / cwd).
        etype = ev.get("type")
        if etype == "custom-title":
            ct = ev.get("customTitle")
            self.custom_title = ct.strip() if isinstance(ct, str) and ct.strip() else None
            return
        if etype == "ai-title":
            at = ev.get("aiTitle")
            if isinstance(at, str) and at.strip():
                self.ai_title = at.strip()
            return

        # cwd rides BOTH user and assistant events, so capture it from any event
        # — the tile can be named before the model's first reply.
        cwd = ev.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.cwd = cwd

        msg = ev.get("message")
        if not isinstance(msg, dict):
            return
        if msg.get("role") != "assistant":
            return
        # Token usage rides EVERY record of an assistant message — Claude Code
        # writes one message as several records, one per content block, each
        # repeating the same `message.id` with its own snapshot of `usage`. So
        # fold per message rather than summing per record, and do it before the
        # activity early-return (a text/thinking record carries usage too).
        usage = msg.get("usage")
        if isinstance(usage, dict):
            self._record_seq += 1
            rkey = record_key(ev, f"seq:{self._record_seq}")
            mkey = message_key(msg, rkey)
            new = merge_counts(self._counted.get(mkey), usage_counts(usage))
            if new != self._counted.get(mkey):
                # Add only the increase, so the running tally stays correct
                # without re-reading anything already counted.
                old = self._counted.get(mkey) or (0, 0, 0, 0)
                self.tokens.add_counts(tuple(n - o for n, o in zip(new, old)))
                self._counted[mkey] = new
        content = msg.get("content")
        if not isinstance(content, list):
            return
        activity, tool, target = _classify(content)
        if activity == Activity.IDLE:
            return
        self.last_activity = activity
        self.last_tool = tool
        self.last_target = target
        # Use the event's OWN timestamp (truthful "last active") rather than
        # wall-clock-at-read — otherwise restarting the app reads the whole
        # backlog and stamps every old action as "just now". Fall back to
        # wall-clock only if the timestamp is missing/unparseable. Staleness
        # still keys off the file's mtime in poll(); this only drives display.
        self.last_event_ts = parse_iso_ts(ev.get("timestamp")) or time.time()

    def _state(
        self, activity: Activity, tool: str | None, is_stale: bool = False
    ) -> TranscriptState:
        return TranscriptState(
            activity=activity,
            tool_name=tool if activity != Activity.IDLE else None,
            target=self.last_target if activity != Activity.IDLE else None,
            transcript_path=self.path,
            last_event_ts=self.last_event_ts,
            session_id=self.path.stem,
            cwd=self.cwd,
            project_name=session_label(
                self.custom_title, self.ai_title, self.cwd, self.path
            ),
            is_stale=is_stale,
            # Snapshot so the emitted state doesn't share the tail's mutable
            # tally (which keeps growing on later polls).
            tokens=replace(self.tokens),
        )


def _idle_state() -> TranscriptState:
    """The empty-shelf state — lets the dashboard fall back to rate-based mood."""
    return TranscriptState(
        activity=Activity.IDLE,
        tool_name=None,
        transcript_path=None,
        last_event_ts=None,
    )


class TranscriptWatcher(QObject):
    """Tails every recently-active Claude Code transcript and emits their states.

    Emits the full shelf via `sessions_changed`; `state_changed` keeps the old
    single-mascot contract by carrying the focused (newest) session.
    """

    sessions_changed = Signal(object)  # list[TranscriptState], newest-first
    state_changed = Signal(object)     # BACK-COMPAT: focused state, or IDLE when empty

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._tails: dict[Path, _SessionTail] = {}
        # Discovery groups (newest-first): (parent_path, [agent_paths]). The first
        # group's parent is the "focused" session for the back-compat signal.
        self._groups: list[tuple[Path, list[Path]]] = []
        self._poll_count = 0

    def start(self) -> None:
        self._rescan()
        self._poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _scan_entries(self) -> list[tuple[Path, float]]:
        """Gather (path, recency) for every transcript on disk (the I/O half).

        For a parent SESSION, `recency` is the last real EVENT timestamp — NOT
        the file mtime — so a maintenance pass that rewrites a dead transcript
        without adding content can't bump it back onto the shelf. Only files
        whose mtime is recent enough to matter are read (an old mtime already
        rules a file out, and mtime is never earlier than the last event), so
        the tail reads stay confined to the handful of candidate files.

        Subagent transcripts (and the subagents/ bookkeeping tree) keep mtime:
        they're short-lived and written continuously while the agent works, and
        group_sessions folds/ignores them anyway.
        """
        if not TRANSCRIPTS_DIR.exists():
            return []
        now = time.time()
        entries: list[tuple[Path, float]] = []
        for p in TRANSCRIPTS_DIR.rglob("*.jsonl"):
            try:
                st = p.stat()
            except OSError:
                continue
            m = st.st_mtime
            if is_subagent_path(p) or now - m > ACTIVE_WINDOW_SECONDS:
                # Agent/bookkeeping, or a stale mtime that already excludes it —
                # no need to read content in either case.
                entries.append((p, m))
            else:
                # A shelf candidate: judge it by its real last-event time so a
                # touched-but-dead transcript is dropped despite its fresh mtime.
                entries.append((p, last_activity_ts(p, st.st_size, m)))
        return entries

    def _rescan(self) -> None:
        """Refresh the set of tailed files: add new ones, drop those that left.

        Tails both parent transcripts and their live subagent transcripts.
        """
        self._groups = group_sessions(self._scan_entries(), time.time())
        parents: set[Path] = set()
        agent_paths: set[Path] = set()
        for parent, agents in self._groups:
            parents.add(parent)
            agent_paths.update(agents)
        wanted = parents | agent_paths
        # Drop tails whose file fell out of the window so their offsets are freed.
        for path in list(self._tails):
            if path not in wanted:
                del self._tails[path]
        # Agent tails dim sooner than parent tails (AGENT_STALE_SECONDS).
        for path in parents:
            if path not in self._tails:
                self._tails[path] = _SessionTail(path)
        for path in agent_paths:
            if path not in self._tails:
                self._tails[path] = _SessionTail(path, AGENT_STALE_SECONDS)

    def _poll(self) -> None:
        self._poll_count += 1
        if self._poll_count % RESCAN_DIR_EVERY_N_POLLS == 0:
            self._rescan()

        now = time.time()
        states: list[TranscriptState] = []
        for parent, agent_paths in self._groups:
            tail = self._tails.get(parent)
            if tail is None:
                continue
            state = tail.poll(now)
            agents: list[AgentState] = []
            for ap in agent_paths:
                atail = self._tails.get(ap)
                if atail is None:
                    continue
                a = atail.poll(now)
                agents.append(AgentState(
                    agent_id=ap.stem,
                    activity=a.activity,
                    tool_name=a.tool_name,
                    is_stale=a.is_stale,
                ))
            if agents:
                state.agents = agents
                # The parent transcript freezes during a Task call, so a session
                # that's only supervising would read as idle. Show it PLANNING —
                # but only while a child is actually working, so a row of
                # winding-down agents doesn't paint a falsely-active parent.
                if (state.is_stale or state.activity == Activity.IDLE) and any_agent_active(agents):
                    state.activity = Activity.PLANNING
                    state.tool_name = None
                    state.is_stale = False
            states.append(state)

        self.sessions_changed.emit(states)
        self.state_changed.emit(states[0] if states else _idle_state())
