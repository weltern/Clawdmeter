"""Re-authenticate by handing off to the Claude Code CLI.

Clawdmeter has no account of its own: it reads the OAuth token out of the file
Claude Code owns. So when the stored refresh token dies, the only thing that can
mint a new one is a fresh sign-in through Claude Code itself.

This module launches `claude auth login` rather than reimplementing that flow.
Reimplementing it would mean guessing an authorize endpoint, a redirect URI and
a scope set that are documented nowhere we can check, in the one part of the app
where being wrong is worst. Shelling out also adds no new dependency: the
credentials file Clawdmeter reads is already Claude Code's, so if the CLI is
absent there was never a token to read.

Nothing here touches the credentials file. The CLI writes it; the poller
notices on its next cycle because it re-reads the expiry every time.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

CLI_NAME = "claude"
# Where the native installer puts it when it is not on PATH.
FALLBACK_BINS = (
    Path.home() / ".local" / "bin" / "claude.exe",
    Path.home() / ".local" / "bin" / "claude",
)

# --claudeai is the subscription flow (the default, stated explicitly so a
# change of default upstream cannot silently send anyone to Console billing).
LOGIN_ARGS = ("auth", "login", "--claudeai")


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _is_macos() -> bool:
    return sys.platform == "darwin"


def cli_path() -> str | None:
    """Absolute path to the Claude Code CLI, or None if it isn't installed."""
    found = shutil.which(CLI_NAME)
    if found:
        return found
    for candidate in FALLBACK_BINS:
        if candidate.is_file():
            return str(candidate)
    return None


def is_available() -> bool:
    return cli_path() is not None


def login_argv() -> list[str] | None:
    """The exact command a sign-in runs, or None when the CLI is missing."""
    exe = cli_path()
    if exe is None:
        return None
    return [exe, *LOGIN_ARGS]


def login_command_text() -> str:
    """The command to show the user when we cannot run it for them.

    Deliberately the bare `claude ...` form rather than the resolved absolute
    path: this is for a human to type, and it is what the docs would say.
    """
    return f"{CLI_NAME} " + " ".join(LOGIN_ARGS)


def _spawn_windows(argv: list[str]) -> None:
    # A GUI process has no console, and `claude auth login` is interactive, so
    # it needs one of its own or the user sees nothing happen at all.
    subprocess.Popen(argv, creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))


SPAWN_GRACE_SECONDS = 0.4


def _survived(proc: subprocess.Popen) -> bool:
    """Did the spawn actually take?

    Covers both launcher shapes without needing to know which is which:
      * a terminal that stays in the foreground (xterm) is still running when
        the grace period elapses — success;
      * one that hands off to a server and exits (gnome-terminal, and osascript)
        returns 0 — success;
      * one that rejected how it was invoked exits non-zero almost at once —
        failure, and worth trying the next candidate.

    Without this, a terminal that opened but never ran the command still
    reported "finish in the window that just opened", which is the worst
    available outcome for a remedy of last resort.
    """
    try:
        proc.wait(timeout=SPAWN_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return True
    return proc.returncode == 0


def _applescript_string(argv: list[str]) -> str:
    """argv as a shell command, escaped to survive an AppleScript literal.

    Two layers: shlex for the shell inside Terminal, then backslash and quote
    escaping for the AppleScript string wrapping it. A path containing a double
    quote would otherwise end the literal early.
    """
    command = " ".join(shlex.quote(a) for a in argv)
    return command.replace("\\", "\\\\").replace('"', '\\"')


def _spawn_macos(argv: list[str]) -> None:
    script = f'tell application "Terminal" to do script "{_applescript_string(argv)}"'
    proc = subprocess.Popen(["osascript", "-e", script])
    if not _survived(proc):
        raise OSError("osascript could not drive Terminal")


# (binary, the flag that makes it take a command PLUS ITS ARGUMENTS as argv).
#
# The flag is the whole point, and it is not uniform:
#   gnome-terminal --      everything after -- is the command
#   konsole -e             takes the command and its arguments
#   xfce4-terminal -x      --execute, "the remainder of the command line".
#                          Its -e wants ONE quoted string instead, which is the
#                          trap: passing argv to -e opens a terminal that never
#                          runs the command.
#   xterm -e               takes the command and its arguments
#   x-terminal-emulator    the Debian alternative — behaviour depends on
#                          whichever terminal it points at, so it is the last
#                          resort rather than, as before, the first choice.
LINUX_TERMINALS = (
    ("gnome-terminal", "--"),
    ("konsole", "-e"),
    ("xfce4-terminal", "-x"),
    ("xterm", "-e"),
    ("x-terminal-emulator", "-e"),
)


def _spawn_linux(argv: list[str]) -> None:
    tried = []
    for term, flag in LINUX_TERMINALS:
        exe = shutil.which(term)
        if not exe:
            continue
        tried.append(term)
        if _survived(subprocess.Popen([exe, flag, *argv])):
            return
    if tried:
        raise OSError("no terminal would run the command (tried "
                      + ", ".join(tried) + ")")
    raise FileNotFoundError("no terminal emulator found")


def start_login() -> tuple[bool, str]:
    """Open a sign-in. Returns (started, message) — never raises.

    A False here is not a failure to report as a bug: the message is written to
    be shown to the user, and always ends up telling them the command to run,
    because a re-auth path that dead-ends is the same defect this whole feature
    exists to remove.
    """
    argv = login_argv()
    if argv is None:
        return False, (
            "Claude Code isn't on PATH. Sign in with: "
            f"{login_command_text()}"
        )
    try:
        if _is_windows():
            _spawn_windows(argv)
        elif _is_macos():
            _spawn_macos(argv)
        else:
            _spawn_linux(argv)
    except (OSError, ValueError) as exc:
        return False, (
            f"Couldn't open a terminal ({exc}). Sign in with: "
            f"{login_command_text()}"
        )
    return True, (
        "Signing in with Claude Code — finish in the window that just opened. "
        "Clawdmeter picks the new token up on its next poll."
    )
