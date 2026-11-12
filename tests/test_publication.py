"""Offline publication contract; every provider response is synthetic."""

import math
from dataclasses import replace
from datetime import UTC, datetime

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from sqlalchemy import select
from test_evidence import activity, put

from vibesynergy.core import config_edit
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.core.secrets import SecretStore
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach, publication, publication_format, publication_plugins
from vibesynergy.store.schema import publication_ledger

DEFAULT_OPENER = publication_format.DEFAULT_OPENER
DEFAULT_CLOSER = publication_format.DEFAULT_CLOSER
LEGACY_OPENER = publication_format.LEGACY_OPENER
LEGACY_CLOSER = publication_format.LEGACY_CLOSER


def owned_block(body: str, publication: dict | None = None) -> str:
    return publication_format.wrap_owned(body, publication or {})


class FakeProvider:
    def __init__(self, text="Human note"):
        self.text = text
        self.writes = []
        self.fail_readback = False

    def read(self, source_id):
        assert source_id == "1"
        if self.fail_readback and self.writes:
            return {"activityId": source_id, "description": "Other text"}
        return {"activityId": source_id, "description": self.text}

    def write(self, source_id, description):
        self.writes.append((source_id, description))
        self.text = description


ORIGINAL_WEATHER = publication_plugins._weather
STATS = (
    "  🔥 Aerobic training effect: 1.9\n"
    "  ⚡ Anaerobic training effect: 0.0\n"
    "  🏷️ Effect label: RECOVERY\n"
    "  📊 Training load: 25.5\n"
    "  🌡️ Temperature: 17-24°C\n"
    "  😌 Workout feel: Normal\n"
    "  😄 RPE: 1/10 (Nothing at all)"
)


def garmin_activity(**changes):
    return replace(
        activity(),
        training_load=25.5,
        aerobic_te=1.9,
        anaerobic_te=0.0,
        extra={
            "trainingEffectLabel": "RECOVERY",
            "feel": 50,
            "rpe": 10,
            "minTemperature": 17.0,
            "maxTemperature": 24.0,
        },
        **changes,
    )


@pytest.fixture(autouse=True)
def offline_plugins(monkeypatch):
    """No network in these tests unless a test opts in."""
    monkeypatch.setattr(publication_plugins, "_weather", lambda *_: None)


def only_stats(runtime):
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"quote_enabled": False, "weather_enabled": False}},
    )


def ready(runtime):
    put(runtime, "garmin", garmin_activity())


def test_preview_publish_and_retry_preserve_human_text(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert item["description"].startswith(f"Human note\n\n{DEFAULT_OPENER}")
    assert item["owned_block"] == owned_block(f"-Garmin stats:\n{STATS}")
    assert fake.writes == []
    published = publication.publish(
        runtime, "garmin:1", "garmin", item["preview_version"], client=fake
    )
    assert published["state"] == "verified"
    assert fake.writes == [("1", item["description"])]
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert again["already_published"]
    publication.publish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    assert len(fake.writes) == 1
    fake.text = "New human note\n\n" + fake.text.split("\n\n", 1)[1]
    changed = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert changed["description"].startswith("New human note")
    assert changed["already_published"]


@pytest.mark.parametrize("language", ["en", "nl"])
def test_public_block_has_only_whitelisted_statistics(runtime, language):
    ready(runtime)
    only_stats(runtime)
    item = publication.preview(
        runtime, "garmin:1", "garmin", language=language, client=FakeProvider("")
    )
    expected = (
        "-Garmin stats:\n"
        "  🔥 Aerobe trainingseffect: 1.9\n"
        "  ⚡ Anaëroob trainingseffect: 0.0\n"
        "  🏷️ Effect-label: RECOVERY\n"
        "  📊 Training load: 25.5\n"
        "  🌡️ Temperatuur: 17-24°C\n"
        "  😌 Uitvoeringsgevoel (feel): Normaal\n"
        "  😄 RPE: 1/10 (Helemaal niets)"
        if language == "nl"
        else f"-Garmin stats:\n{STATS}"
    )
    assert item["owned_block"] == owned_block(expected)
    assert item["description"] == item["owned_block"]
    assert "Check how you feel" not in item["description"]


def test_full_dutch_format_is_stats_weather_and_quote(runtime, monkeypatch):
    ready(runtime)
    monkeypatch.setattr(
        publication_plugins, "_weather", lambda *_: ("☁️ Bewolkt, 11°C, 5 km/h W", {})
    )
    monkeypatch.setattr(
        publication_plugins,
        "_quote",
        lambda *_: ("Simplify, simplify. — Henry David Thoreau", {"quote_id": "x"}),
    )
    item = publication.preview(
        runtime, "garmin:1", "garmin", language="nl", client=FakeProvider("")
    )
    assert item["owned_block"] == owned_block(
        "-Garmin stats:\n"
        "  🔥 Aerobe trainingseffect: 1.9\n"
        "  ⚡ Anaëroob trainingseffect: 0.0\n"
        "  🏷️ Effect-label: RECOVERY\n"
        "  📊 Training load: 25.5\n"
        "  🌡️ Temperatuur: 17-24°C\n"
        "  😌 Uitvoeringsgevoel (feel): Normaal\n"
        "  😄 RPE: 1/10 (Helemaal niets)\n"
        "  ☁️ Bewolkt, 11°C, 5 km/h W\n"
        "\n"
        "-Quote: Simplify, simplify. — Henry David Thoreau"
    )
    assert "AI inzicht" not in item["description"]
    assert item["owned_block"].endswith(DEFAULT_CLOSER)
    assert {k: v["status"] for k, v in item["plugins"].items()} == {
        "weather": "ok",
        "quote": "ok",
    }


def test_publishing_does_not_need_an_assessment(runtime):
    ready(runtime)
    only_stats(runtime)
    item = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert item["state"] == "ready"
    assert "AI" not in item["owned_block"]
    assert item["owned_block"].endswith("[ http://vibesynergy.nl/ */]")


def test_missing_garmin_fields_leave_their_lines_out(runtime):
    put(runtime, "garmin", replace(activity(), aerobic_te=2.4))
    only_stats(runtime)
    item = publication.preview(
        runtime, "garmin:1", "garmin", language="nl", client=FakeProvider("")
    )
    assert item["owned_block"] == owned_block("-Garmin stats:\n  🔥 Aerobe trainingseffect: 2.4")


def test_no_garmin_fields_blocks_publication(runtime):
    put(runtime, "garmin", activity())
    item = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert item == {
        "state": "blocked",
        "reason": "no_public_statistics",
        "assessment_state": None,
        "destination": "garmin",
    }


def test_text_added_below_the_block_moves_above_it_on_republish(runtime, monkeypatch):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider("Mine")
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    owned = item["owned_block"]
    fake.text = f"Mine\n\n{owned}\n\nAdded later"
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert again["description"] == f"Mine\n\nAdded later\n\n{owned}"
    assert again["description"].count(DEFAULT_OPENER) == 1
    publication.publish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    final = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.unpublish(runtime, "garmin:1", "garmin", final["preview_version"], client=fake)
    assert fake.text == "Mine\n\nAdded later"


@pytest.mark.parametrize("encrypted", [False, True], ids=["legacy-plaintext", "encrypted"])
def test_strava_scope_check_and_writer_share_token_store(runtime, encrypted):
    store = SecretStore(runtime.settings.secrets_dir, "strava", encrypted=encrypted)
    tokens = {"scope": "read,activity:write", "access_token": "synthetic-token"}
    store.set("tokens", tokens)
    provider = publication.provider(runtime, "strava")
    assert provider.client._store is provider.store
    assert provider.client._store.get("tokens") == tokens
    assert provider.store.get("tokens") == tokens
    # A legacy plaintext document is rewritten encrypted on its first read.
    assert (runtime.settings.secrets_dir / "strava" / "tokens.enc").exists()
    assert not (runtime.settings.secrets_dir / "strava" / "tokens.json").exists()
    store.set("tokens", {**tokens, "scope": "read"})
    with pytest.raises(publication.Conflict, match="activity write access"):
        publication.provider(runtime, "strava")


@pytest.mark.parametrize(
    ("profile_id", "activity_owner", "stored_id", "allowed"),
    [
        (123, 123, 123, True),
        (123, 456, 123, False),
        (123, None, 123, False),
        (123, 123, 456, False),
        (123, 123, None, True),
        (None, 123, None, False),
    ],
)
def test_garmin_owner_requires_live_session_match(
    runtime, monkeypatch, profile_id, activity_owner, stored_id, allowed
):
    provider = publication.GarminProvider(runtime)
    if stored_id is not None:
        provider.store.set("account", {"id": stored_id})
    detail = {"activityId": "1", "userProfileId": activity_owner, "description": ""}
    monkeypatch.setattr(provider, "_call", lambda *_: (detail, profile_id))
    if allowed:
        assert provider.read("1") == detail
    else:
        with pytest.raises(publication.Conflict, match="owner could not be verified"):
            provider.read("1")


def test_remove_restores_human_text_and_clears_checkpoint(runtime):
    ready(runtime)
    fake = FakeProvider("My own words  \n")
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert again["undo_description"] == "My own words  \n"
    result = publication.unpublish(
        runtime, "garmin:1", "garmin", again["preview_version"], client=fake
    )
    assert result["state"] == "removed"
    assert fake.text == "My own words  \n"
    with runtime.engine.connect() as conn:
        assert conn.execute(select(publication_ledger)).first() is None


def test_remove_retry_reconciles_completed_provider_write(runtime):
    ready(runtime)
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    original_read = fake.read
    calls = 0

    def flaky_read(source_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            return {"activityId": source_id, "description": "Other text"}
        return original_read(source_id)

    fake.read = flaky_read
    try:
        publication.unpublish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    except publication.Conflict:
        pass
    else:
        raise AssertionError("failed read-back was accepted")
    assert fake.text == "Human note"
    fake.read = original_read
    pending = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert pending["state"] == "undo_pending"
    publication.unpublish(runtime, "garmin:1", "garmin", pending["preview_version"], client=fake)
    assert len(fake.writes) == 2


def test_changed_owned_section_and_stale_preview_block_writes(runtime):
    ready(runtime)
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    fake.text = "Edited meanwhile"
    try:
        publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    except publication.Conflict as exc:
        assert "preview" in str(exc)
    else:
        raise AssertionError("stale preview was accepted")
    assert not fake.writes
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    fake.text = fake.text.replace("Training load", "Changed load")
    try:
        publication.preview(runtime, "garmin:1", "garmin", client=fake)
    except publication.Conflict as exc:
        assert "published section changed" in str(exc)
    else:
        raise AssertionError("edited owned section was adopted")


def test_failed_readback_keeps_pending_checkpoint_and_reconciles(runtime):
    ready(runtime)
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    fake.fail_readback = True
    try:
        publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    except publication.Conflict as exc:
        assert "did not return" in str(exc)
    else:
        raise AssertionError("unverified write reported success")
    with runtime.engine.connect() as conn:
        row = conn.execute(select(publication_ledger)).mappings().one()
        assert row["state"] == "pending"
    fake.fail_readback = False
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    assert len(fake.writes) == 1
    with runtime.engine.connect() as conn:
        assert conn.execute(select(publication_ledger.c.state)).scalar_one() == "verified"


def test_quote_and_weather_are_independent_and_keep_verified_sections(runtime, monkeypatch):
    ready(runtime)
    config_edit.apply_changes(
        runtime.settings.config_path,
        {
            "publication": {
                "quote_enabled": True,
                "weather_enabled": True,
            }
        },
    )
    fake = FakeProvider()
    calls = []

    def weather(_rt, _activity_id, _language):
        calls.append(1)
        if len(calls) > 1:
            raise ValueError("synthetic outage")
        return "☁️ Bewolkt, 12°C", {
            "source": "synthetic",
            "time": "2020-01-01T10:00Z",
        }

    monkeypatch.setattr(publication_plugins, "_weather", weather)
    first = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert first["plugins"]["quote"]["status"] == "ok"
    assert first["plugins"]["weather"]["status"] == "ok"
    assert "Bewolkt, 12°C" in first["description"]
    assert "-Quote:" in first["description"]
    assert fake.writes == []
    second = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert second["description"] == first["description"]
    assert len(calls) == 1  # cached, including on publication retry
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"quote_enabled": False}}
    )
    without_quote = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert "-Quote:" not in without_quote["description"]
    assert "Bewolkt, 12°C" in without_quote["description"]
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"weather_enabled": False}}
    )
    without_both = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert "Bewolkt, 12°C" not in without_both["description"]


def test_weather_failure_reuses_prior_section_on_changed_assessment(runtime, monkeypatch):
    ready(runtime)
    config_edit.apply_changes(
        runtime.settings.config_path, {"publication": {"weather_enabled": True}}
    )
    fake = FakeProvider()
    monkeypatch.setattr(
        publication_plugins,
        "_weather",
        lambda *_: ("☁️ Bewolkt, 12°C", {"source": "synthetic"}),
    )
    first = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    config_edit.apply_changes(runtime.settings.config_path, {"publication": {"quote_themes": []}})
    # A new assessment input version forces one new weather attempt while retaining
    # the previously verified section if the provider has a transient failure.
    # Cache identity is the activity, not an assessment. A stale cache version still
    # keeps the last verified weather line when the provider fails.
    with runtime.engine.begin() as conn:
        from vibesynergy.store.schema import publication_enrichment

        conn.execute(publication_enrichment.update().values(version="stale"))

    def failed_weather(*_):
        raise ValueError("down")

    monkeypatch.setattr(publication_plugins, "_weather", failed_weather)
    second = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert second["plugins"]["weather"]["status"] == "failed"
    assert second["plugins"]["weather"]["reused"] is True
    assert "☁️ Bewolkt, 12°C" in second["description"]
    assert first["assessment_id"] == second["assessment_id"]


def test_weather_uses_start_hour_and_never_current_conditions(runtime, monkeypatch):
    start = datetime(2020, 1, 1, 10, 20, tzinfo=UTC)
    monkeypatch.setattr(publication_plugins, "_location", lambda *_: (52.1, 5.1, start))
    requests = []

    class Reply:
        content = b"{}"

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "hourly": {
                    "time": ["2020-01-01T09:00", "2020-01-01T10:00", "2020-01-01T11:00"],
                    "temperature_2m": [4, 7, 10],
                    "weather_code": [3, 61, 3],
                    "wind_speed_10m": [5, 10.4, 15],
                    "wind_direction_10m": [0, 270, 90],
                }
            }

    def http(url, *, params, timeout):
        requests.append((url, params, timeout))
        return Reply()

    section, provenance = ORIGINAL_WEATHER(
        runtime, "garmin:1", "en", http=http, now=datetime(2020, 1, 10, tzinfo=UTC)
    )
    assert section == "🌦️ Light rain, 7°C, 10 km/h W"
    assert provenance["time"] == "2020-01-01T10:00:00+00:00"
    assert requests[0][1]["start_date"] == "2020-01-01"
    assert requests[0][2] == 5
    assert requests[0][0] == publication_plugins.WEATHER_URL
    # Published right after (8-11, the brick run): the recent hours, not a wait for the archive.
    ORIGINAL_WEATHER(runtime, "garmin:1", "en", http=http, now=datetime(2020, 1, 2, tzinfo=UTC))
    assert requests[-1][0] == publication_plugins.RECENT_WEATHER_URL


def test_quote_rotation_avoids_the_previous_verified_quote(runtime):
    themes = list(publication_plugins.THEMES)
    section, provenance = publication_plugins._quote(runtime, "first", "en", themes)
    publication_plugins._save(runtime, "first", "quote", section, provenance, "test")
    _, next_provenance = publication_plugins._quote(runtime, "second", "en", themes)
    assert next_provenance["quote_id"] != provenance["quote_id"]


def test_api_and_mcp_share_preview_and_user_triggered_publish(runtime, monkeypatch):
    ready(runtime)
    fake = FakeProvider()
    monkeypatch.setattr(publication, "provider", lambda *_: fake)
    publication.preview(runtime, "garmin:1", "garmin")  # fills the plugin cache
    expected = publication.preview(runtime, "garmin:1", "garmin")
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")
    api_preview = client.get(
        "/api/activity-publication-preview", params={"id": "garmin:1", "destination": "garmin"}
    ).json()["data"]
    assert api_preview == expected

    async def mcp_preview():
        async with Client(build_server(runtime)) as connection:
            return await connection.call_tool(
                "vibesynergy_activity_publication_preview",
                {"id": "garmin:1", "destination": "garmin"},
            )

    assert anyio.run(mcp_preview).structured_content["data"] == expected
    api_publish = client.post(
        "/api/activity-publication",
        json={
            "id": "garmin:1",
            "destination": "garmin",
            "preview_version": expected["preview_version"],
        },
        headers={"x-vibesynergy": "portal"},
    ).json()["data"]
    assert api_publish["state"] == "verified"
    assert fake.writes == [("1", expected["description"])]
    current = publication.preview(runtime, "garmin:1", "garmin")

    async def mcp_publish():
        async with Client(build_server(runtime)) as connection:
            return await connection.call_tool(
                "vibesynergy_activity_publish",
                {
                    "id": "garmin:1",
                    "destination": "garmin",
                    "preview_version": current["preview_version"],
                },
            )

    assert anyio.run(mcp_publish).structured_content["data"]["state"] == "verified"
    assert len(fake.writes) == 1
    removed = client.post(
        "/api/activity-publication/remove",
        json={
            "id": "garmin:1",
            "destination": "garmin",
            "preview_version": current["preview_version"],
        },
        headers={"x-vibesynergy": "portal"},
    ).json()["data"]
    assert removed["state"] == "removed"
    assert fake.text == "Human note"
    republish = publication.preview(runtime, "garmin:1", "garmin")
    publication.publish(runtime, "garmin:1", "garmin", republish["preview_version"])
    final = publication.preview(runtime, "garmin:1", "garmin")

    async def mcp_unpublish():
        async with Client(build_server(runtime)) as connection:
            return await connection.call_tool(
                "vibesynergy_activity_unpublish",
                {
                    "id": "garmin:1",
                    "destination": "garmin",
                    "preview_version": final["preview_version"],
                },
            )

    assert anyio.run(mcp_unpublish).structured_content["data"]["state"] == "removed"
    assert fake.text == "Human note"


def test_one_active_profile_revision_is_shared_across_consumers(runtime, monkeypatch):
    ready(runtime)
    fake = FakeProvider()
    monkeypatch.setattr(publication, "provider", lambda *_: fake)
    client = TestClient(create_portal_app(runtime), base_url="http://localhost")

    def dashboard_profile():
        return client.get("/api/dashboard").json()["data"]["active_profile"]

    def daily_profile():
        return client.get("/api/daily-coaching").json()["data"]["active_profile"]

    def assessment():
        return client.get("/api/activity-assessment", params={"id": "garmin:1"}).json()["data"]

    def annotation():
        return client.get("/api/activity-annotation-preview", params={"id": "garmin:1"}).json()[
            "data"
        ]

    def publication_preview():
        return client.get(
            "/api/activity-publication-preview", params={"id": "garmin:1", "destination": "garmin"}
        ).json()["data"]

    async def mcp_profile():
        async with Client(build_server(runtime)) as connection:
            result = await connection.call_tool("vibesynergy_coach_state", {})
            return result.structured_content["data"]["active_profile"]

    first_assessment = assessment()
    first_annotation = annotation()
    first_publication = publication_preview()
    first_binding = first_annotation["active_profile"]
    first_mcp = anyio.run(mcp_profile)
    assert first_assessment["state"] == "missing"
    assert first_assessment["coaching_state"]["reason"] == "retired"
    assert first_annotation["state"] == "preview"
    assert first_publication["state"] == "ready"
    assert first_assessment["assessment"] is None
    assert dashboard_profile() == daily_profile() == first_binding == first_mcp

    coach.save_draft(
        runtime.engine,
        "policy",
        {"priority": "performance", "phase": "taper", "instructions": "Keep the long run easy."},
        expected_version=0,
        reason="Synthetic profile update",
    )
    coach.activate(runtime.engine, "policy", expected_version=1, reason="Activate")

    changed_assessment = assessment()
    changed_annotation = annotation()
    changed_publication = publication_preview()
    changed_binding = changed_annotation["active_profile"]
    changed_mcp = anyio.run(mcp_profile)
    assert changed_assessment["state"] == "missing"
    assert changed_assessment["coaching_state"]["reason"] == "retired"
    assert changed_annotation["state"] == "preview"
    assert changed_publication["state"] == "ready"
    assert dashboard_profile() == daily_profile() == changed_binding == changed_mcp
    assert changed_binding["profile_version"] != first_binding["profile_version"]


def test_garmin_weather_converts_units_and_follows_the_condition():
    weather = {
        "temp": 66,
        "windSpeed": 8,
        "windDirection": 180,
        "windDirectionCompassPoint": "s",
        "weatherTypeDTO": {"desc": "Cloudy"},
    }
    assert publication_format.garmin_weather(weather, "nl") == "☁️ Bewolkt, 19°C, 13 km/h Z"
    assert publication_format.garmin_weather(weather, "en") == "☁️ Cloudy, 19°C, 13 km/h S"
    rain = {**weather, "weatherTypeDTO": {"desc": "Rain"}, "windDirectionCompassPoint": "ssw"}
    assert publication_format.garmin_weather(rain, "nl") == "🌧️ Regen, 19°C, 13 km/h ZZW"
    sunny = {"temp": 50, "weatherTypeDTO": {"desc": "Sunny"}}
    assert publication_format.garmin_weather(sunny, "nl") == "☀️ Zonnig, 10°C"
    assert publication_format.garmin_weather({"weatherTypeDTO": {}}, "nl") is None
    assert publication_format.garmin_weather(None, "nl") is None


def test_stats_lines_full_and_invalid_values():
    fields = {
        "aerobic_te": 1.94,
        "anaerobic_te": 0.0,
        "te_label": "RECOVERY",
        "training_load": 25.46,
        "stamina_start": 99.0,
        "stamina_end": 95.0,
        "temp_min": 17.0,
        "temp_max": 24.0,
        "temp_avg": 19.2,
        "feel": 50,
        "rpe": 10,
    }
    assert publication_format.stats_lines(fields, "nl") == [
        "🔥 Aerobe trainingseffect: 1.9",
        "⚡ Anaëroob trainingseffect: 0.0",
        "🏷️ Effect-label: RECOVERY",
        "📊 Training load: 25.5",
        "🔋 Stamina: 99 -> 95",
        "🌡️ Temperatuur: 17-24°C (gem 19°C)",
        "😌 Uitvoeringsgevoel (feel): Normaal",
        "😄 RPE: 1/10 (Helemaal niets)",
    ]
    broken = {
        "aerobic_te": math.nan,
        "anaerobic_te": 7.0,
        "te_label": None,
        "training_load": -1.0,
        "stamina_start": 99.0,
        "stamina_end": None,
        "temp_min": 24.0,
        "temp_max": 17.0,
        "feel": 150,
        "rpe": True,
    }
    assert publication_format.stats_lines(broken, "nl") == []
    assert publication_format.stats_lines({}, "nl") == []


def test_new_defaults_are_on_and_config_switches_them_off(runtime):
    ready(runtime)
    item = publication.preview(
        runtime, "garmin:1", "garmin", language="nl", client=FakeProvider("")
    )
    assert {k: v["status"] for k, v in item["plugins"].items()} == {
        "weather": "unavailable",
        "quote": "ok",
    }
    config_edit.apply_changes(
        runtime.settings.config_path,
        {
            "publication": {
                "weather_enabled": False,
                "quote_enabled": False,
            }
        },
    )
    off = publication.preview(runtime, "garmin:1", "garmin", language="nl", client=FakeProvider(""))
    assert {v["status"] for v in off["plugins"].values()} == {"disabled"}


def test_unknown_weather_text_and_negative_range():
    assert publication_format.weather_line("[/VibeSynergy]", 3, None, language="nl") is None
    assert publication_format.weather_line("Squalls", 3, None, language="en") == "🌡️ Squalls, 3°C"
    assert publication_format.stats_lines({"temp_min": -3.0, "temp_max": -1.0}, "nl") == [
        "🌡️ Temperatuur: -3 \u2013 -1°C"
    ]


def test_default_opener_and_closer_wrap_the_block(runtime):
    ready(runtime)
    only_stats(runtime)
    item = publication.preview(runtime, "garmin:1", "garmin", client=FakeProvider(""))
    assert item["owned_block"].startswith(DEFAULT_OPENER)
    assert item["owned_block"].endswith(DEFAULT_CLOSER)


def test_legacy_vibesynergy_block_is_rewritten_with_byte_identical_user_text(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider("Mine")
    first = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", first["preview_version"], client=fake)
    body = first["owned_block"].split("\n", 1)[1].rsplit("\n", 1)[0]
    legacy_owned = f"{LEGACY_OPENER}\n{body}\n{LEGACY_CLOSER}"
    fake.text = f"Mine\n\n{legacy_owned}"
    with runtime.engine.begin() as conn:
        conn.execute(
            publication_ledger.update()
            .where(
                publication_ledger.c.source == "garmin",
                publication_ledger.c.source_id == "1",
            )
            .values(owned_block=legacy_owned)
        )
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert item["owned_block"].startswith(DEFAULT_OPENER)
    assert item["owned_block"].count(DEFAULT_OPENER) == 1
    assert item["description"].startswith("Mine\n\n")
    assert publication._without_owned(item["description"]) == "Mine"
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    assert fake.text == item["description"]


def test_changed_markers_still_find_and_rewrite_without_duplicates(runtime):
    ready(runtime)
    only_stats(runtime)
    custom = {"opener": "[/* Custom opener]", "closer": "[Custom closer */]"}
    config_edit.apply_changes(runtime.settings.config_path, {"publication": custom})
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    old_owned = item["owned_block"]
    config_edit.apply_changes(
        runtime.settings.config_path,
        {"publication": {"opener": "[/* New opener]", "closer": "[New closer */]"}},
    )
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert again["owned_block"].startswith("[/* New opener]")
    assert again["owned_block"] != old_owned
    assert again["description"].count("[/*") == 1
    publication.publish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    final = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.unpublish(runtime, "garmin:1", "garmin", final["preview_version"], client=fake)
    assert fake.text == "Human note"


def test_untracked_legacy_block_without_ledger_is_refused(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider(f"Note\n\n{LEGACY_OPENER}\n-body\n{LEGACY_CLOSER}")
    with pytest.raises(publication.Conflict, match="needs review"):
        publication.preview(runtime, "garmin:1", "garmin", client=fake)


def test_custom_section_names_in_compose(runtime, monkeypatch):
    ready(runtime)
    only_stats(runtime)
    monkeypatch.setattr(publication_plugins, "_quote", lambda *_: ("A quote.", {"source": "test"}))
    config_edit.apply_changes(
        runtime.settings.config_path,
        {
            "publication": {
                "quote_enabled": True,
                "label_stats": "Mijn stats",
                "label_quote": "Mijn quote",
            }
        },
    )
    item = publication.preview(
        runtime, "garmin:1", "garmin", language="nl", client=FakeProvider("")
    )
    assert "AI inzicht" not in item["owned_block"]
    assert "-Mijn stats:" in item["owned_block"]
    assert "-Mijn quote:" in item["owned_block"]


@pytest.mark.parametrize(
    "publication_values, message",
    [
        ({"opener": "bad"}, "opener"),
        ({"closer": "bad"}, "closer"),
        ({"opener": "[/*" + "x" * 98}, "100 characters"),
        ({"closer": "[" + "x" * 98 + " */]"}, "100 characters"),
        ({"label_stats": "x" * 41}, "40 characters"),
    ],
)
def test_invalid_publication_settings_rejected(tmp_path, publication_values, message):
    cfg = tmp_path / "c.toml"
    lines = [f'{k} = "{v}"' for k, v in publication_values.items()]
    cfg.write_text("[publication]\n" + "\n".join(lines) + "\n")
    with pytest.raises(ConfigError, match=message):
        load_settings(cfg)


def test_settings_api_rejects_invalid_publication_markers(runtime, tmp_path):
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from vibesynergy.portal.app import create_portal_app
    from vibesynergy.runtime import Runtime

    TAILNET = "vibesynergy.test"
    WRITE = {"x-vibesynergy": "portal"}

    config = tmp_path / "config.toml"
    config.write_text(
        f'[user]\ntimezone = "Europe/Amsterdam"\n\n[server]\npublic_url = "https://{TAILNET}"\n'
    )
    settings = replace(load_settings(config), data_dir=runtime.settings.data_dir)
    rt = Runtime.open(settings)
    client = TestClient(create_portal_app(rt), base_url=f"https://{TAILNET}:8447")
    bad = {"changes": {"publication": {"opener": "not a marker"}}}
    assert client.post("/api/settings", json=bad, headers=WRITE).status_code == 400
    rt.close()


def test_crlf_removal_keeps_the_original_human_bytes():
    human = "Human note\r\nStill mine"
    text = f"{human}\r\n\r\n{DEFAULT_OPENER}\r\nbody\r\n{DEFAULT_CLOSER}"
    spans = publication_format.find_blocks(text)
    assert len(spans) == 1
    assert publication_format.block_text(text, spans[0]) == (
        f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}"
    )
    assert publication._without_owned(text) == human


def test_crlf_readback_counts_as_the_same_description(runtime):
    ready(runtime)
    only_stats(runtime)

    class CrlfProvider(FakeProvider):
        def write(self, source_id, description):
            self.writes.append((source_id, description))
            normalized = description.replace("\r\n", "\n").replace("\r", "\n")
            self.text = normalized.replace("\n", "\r\n")

    fake = CrlfProvider("Human note\r\nStill mine")
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    again = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    assert again["already_published"]
    assert again["undo_description"] == "Human note\r\nStill mine"
    publication.publish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    assert len(fake.writes) == 1
    publication.unpublish(runtime, "garmin:1", "garmin", again["preview_version"], client=fake)
    assert fake.text == "Human note\r\nStill mine"


def test_quotes_are_source_checked_and_every_theme_has_lines(runtime):
    """Each quote was found verbatim in its Project Gutenberg text (8-11: 58 lines)."""
    texts = set()
    for quote in publication_plugins.QUOTES:
        assert quote["theme"] in publication_plugins.THEMES
        assert quote["url"].startswith("https://www.gutenberg.org/")
        assert quote["author"] and quote["work"] and "http" not in quote["text"].lower()
        texts.add(quote["text"])
    assert len(texts) == len(publication_plugins.QUOTES) >= 120
    assert not any(
        "Ecclesiastes" in q["author"] or "Bible" in q["work"] for q in publication_plugins.QUOTES
    )
    sources = {publication_plugins.collection_of(q) for q in publication_plugins.QUOTES}
    assert sources == set(publication_plugins.COLLECTIONS)
    themes = {quote["theme"] for quote in publication_plugins.QUOTES}
    assert themes == set(publication_plugins.THEMES)
    assert publication_plugins._quote(runtime, "activity-1", "en", ["no such theme"]) is None


def test_quote_sources_can_be_switched_off(runtime):
    """show each source in the settings and let it be switched off."""
    themes = list(publication_plugins.THEMES)
    for n in range(5):
        section, provenance = publication_plugins._quote(
            runtime, f"stoic-{n}", "en", themes, ["stoics"]
        )
        assert provenance["collection"] == "stoics"
        publication_plugins._save(runtime, f"stoic-{n}", "quote", section, provenance, "t")
    left = publication_plugins.quotes_left(runtime, themes, ["stoics", "maxims"])
    stoics = [
        q for q in publication_plugins.QUOTES if publication_plugins.collection_of(q) == "stoics"
    ]
    assert left["stoics"] == len(stoics) - 5 and left["maxims"] > 0
    assert publication_plugins._quote(runtime, "none", "en", themes, []) is None


def test_a_quote_is_never_used_twice(runtime):
    """the quotes must always be unique."""
    themes = ["sport", "time"]
    pool = [q for q in publication_plugins.QUOTES if q["theme"] in themes]
    assert sum(publication_plugins.quotes_left(runtime, themes).values()) == len(pool)
    seen = []
    for n in range(len(pool)):
        section, provenance = publication_plugins._quote(runtime, f"activity-{n}", "en", themes)
        publication_plugins._save(runtime, f"activity-{n}", "quote", section, provenance, "t")
        seen.append(provenance["quote_id"])
    assert len(set(seen)) == len(pool)
    assert sum(publication_plugins.quotes_left(runtime, themes).values()) == 0
    # All used: no quote rather than a repeat; the activity that has one keeps it.
    assert publication_plugins._quote(runtime, "activity-new", "en", themes) is None
    again = publication_plugins._quote(runtime, "activity-0", "en", themes)
    assert again is not None and again[1]["quote_id"] == seen[0]


def test_crlf_descriptions_still_find_one_block():
    body = "line"
    text = f"intro\r\n\r\n{DEFAULT_OPENER}\r\n{body}\r\n{DEFAULT_CLOSER}\r\n"
    spans = publication_format.find_blocks(text)
    assert len(spans) == 1
    assert body in publication_format.block_text(text, spans[0])
    assert not publication_format.orphan_markers(text)


def test_token_detection_uses_first_closer_after_opener():
    text = (
        f"{DEFAULT_OPENER}\nline\n{DEFAULT_CLOSER}\nmore\n{DEFAULT_OPENER}\ntail\n{DEFAULT_CLOSER}"
    )
    spans = publication_format.find_blocks(text)
    assert len(spans) == 2
    assert publication_format.block_text(text, spans[0]).startswith(DEFAULT_OPENER)


def test_two_closers_after_one_opener_use_the_first_closer():
    text = f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}\n{DEFAULT_CLOSER}\nrest"
    spans = publication_format.find_blocks(text)
    assert len(spans) == 1
    assert publication_format.block_text(text, spans[0]) == (
        f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}"
    )


def test_default_opener_and_closer_marker_rules():
    publication_format.validate_opener("[/*]")
    assert publication_format._is_opener_line("[/*]")
    assert not publication_format._is_closer_line("[/*]")
    assert not publication_format._is_opener_line(DEFAULT_CLOSER)
    with pytest.raises(ConfigError, match="must not end with"):
        publication_format.validate_opener("[/* x */]")
    assert publication_format._is_closer_line("[/* x */]")
    assert not publication_format._is_opener_line("[/* x */]")


def test_untracked_new_format_block_without_ledger_is_refused(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider(f"Note\n\n{DEFAULT_OPENER}\n-body\n{DEFAULT_CLOSER}")
    with pytest.raises(publication.Conflict, match="needs review"):
        publication.preview(runtime, "garmin:1", "garmin", client=fake)


def test_stray_closer_without_ledger_is_refused(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider(f"Note\n\n{DEFAULT_CLOSER}")
    with pytest.raises(publication.Conflict, match="needs review"):
        publication.preview(runtime, "garmin:1", "garmin", client=fake)


def test_stray_marker_with_ledger_is_refused(runtime):
    ready(runtime)
    only_stats(runtime)
    fake = FakeProvider()
    item = publication.preview(runtime, "garmin:1", "garmin", client=fake)
    publication.publish(runtime, "garmin:1", "garmin", item["preview_version"], client=fake)
    fake.text = f"Mine\n\n{item['owned_block']}\n{LEGACY_CLOSER}"
    with pytest.raises(publication.Conflict, match="section changed"):
        publication.preview(runtime, "garmin:1", "garmin", client=fake)


@pytest.mark.parametrize(
    "description, expected",
    [
        ("A\n\n\n\nB\n\n" + f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}", "A\n\n\n\nB"),
        (
            "A\n\n\n\n\n\n\n\n\n\n\n\n" + f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}",
            "A\n\n\n\n\n\n\n\n\n\n",
        ),
        (f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}\n\nTail", "Tail"),
        ("Head\n\n" + f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}", "Head"),
        (f"{DEFAULT_OPENER}\nbody\n{DEFAULT_CLOSER}", ""),
    ],
)
def test_without_owned_keeps_user_text_byte_identical(description, expected):
    assert publication._without_owned(description) == expected
