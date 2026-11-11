"""Fixes from the first real deployment: log redaction, Garmin 404s, login input checks."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date

import pytest
from garmin_fake import FakeGarmin
from garminconnect import GarminConnectNotFoundError

from vibesynergy.api.access_log import RedactSecrets, log_config, redact_path
from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import Window
from vibesynergy.runtime import Runtime
from vibesynergy.sources import oauth
from vibesynergy.sources.garmin.source import Throttled

KEY = "q8Zk3vN0pX2wY7rT5uB1cD4eF6gH9jKmLnOaSbVc"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (f"/vibesynergy/withings/notify/{KEY}", "/vibesynergy/withings/notify/[redacted]"),
        (f"/vibesynergy/{KEY}/mcp", "/vibesynergy/[redacted]/mcp"),
        (
            "/vibesynergy/strava/webhook?hub.verify_token=abc&hub.challenge=x",
            "/vibesynergy/strava/webhook?[redacted]",
        ),
        (
            "/vibesynergy/withings/callback?code=abc&state=def",
            "/vibesynergy/withings/callback?[redacted]",
        ),
        ("/vibesynergy/health", "/vibesynergy/health"),
        ("/vibesynergy/strava/webhook", "/vibesynergy/strava/webhook"),
    ],
)
def test_redact_path(path, expected) -> None:
    assert redact_path(path) == expected


def test_access_log_filter_and_config() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "POST", f"/vibesynergy/withings/notify/{KEY}", "1.1", 200),
        None,
    )
    assert RedactSecrets().filter(record)
    assert KEY not in record.getMessage() and "[redacted]" in record.getMessage()
    config = log_config()
    assert "redact_secrets" in config["handlers"]["access"]["filters"]


class NoDataGarmin(FakeGarmin):
    """Garmin answers 404 for a day without data."""

    def download_health_snapshot(self, day: str) -> bytes:
        raise GarminConnectNotFoundError("404: no wellness data")

    def get_hrv_data(self, day: str) -> dict:
        raise GarminConnectNotFoundError("404")


def test_garmin_404_is_empty_not_an_error(runtime) -> None:
    settings = replace(runtime.settings, sources={"garmin": {"enabled": True}})
    rt = Runtime.open(settings)
    rt.registry.get("garmin")._api = Throttled(NoDataGarmin(), 0)
    day = date(2026, 1, 5)  # long settled: the empty result is not fetched again
    results = rt.sync.sync("garmin", ["wellness_files", "hrv"], Window(day, day))
    assert [(r.dataset, r.error, r.normalise_errors) for r in results] == [
        ("wellness_files", None, 0),
        ("hrv", None, 0),
    ]
    again = rt.sync.sync("garmin", ["wellness_files"], Window(day, day))[0]
    assert again.skipped == 1
    rt.close()


def test_strava_client_id_must_be_numeric(tmp_path) -> None:
    store = SecretStore(tmp_path, "strava")
    with pytest.raises(oauth.AuthError, match="is a number"):
        oauth.ask_app(
            store, "Strava", numeric_id=True, ask=lambda _: "0395bcf6aa11", secret=lambda _: "x"
        )
    assert store.get(oauth.APP) is None  # nothing saved
    app = oauth.ask_app(
        store, "Strava", numeric_id=True, ask=lambda _: "123497", secret=lambda _: "s3cr3t"
    )
    assert app.client_id == "123497"


def test_stored_non_numeric_strava_id_is_not_offered(tmp_path) -> None:
    store = SecretStore(tmp_path, "strava")
    store.set(oauth.APP, {"client_id": "0395bcf6", "client_secret": "x"})
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "123497"

    oauth.ask_app(store, "Strava", numeric_id=True, ask=ask, secret=lambda _: "s")
    assert not any("stored" in p for p in prompts)


def test_same_id_and_secret_rejected(tmp_path) -> None:
    with pytest.raises(oauth.AuthError, match="same as the Client ID"):
        oauth.ask_app(
            SecretStore(tmp_path, "w"), "Withings", ask=lambda _: "abc", secret=lambda _: "abc"
        )


def test_pasting_the_consent_link_is_explained(monkeypatch) -> None:
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    with pytest.raises(oauth.AuthError, match="consent link itself"):
        oauth.paste_back_code(
            "https://www.strava.com/oauth/authorize",
            {"client_id": "1"},
            ask=lambda _: "https://www.strava.com/oauth/authorize?client_id=1&state=x",
        )
