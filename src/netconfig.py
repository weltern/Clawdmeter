"""Network setup for work networks: certificates and proxy.

The goal is one promise: if Claude Code works on this machine, Clawdmeter works
too. Out of the box httpx trusts only its bundled certifi certificates and only
the HTTPS_PROXY-style environment variables, which breaks on two very common
corporate setups:

  * A proxy that inspects TLS (Zscaler, Netskope, Palo Alto, ...). It re-signs
    every connection with the company's own root certificate, which IT installs
    in the Windows / macOS certificate store. certifi has never heard of it, so
    every request failed a certificate check and the dashboard said "offline".
  * A proxy set in the system's network settings rather than the environment.
    An app started from the Run key or the Start menu usually has no proxy
    variables at all, so it could not get out.

apply() runs once at startup, before any request, and fixes both without
touching the code that makes requests:

  1. Trust the operating system's certificate store (the `truststore` package),
     as Claude Code does by default. certifi's roots still load on top.
  2. Fill in proxy and CA settings that are not already in the environment,
     from Claude Code's own `env` settings: the managed (IT-deployed) settings
     first, then the user's ~/.claude/settings.json. Whatever IT configured for
     Claude Code then applies here too.
  3. If there is still no proxy, use the system proxy (Windows Internet
     Options, macOS network settings), with its bypass list.

Everything is filled in through os.environ, which httpx reads whenever a client
is created, so every existing httpx.Client in the app picks it up as is. A
variable that is already set is never overwritten: the user's own environment
always wins.

Not covered: PAC / WPAD auto-configuration scripts, and proxies that require
NTLM or Kerberos sign-in. Claude Code supports neither, and points such
networks at an LLM gateway or a local authenticating proxy instead.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# The variables copied from Claude Code's settings. Claude Code checks the
# lowercase proxy names first, so both spellings are recognised.
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
NO_PROXY_VARS = ("NO_PROXY", "no_proxy")
# Claude Code's extra-CA variable; httpx reads SSL_CERT_FILE instead.
CA_VAR = "NODE_EXTRA_CA_CERTS"
COPIED_VARS = PROXY_VARS + NO_PROXY_VARS + (CA_VAR,)

# Where Claude Code reads managed settings (code.claude.com/docs/en/managed-settings).
MANAGED_SETTINGS_FILES = {
    "win32": Path(r"C:\Program Files\ClaudeCode\managed-settings.json"),
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    "linux": Path("/etc/claude-code/managed-settings.json"),
}
MANAGED_SETTINGS_REG_KEY = r"SOFTWARE\Policies\ClaudeCode"
MANAGED_SETTINGS_REG_VALUE = "Settings"

# Off switch for the system certificate store, in case it ever misbehaves on a
# machine: set CLAWDMETER_SYSTEM_CERTS=0 to go back to certifi only.
SYSTEM_CERTS_ENV = "CLAWDMETER_SYSTEM_CERTS"


@dataclass
class NetworkSetup:
    """What apply() did, for the log and for tests."""

    system_certs: bool = False
    # variable name -> where its value came from ("managed settings", ...)
    filled: dict[str, str] = field(default_factory=dict)
    system_proxy: str | None = None

    def summary(self) -> str:
        parts = ["system certificates" if self.system_certs else "bundled certificates only"]
        for name, source in self.filled.items():
            parts.append(f"{name} from {source}")
        if self.system_proxy:
            parts.append(f"system proxy {_redact(self.system_proxy)}")
        return "; ".join(parts)


def _redact(url: str) -> str:
    """A proxy URL without any user:password in it, for the log."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        scheme, rest = "", url
    host = rest.rpartition("@")[2]
    return f"{scheme}://{host}" if sep else host


# --- system certificate store -----------------------------------------------

def use_system_certificates() -> bool:
    """Make every TLS connection trust the OS certificate store. False if not."""
    if os.environ.get(SYSTEM_CERTS_ENV, "").strip() == "0":
        return False
    try:
        import truststore
    except ImportError:
        return False
    try:
        truststore.inject_into_ssl()
    except Exception as exc:  # never let this stop the app from starting
        log.warning("Could not use the system certificate store: %s", exc)
        return False
    return True


# --- Claude Code's env settings ---------------------------------------------

def _platform_key() -> str:
    if sys.platform.startswith("win"):
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _env_block(raw: str | None) -> dict[str, str]:
    """The string values of a settings JSON's `env` object; {} for anything else."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return {}
    return {k: v for k, v in env.items() if isinstance(k, str) and isinstance(v, str) and v}


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError:
        return None


def _read_managed_registry() -> str | None:
    """The managed settings JSON from HKLM, else HKCU, policies (Windows only)."""
    try:
        import winreg
    except ImportError:
        return None
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, MANAGED_SETTINGS_REG_KEY) as key:
                value, _ = winreg.QueryValueEx(key, MANAGED_SETTINGS_REG_VALUE)
        except OSError:
            continue
        if isinstance(value, str) and value.strip():
            return value
    return None


def claude_config_dir() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".claude"


def claude_code_env_sources() -> list[tuple[str, dict[str, str]]]:
    """Claude Code's `env` settings, highest priority first, as (source, env)."""
    sources: list[tuple[str, dict[str, str]]] = []
    if _platform_key() == "win32":
        sources.append(("Claude Code managed settings (registry)",
                        _env_block(_read_managed_registry())))
    managed = MANAGED_SETTINGS_FILES[_platform_key()]
    sources.append(("Claude Code managed settings",
                    _env_block(_read_text(managed))))
    sources.append(("Claude Code settings",
                    _env_block(_read_text(claude_config_dir() / "settings.json"))))
    return [(name, env) for name, env in sources if env]


def fill_from_claude_code(environ, sources) -> dict[str, str]:
    """Copy proxy / CA variables that ``environ`` lacks. Returns name -> source."""
    filled: dict[str, str] = {}
    for source, env in sources:
        for name in COPIED_VARS:
            if name in env and not environ.get(name) and name not in filled:
                environ[name] = env[name]
                filled[name] = source
    return filled


# --- system proxy -----------------------------------------------------------

def has_proxy(environ) -> bool:
    return any(environ.get(name) for name in PROXY_VARS)


def windows_bypass_to_no_proxy(override: str) -> str:
    """A system bypass list -> a NO_PROXY value.

    Internet Options' "<local>;*.corp.example;10.*" ->
    "localhost,127.0.0.1,::1,.corp.example". macOS exceptions use the same shapes.
    Wildcards other than a leading "*." have no NO_PROXY equivalent and are
    dropped rather than guessed at.
    """
    out: list[str] = []
    for entry in override.replace(",", ";").split(";"):
        entry = entry.strip()
        if not entry:
            continue
        if entry.lower() == "<local>":
            out += ["localhost", "127.0.0.1", "::1"]
        elif entry.startswith("*.") and "*" not in entry[2:]:
            out.append(entry[1:])
        elif "*" not in entry:
            out.append(entry)
    seen: dict[str, None] = dict.fromkeys(out)
    return ",".join(seen)


def _system_proxy_bypass() -> str | None:
    """The system proxy's bypass list as NO_PROXY, or None (Windows and macOS)."""
    if sys.platform == "darwin":
        try:
            from _scproxy import _get_proxy_settings
            settings = _get_proxy_settings()
        except Exception:
            return None
        hosts = [h for h in settings.get("exceptions", ()) if isinstance(h, str)]
        if settings.get("exclude_simple"):
            hosts = ["localhost", "127.0.0.1", "::1", *hosts]
        return windows_bypass_to_no_proxy(";".join(hosts)) or None
    raw = _windows_proxy_bypass()
    return windows_bypass_to_no_proxy(raw) or None if raw else None


def _windows_proxy_bypass() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            value, _ = winreg.QueryValueEx(key, "ProxyOverride")
    except OSError:
        return None
    return value if isinstance(value, str) else None


def fill_system_proxy(environ, *, getproxies=urllib.request.getproxies,
                      bypass=_system_proxy_bypass) -> str | None:
    """Use the system proxy when the environment names none. Returns it, or None.

    urllib's getproxies() already reads Internet Options on Windows and the
    network settings on macOS (and only the environment on Linux).
    """
    if has_proxy(environ):
        return None
    try:
        proxies = getproxies() or {}
    except Exception:  # registry / SystemConfiguration oddities
        return None
    https = proxies.get("https") or proxies.get("http")
    if not https:
        return None
    environ["HTTPS_PROXY"] = https
    if proxies.get("http") and not environ.get("HTTP_PROXY"):
        environ["HTTP_PROXY"] = proxies["http"]
    if not any(environ.get(n) for n in NO_PROXY_VARS):
        no_proxy = bypass()
        if no_proxy:
            environ["NO_PROXY"] = no_proxy
    return https


# --- CA file ----------------------------------------------------------------

def fill_ca_file(environ, *, system_certs: bool) -> bool:
    """Point httpx at Claude Code's extra CA file. True if it did.

    httpx reads SSL_CERT_FILE, and Claude Code (Node) reads NODE_EXTRA_CA_CERTS.
    Only done on top of the system store: without it, SSL_CERT_FILE *replaces*
    httpx's bundled roots, so a file holding just the company's CA would break
    every public site.
    """
    extra = environ.get(CA_VAR)
    if not system_certs or not extra or environ.get("SSL_CERT_FILE"):
        return False
    if not Path(extra).is_file():
        return False
    environ["SSL_CERT_FILE"] = extra
    return True


# --- entry point ------------------------------------------------------------

def apply(environ=None) -> NetworkSetup:
    """Set up certificates and proxy for every request. Never raises."""
    environ = os.environ if environ is None else environ
    setup = NetworkSetup()
    try:
        setup.system_certs = use_system_certificates()
        setup.filled = fill_from_claude_code(environ, claude_code_env_sources())
        setup.system_proxy = fill_system_proxy(environ)
        if fill_ca_file(environ, system_certs=setup.system_certs):
            setup.filled["SSL_CERT_FILE"] = CA_VAR
    except Exception as exc:  # a bad setting must not stop the app starting
        log.warning("Network setup incomplete: %s", exc)
    log.info("Network: %s", setup.summary())
    return setup
