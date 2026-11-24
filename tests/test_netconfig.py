"""Work-network setup: system certificates, Claude Code's proxy settings, system proxy.

Everything here runs against a plain dict standing in for os.environ, so no
test changes the real environment of the test run.
"""

from __future__ import annotations

import json
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import netconfig  # noqa: E402


# --- Claude Code's env settings ---------------------------------------------

def test_env_block_keeps_only_string_values():
    raw = json.dumps({"env": {"HTTPS_PROXY": "http://proxy:8080", "N": 3, "EMPTY": ""},
                      "permissions": {}})
    assert netconfig._env_block(raw) == {"HTTPS_PROXY": "http://proxy:8080"}


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"env": "x"}', '{"other": 1}'])
def test_env_block_is_empty_for_anything_unusable(raw):
    assert netconfig._env_block(raw) == {}


def test_fill_never_overwrites_the_users_environment():
    environ = {"HTTPS_PROXY": "http://mine:1"}
    sources = [("managed", {"HTTPS_PROXY": "http://it:2", "NO_PROXY": ".corp"})]
    filled = netconfig.fill_from_claude_code(environ, sources)
    assert environ["HTTPS_PROXY"] == "http://mine:1"
    assert environ["NO_PROXY"] == ".corp"
    assert filled == {"NO_PROXY": "managed"}


def test_managed_settings_win_over_user_settings():
    environ: dict[str, str] = {}
    sources = [("managed", {"HTTPS_PROXY": "http://it:2"}),
               ("user", {"HTTPS_PROXY": "http://user:3", "NODE_EXTRA_CA_CERTS": "/ca.pem"})]
    filled = netconfig.fill_from_claude_code(environ, sources)
    assert environ == {"HTTPS_PROXY": "http://it:2", "NODE_EXTRA_CA_CERTS": "/ca.pem"}
    assert filled == {"HTTPS_PROXY": "managed", "NODE_EXTRA_CA_CERTS": "user"}


def test_only_network_variables_are_copied():
    environ: dict[str, str] = {}
    netconfig.fill_from_claude_code(environ, [("user", {"ANTHROPIC_API_KEY": "sk-x",
                                                        "DISABLE_TELEMETRY": "1"})])
    assert environ == {}


def test_user_settings_follow_claude_config_dir(tmp_path, monkeypatch):
    (tmp_path / "settings.json").write_text(
        json.dumps({"env": {"HTTPS_PROXY": "http://cfg:9"}}), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(netconfig, "MANAGED_SETTINGS_FILES",
                        {k: tmp_path / "absent.json" for k in ("win32", "darwin", "linux")})
    monkeypatch.setattr(netconfig, "_read_managed_registry", lambda: None)
    assert netconfig.claude_code_env_sources() == [
        ("Claude Code settings", {"HTTPS_PROXY": "http://cfg:9"})]


def test_settings_file_with_a_bom_still_reads(tmp_path):
    # Notepad on Windows saves UTF-8 with a byte-order mark.
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"env": {"NO_PROXY": "x"}}).encode())
    assert netconfig._env_block(netconfig._read_text(path)) == {"NO_PROXY": "x"}


# --- system proxy -----------------------------------------------------------

def test_system_proxy_fills_in_when_nothing_is_set():
    environ: dict[str, str] = {}
    got = netconfig.fill_system_proxy(
        environ,
        getproxies=lambda: {"http": "http://sys:80", "https": "http://sys:80"},
        bypass=lambda: "localhost,.corp")
    assert got == "http://sys:80"
    assert environ == {"HTTPS_PROXY": "http://sys:80", "HTTP_PROXY": "http://sys:80",
                       "NO_PROXY": "localhost,.corp"}


def test_system_proxy_is_ignored_when_a_proxy_is_already_set():
    environ = {"https_proxy": "http://env:1"}
    assert netconfig.fill_system_proxy(environ, getproxies=lambda: {"https": "http://sys:80"},
                                       bypass=lambda: None) is None
    assert environ == {"https_proxy": "http://env:1"}


def test_no_system_proxy_leaves_the_environment_alone():
    environ: dict[str, str] = {}
    assert netconfig.fill_system_proxy(environ, getproxies=dict, bypass=lambda: None) is None
    assert environ == {}


def test_a_failing_system_lookup_is_not_fatal():
    def boom():
        raise OSError("registry unavailable")
    assert netconfig.fill_system_proxy({}, getproxies=boom, bypass=lambda: None) is None


def test_existing_no_proxy_is_kept():
    environ = {"no_proxy": "mine"}
    netconfig.fill_system_proxy(environ, getproxies=lambda: {"https": "http://sys:80"},
                                bypass=lambda: "theirs")
    assert environ["no_proxy"] == "mine" and "NO_PROXY" not in environ


@pytest.mark.parametrize("override, expected", [
    ("<local>", "localhost,127.0.0.1,::1"),
    ("*.corp.example;intranet", ".corp.example,intranet"),
    ("10.*;192.168.*", ""),                # no NO_PROXY equivalent: dropped, not guessed
    ("<local>;localhost;*.a.b", "localhost,127.0.0.1,::1,.a.b"),
    ("", ""),
])
def test_bypass_list_translation(override, expected):
    assert netconfig.windows_bypass_to_no_proxy(override) == expected


# --- CA file ----------------------------------------------------------------

def test_extra_ca_becomes_ssl_cert_file_on_top_of_the_system_store(tmp_path):
    ca = tmp_path / "corp.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n", encoding="ascii")
    environ = {"NODE_EXTRA_CA_CERTS": str(ca)}
    assert netconfig.fill_ca_file(environ, system_certs=True)
    assert environ["SSL_CERT_FILE"] == str(ca)


def test_extra_ca_is_not_used_without_the_system_store(tmp_path):
    # SSL_CERT_FILE replaces httpx's bundled roots, so a file with only the
    # company's CA would break every public site.
    ca = tmp_path / "corp.pem"
    ca.write_text("x", encoding="ascii")
    environ = {"NODE_EXTRA_CA_CERTS": str(ca)}
    assert not netconfig.fill_ca_file(environ, system_certs=False)
    assert "SSL_CERT_FILE" not in environ


def test_extra_ca_never_replaces_an_explicit_ssl_cert_file(tmp_path):
    environ = {"NODE_EXTRA_CA_CERTS": str(tmp_path), "SSL_CERT_FILE": "/mine.pem"}
    assert not netconfig.fill_ca_file(environ, system_certs=True)
    assert environ["SSL_CERT_FILE"] == "/mine.pem"


def test_missing_extra_ca_file_is_ignored(tmp_path):
    environ = {"NODE_EXTRA_CA_CERTS": str(tmp_path / "gone.pem")}
    assert not netconfig.fill_ca_file(environ, system_certs=True)


# --- system certificates and the whole thing --------------------------------

def test_system_certificates_can_be_switched_off(monkeypatch):
    monkeypatch.setenv(netconfig.SYSTEM_CERTS_ENV, "0")
    assert netconfig.use_system_certificates() is False


def test_system_certificates_inject_into_ssl(monkeypatch):
    import truststore
    monkeypatch.delenv(netconfig.SYSTEM_CERTS_ENV, raising=False)
    original = ssl.SSLContext
    try:
        assert netconfig.use_system_certificates() is True
        assert ssl.SSLContext is truststore.SSLContext
    finally:
        truststore.extract_from_ssl()
    assert ssl.SSLContext is original


def test_apply_never_raises(monkeypatch):
    monkeypatch.setattr(netconfig, "use_system_certificates", lambda: False)

    def boom():
        raise RuntimeError("bad settings")
    monkeypatch.setattr(netconfig, "claude_code_env_sources", boom)
    setup = netconfig.apply({})
    assert setup.system_certs is False and setup.filled == {}


def test_summary_hides_proxy_credentials():
    setup = netconfig.NetworkSetup(system_certs=True, system_proxy="http://me:secret@proxy:8080")
    assert "secret" not in setup.summary()
    assert "http://proxy:8080" in setup.summary()
