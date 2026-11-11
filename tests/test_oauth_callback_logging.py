"""OAuth callbacks log their outcome without codes, tokens, state values or URLs."""

from __future__ import annotations

import logging

from test_strava_portal_oauth import _connect, strava_oauth  # noqa: F401

SECRET_CODE = "sekritcode-9f8e7d"


def _callback_logs(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "OAuth callback" in r.getMessage()]


def _assert_clean(caplog, *secrets: str) -> None:
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in secrets:
        assert secret not in text


def test_connected_is_logged_without_secrets(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, portal, public = strava_oauth
    state, _url = _connect(portal)
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": SECRET_CODE, "scope": "read,activity:write"},
        follow_redirects=False,
    )
    logs = _callback_logs(caplog)
    assert logs and "outcome=" in logs[-1] and "has_code=True" in logs[-1]
    _assert_clean(caplog, state, SECRET_CODE, "ssecret")


def test_missing_code_is_logged(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, portal, public = strava_oauth
    state, _url = _connect(portal)
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get("/vibesynergy/strava/callback", params={"state": state}, follow_redirects=False)
    logs = _callback_logs(caplog)
    assert any("outcome=failed" in m and "reason=missing_code" in m for m in logs)
    _assert_clean(caplog, state)


def test_error_param_is_logged(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, portal, public = strava_oauth
    state, _url = _connect(portal)
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "error": "access_denied"},
        follow_redirects=False,
    )
    logs = _callback_logs(caplog)
    assert any("outcome=denied" in m and "reason=error_param" in m for m in logs)
    _assert_clean(caplog, state, "access_denied")


def test_missing_write_scope_is_logged(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, portal, public = strava_oauth
    state, _url = _connect(portal)
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": SECRET_CODE, "scope": "read"},
        follow_redirects=False,
    )
    assert any("reason=scope_missing_write" in m for m in _callback_logs(caplog))
    _assert_clean(caplog, state, SECRET_CODE)


def test_unknown_state_is_logged(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, _portal, public = strava_oauth
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    bogus = "x" * 43
    public.get("/vibesynergy/strava/callback", params={"state": bogus, "code": SECRET_CODE})
    logs = _callback_logs(caplog)
    assert logs and any(("state_unknown_or_used" in m) or ("state_mismatch" in m) for m in logs)
    _assert_clean(caplog, bogus, SECRET_CODE)


def test_no_state_is_logged_for_other_source(strava_oauth, caplog):  # noqa: F811
    _rt, _store, _fake, _portal, public = strava_oauth
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get("/vibesynergy/withings/callback", params={"code": SECRET_CODE})
    logs = _callback_logs(caplog)
    assert any("OAuth callback withings" in m and "reason=no_state" in m for m in logs)
    _assert_clean(caplog, SECRET_CODE)


def test_exchange_failure_logs_http_status(strava_oauth, caplog, monkeypatch):  # noqa: F811
    from vibesynergy.sources import oauth
    from vibesynergy.sources.strava import client as client_module

    def boom(self, *a, **kw):
        raise oauth.AuthError("Strava token request failed (503); no credentials saved")

    monkeypatch.setattr(client_module.StravaClient, "exchange_code", boom)
    _rt, _store, _fake, portal, public = strava_oauth
    state, _url = _connect(portal)
    caplog.set_level(logging.INFO, logger="vibesynergy.api.app")
    public.get(
        "/vibesynergy/strava/callback",
        params={"state": state, "code": SECRET_CODE, "scope": "read,activity:write"},
        follow_redirects=False,
    )
    logs = _callback_logs(caplog)
    assert any("reason=exchange_failed" in m and "http_status=503" in m for m in logs)
    _assert_clean(caplog, state, SECRET_CODE)


def test_safe_source_and_status_helpers():
    from vibesynergy.api.app import _http_status, _safe_source

    assert _safe_source("strava") == "strava"
    assert _safe_source("x?code=abc") == "other"
    assert _http_status(RuntimeError("failed (597); no")) == "597"
    assert _http_status(RuntimeError("timeout")) == "none"
