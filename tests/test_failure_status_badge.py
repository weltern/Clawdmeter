"""A failed poll must SAY what failed, in the window as well as the tray.

The bug these guard: every probe failure used to collapse into the status
string "error", and `_apply_status_badge` had no branch for it — so it fell to
the else branch and *cleared* the badge. An expired OAuth token therefore
rendered as the 5h / 7d bars silently frozen on their last good values, with
nothing on screen to say why. That is exactly how a token that expired at
02:42 went unnoticed for 34 hours.

The split between kinds matters as much as the wording: "Token expired" names
an action the user can take, "Offline" says to wait.

Run with `python -m pytest tests/ -q`.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx  # noqa: E402
import pytest  # noqa: E402

import dashboard  # noqa: E402
import poller  # noqa: E402


# --------------------------------------------------------------------------
# poller: the failure KIND survives out of _poll_once
# --------------------------------------------------------------------------

def _patch_transport(monkeypatch, handler) -> None:
    class _FakeClient(httpx.Client):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(poller.httpx, "Client", _FakeClient)


@pytest.mark.parametrize("code, expected", [
    (401, poller.STATUS_AUTH_EXPIRED),
    (403, poller.STATUS_AUTH_EXPIRED),
    (429, poller.STATUS_HTTP_ERROR),
    (500, poller.STATUS_HTTP_ERROR),
])
def test_poll_once_classifies_http_failures(monkeypatch, code, expected):
    _patch_transport(monkeypatch, lambda request: httpx.Response(code, json={}))
    sample = poller._poll_once("fake-token")

    assert sample.ok is False
    assert sample.status == expected


def test_poll_once_reports_transport_failure_as_offline(monkeypatch):
    """A timeout is not an auth problem and must not read as one."""
    def handler(request):
        raise httpx.ConnectTimeout("no route to host")

    _patch_transport(monkeypatch, handler)
    sample = poller._poll_once("fake-token")

    assert sample.ok is False
    assert sample.status == poller.STATUS_OFFLINE


# --------------------------------------------------------------------------
# dashboard: the badge renders that kind instead of going blank
# --------------------------------------------------------------------------

class _FakeStyle:
    def unpolish(self, widget) -> None: ...
    def polish(self, widget) -> None: ...


class _FakeLabel:
    def __init__(self) -> None:
        self.text = None
        self.props: dict = {}
        self._style = _FakeStyle()

    def setText(self, text) -> None:
        self.text = text

    def clear(self) -> None:
        self.text = ""

    def setProperty(self, key, value) -> None:
        self.props[key] = value

    def style(self):
        return self._style


class _FakeContainer:
    def __init__(self) -> None:
        self.visible = None

    def setVisible(self, value) -> None:
        self.visible = value


class _FakeDashboard:
    """Just enough surface for the real method, bound below.

    Binding the real `_apply_status_badge` rather than re-implementing its rules
    is the point: a test that restates the mapping would pass against the bug.
    """

    # The genuine method under test, not a copy of its logic.
    _apply_status_badge = dashboard.Dashboard._apply_status_badge

    def __init__(self) -> None:
        self.status_text = _FakeLabel()
        self.status_icon = _FakeLabel()
        self.status_container = _FakeContainer()


def _badge(status: str) -> _FakeDashboard:
    fake = _FakeDashboard()
    fake._apply_status_badge(status)
    return fake


def test_expired_token_names_itself_in_the_badge():
    fake = _badge(poller.STATUS_AUTH_EXPIRED)

    assert fake.status_container.visible is True
    assert fake.status_text.text == "Token expired"
    assert fake.status_text.props["level"] == "block"


def test_missing_token_is_distinct_from_an_expired_one():
    assert _badge(poller.STATUS_NO_TOKEN).status_text.text == "No token found"


def test_network_failure_does_not_blame_the_token():
    fake = _badge(poller.STATUS_OFFLINE)

    assert fake.status_container.visible is True
    assert fake.status_text.text == "Offline"
    assert "token" not in (fake.status_text.text or "").lower()


def test_api_failure_is_a_warning_not_an_auth_error():
    fake = _badge(poller.STATUS_HTTP_ERROR)

    assert fake.status_text.text == "API error"
    assert fake.status_text.props["level"] == "warn"


def test_every_failure_status_has_a_badge():
    """A new STATUS_* with no entry would silently render as nothing again."""
    declared = {
        value for name, value in vars(poller).items()
        if name.startswith("STATUS_") and isinstance(value, str)
    }

    assert declared, "no STATUS_* constants found — the probe is broken"
    assert declared <= set(dashboard.FAILURE_BADGES)


def test_only_styled_levels_are_used():
    """theme.py styles `warn` and `block` only; a third would render unstyled."""
    levels = {level for _, _, level in dashboard.FAILURE_BADGES.values()}

    assert levels <= {"warn", "block"}


# --------------------------------------------------------------------------
# the rate-limit path still works — these statuses are NOT failures
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status, expected", [
    ("rejecting", "Limit reached"),
    ("allowed_warning", "Nearing limit"),
])
def test_rate_limit_statuses_are_unaffected(status, expected):
    assert _badge(status).status_text.text == expected


def test_allowed_still_clears_the_badge():
    fake = _badge("allowed")

    assert fake.status_container.visible is False
    assert fake.status_text.text == ""
