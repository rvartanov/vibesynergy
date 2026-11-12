"""CalDAV setup: encrypted secrets, discovery, redirect policy, the portal API and Google
sign-in. Synthetic hosts and credentials only; every HTTP call is a fake."""

from __future__ import annotations

import json
import stat
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from tests_helpers import add_public_url
from typer.testing import CliRunner

from vibesynergy.cli.main import app as cli
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.core.secrets import SecretError, SecretStore
from vibesynergy.portal.app import create_portal_app
from vibesynergy.runtime import Runtime
from vibesynergy.sources.caldav import account, google
from vibesynergy.sources.caldav.client import CalDAVClient
from vibesynergy.sources.caldav.discovery import CalDAVError, discover
from vibesynergy.sync.scheduler import Scheduler

PASSWORD = "synthetic-app-password-4711"
D = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:a="http://apple.com/ns/ical/"'


def multistatus(*rows: str) -> bytes:
    return f"<d:multistatus {D}>{''.join(rows)}</d:multistatus>".encode()


def row(href: str, prop: str) -> str:
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{prop}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


def principal(href: str) -> bytes:
    return multistatus(
        row("/", f"<d:current-user-principal><d:href>{href}</d:href></d:current-user-principal>")
    )


def home(href: str) -> bytes:
    return multistatus(
        row("/p/", f"<c:calendar-home-set><d:href>{href}</d:href></c:calendar-home-set>")
    )


def calendar_row(name: str, path: str, *, comps=("VEVENT",), color="#FF2968FF", kind="calendar"):
    comp_set = "".join(f'<c:comp name="{c}"/>' for c in comps)
    return row(
        path,
        f"<d:resourcetype><d:collection/><c:{kind}/></d:resourcetype>"
        f"<d:displayname>{name}</d:displayname><a:calendar-color>{color}</a:calendar-color>"
        f"<c:supported-calendar-component-set>{comp_set}</c:supported-calendar-component-set>",
    )


LISTING = multistatus(
    row("/12345/calendars/", "<d:resourcetype><d:collection/></d:resourcetype>"),
    calendar_row("Home", "/12345/calendars/home/"),
    calendar_row("Reminders", "/12345/calendars/tasks/", comps=("VTODO",)),
    calendar_row("Inbox", "/12345/calendars/inbox/", kind="schedule-inbox"),
    calendar_row("Work", "/12345/calendars/work/", color="not-a-colour"),
)


def reply(status=207, content=b"", location=None):
    return SimpleNamespace(
        status_code=status, content=content, headers={"Location": location} if location else {}
    )


class FakeHTTP:
    """Routes (method, url) to canned replies and records the requests."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls: list[tuple[str, str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.post_reply = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.routes.get((method, url))
        if isinstance(result, Exception):
            raise result
        return result or reply(404)

    def post(self, url, data=None, **kwargs):
        self.posts.append((url, data))
        return self.post_reply

    def hosts(self):
        return {urlsplit(url).hostname for _m, url, _k in self.calls}


def icloud_routes(extra=None):
    routes = {
        ("PROPFIND", "https://caldav.icloud.com/"): reply(
            content=principal("https://caldav.icloud.com/12345/principal/")
        ),
        ("PROPFIND", "https://caldav.icloud.com/12345/principal/"): reply(
            content=home("https://p01-caldav.icloud.com:443/12345/calendars/")
        ),
        ("PROPFIND", "https://p01-caldav.icloud.com:443/12345/calendars/"): reply(content=LISTING),
    }
    routes.update(extra or {})
    return routes


def client(http, user="someone@example.test"):
    return CalDAVClient(user, PASSWORD, http=http)


# --- encrypted secret store -------------------------------------------------------------


def test_encrypted_store_round_trip_and_file_modes(tmp_path):
    root = tmp_path / "config" / "secrets"
    store = SecretStore(root, "caldav", encrypted=True)
    store.set("account", {"password": PASSWORD})
    assert store.get("account") == {"password": PASSWORD}
    blob = (root / "caldav" / "account.enc").read_bytes()
    assert PASSWORD.encode() not in blob and not (root / "caldav" / "account.json").exists()
    key = tmp_path / "config" / "keys" / "secrets.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "caldav" / "account.enc").stat().st_mode) == 0o600
    assert stat.S_IMODE((root / "caldav").stat().st_mode) == 0o700
    assert key.parent != root  # the key is kept apart from the documents
    # A second store instance (another process) reads it with the same key.
    assert SecretStore(root, "caldav").get("account") == {"password": PASSWORD}
    store.delete("account")
    assert store.get("account") is None


def test_encrypted_documents_are_bound_to_their_slot_and_need_the_key(tmp_path):
    root = tmp_path / "c" / "secrets"
    store = SecretStore(root, "caldav", encrypted=True)
    store.set("a", {"v": 1})
    store.set("b", {"v": 2})
    (root / "caldav" / "b.enc").write_bytes((root / "caldav" / "a.enc").read_bytes())
    with pytest.raises(SecretError):
        store.get("b")  # swapped file: wrong AAD
    (tmp_path / "c" / "keys" / "secrets.key").unlink()
    with pytest.raises(SecretError):
        store.get("a")


def test_plaintext_store_is_unchanged_and_upgrades_in_place(tmp_path):
    root = tmp_path / "c" / "secrets"
    plain = SecretStore(root, "withings", encrypted=False)  # what older versions wrote
    plain.set("tokens", {"access": "x"})
    assert json.loads((root / "withings" / "tokens.json").read_text()) == {"access": "x"}
    assert not (tmp_path / "c" / "keys").exists()  # no key needed for plaintext sources
    # An encrypted store still reads a legacy plaintext document, and replaces it on write.
    enc = SecretStore(root, "withings")
    assert enc.get("tokens") == {"access": "x"}
    enc.set("tokens", {"access": "y"})
    assert not (root / "withings" / "tokens.json").exists() and enc.get("tokens") == {"access": "y"}


# --- discovery ----------------------------------------------------------------------------


def test_icloud_discovery_follows_principal_and_home_and_filters(monkeypatch):
    http = FakeHTTP(icloud_routes())
    found = discover("https://caldav.icloud.com/", client(http), "icloud.com")
    assert [c["name"] for c in found.calendars] == ["Home", "Work"]  # no tasks, no inbox
    assert found.calendars[0] == {
        "url": "https://p01-caldav.icloud.com:443/12345/calendars/home/",
        "name": "Home",
        "color": "#ff2968",
    }
    assert found.calendars[1]["color"] is None
    methods = [(m, k["headers"]["Depth"]) for m, _u, k in http.calls]
    assert methods == [("PROPFIND", "0"), ("PROPFIND", "0"), ("PROPFIND", "1")]
    assert all(k["auth"] == ("someone@example.test", PASSWORD) for _m, _u, k in http.calls)
    assert all(k["allow_redirects"] is False for _m, _u, k in http.calls)


def test_icloud_principal_and_home_redirects_persist_partition_host():
    routes = {
        ("PROPFIND", "https://caldav.icloud.com/"): reply(
            302, location="https://p01-caldav.icloud.com/"
        ),
        ("PROPFIND", "https://p01-caldav.icloud.com/"): reply(
            content=principal("/12345/principal/")
        ),
        ("PROPFIND", "https://p01-caldav.icloud.com/12345/principal/"): reply(
            content=home("/12345/calendars/")
        ),
        ("PROPFIND", "https://p01-caldav.icloud.com/12345/calendars/"): reply(
            302, location="https://p02-caldav.icloud.com/12345/calendars/"
        ),
        ("PROPFIND", "https://p02-caldav.icloud.com/12345/calendars/"): reply(content=LISTING),
    }
    found = discover("https://caldav.icloud.com/", client(FakeHTTP(routes)), "icloud.com")
    assert found.home == "https://p02-caldav.icloud.com/12345/calendars/"
    assert [c["url"] for c in found.calendars] == [
        f"https://p02-caldav.icloud.com/12345/calendars/{name}/" for name in ("home", "work")
    ]


def test_well_known_is_tried_when_the_given_url_is_not_caldav():
    http = FakeHTTP(
        {
            ("PROPFIND", "https://dav.example.test/"): reply(404),
            ("PROPFIND", "https://dav.example.test/.well-known/caldav"): reply(
                301, location="/remote.php/dav/"
            ),
            ("PROPFIND", "https://dav.example.test/remote.php/dav/"): reply(
                content=principal("/remote.php/dav/principals/users/me/")
            ),
            ("PROPFIND", "https://dav.example.test/remote.php/dav/principals/users/me/"): reply(
                content=home("/remote.php/dav/calendars/me/")
            ),
            ("PROPFIND", "https://dav.example.test/remote.php/dav/calendars/me/"): reply(
                content=multistatus(calendar_row("Mine", "/remote.php/dav/calendars/me/mine/"))
            ),
        }
    )
    found = discover("https://dav.example.test/", client(http), None)
    assert [c["url"] for c in found.calendars] == [
        "https://dav.example.test/remote.php/dav/calendars/me/mine/"
    ]


def test_server_without_principal_lists_the_given_address_as_home():
    http = FakeHTTP(
        {
            ("PROPFIND", "https://apidata.googleusercontent.com/caldav/v2/"): reply(
                content=multistatus(
                    calendar_row("Me", "/caldav/v2/me%40example.test/events/"),
                )
            ),
        }
    )
    found = discover("https://apidata.googleusercontent.com/caldav/v2/", client(http), None)
    assert found.calendars[0]["url"].endswith("/me%40example.test/events/")


def test_credentials_are_never_sent_outside_the_trusted_hosts():
    evil = {
        ("PROPFIND", "https://caldav.icloud.com/"): reply(301, location="https://evil.test/dav/"),
    }
    http = FakeHTTP(icloud_routes(evil))
    with pytest.raises(CalDAVError) as exc:
        discover("https://caldav.icloud.com/", client(http), "icloud.com")
    assert exc.value.code == "redirect" and http.hosts() == {"caldav.icloud.com"}
    # A look-alike suffix is not inside *.icloud.com.
    look = {
        ("PROPFIND", "https://caldav.icloud.com/"): reply(301, location="https://evilicloud.com/")
    }
    with pytest.raises(CalDAVError):
        discover("https://caldav.icloud.com/", client(FakeHTTP(look)), "icloud.com")
    # Without a trusted domain only the same host is allowed.
    same_only = {
        ("PROPFIND", "https://a.example.test/"): reply(301, location="https://b.example.test/")
    }
    with pytest.raises(CalDAVError) as exc:
        discover("https://a.example.test/", client(FakeHTTP(same_only)), None)
    assert exc.value.code == "redirect"


def test_redirects_are_https_only_and_capped_at_five():
    plain = {("PROPFIND", "https://a.example.test/"): reply(302, location="http://a.example.test/")}
    with pytest.raises(CalDAVError) as exc:
        discover("https://a.example.test/", client(FakeHTTP(plain)), None)
    assert exc.value.code == "redirect"
    with pytest.raises(CalDAVError) as exc:
        discover("http://a.example.test/", client(FakeHTTP()), None)
    assert exc.value.code == "insecure_url"
    hops = {
        ("PROPFIND", f"https://a.example.test/{i}"): reply(302, location=f"/{i + 1}")
        for i in range(10)
    }
    hops[("PROPFIND", "https://a.example.test/")] = reply(302, location="/0")
    http = FakeHTTP(hops)
    with pytest.raises(CalDAVError) as exc:
        discover("https://a.example.test/", client(http), None)
    assert exc.value.code == "redirect" and len(http.calls) == 6  # the start plus five hops


@pytest.mark.parametrize(
    ("route", "code"),
    [
        (reply(401), "auth_failed"),
        (reply(403), "auth_failed"),
        (reply(200, b"<html>hello</html>"), "not_caldav"),
        (reply(207, b"not xml at all"), "not_caldav"),
        (reply(207, b"<x/>"), "not_caldav"),
        (requests.ConnectionError("boom"), "network"),
    ],
)
def test_errors_map_to_codes_and_never_echo_the_password(route, code):
    http = FakeHTTP(
        {
            ("PROPFIND", "https://a.example.test/"): route,
            ("PROPFIND", "https://a.example.test/.well-known/caldav"): route,
        }
    )
    with pytest.raises(CalDAVError) as exc:
        discover("https://a.example.test/", client(http), None)
    assert exc.value.code == code and PASSWORD not in str(exc.value)


def test_bearer_auth_is_sent_as_a_header_not_basic():
    http = FakeHTTP(icloud_routes())
    found = discover(
        "https://caldav.icloud.com/", CalDAVClient(bearer=lambda: "tok-1", http=http), "icloud.com"
    )
    assert found.calendars
    assert all(
        k["auth"] is None and k["headers"]["Authorization"] == "Bearer tok-1"
        for _m, _u, k in http.calls
    )


# --- portal API ---------------------------------------------------------------------------

WRITE = {"x-vibesynergy": "portal", "origin": "http://localhost"}


@pytest.fixture
def portal(runtime, env):
    http = FakeHTTP(icloud_routes())
    rt = Runtime.open(load_settings(env))
    api = TestClient(create_portal_app(rt, caldav_http=http), base_url="http://localhost")
    yield api, http, rt
    rt.close()


def connect(api, **over):
    body = {
        "provider": "icloud",
        "username": "someone@example.test",
        "password": PASSWORD,
        **over,
    }
    return api.post("/api/caldav/connect", json=body, headers=WRITE)


def test_connect_lists_calendars_and_never_returns_secrets(portal):
    api, _http, rt = portal
    reply_ = connect(api)
    assert reply_.status_code == 200, reply_.text
    data = reply_.json()["data"]
    assert data["connected"] and data["password_set"] and data["username"] == "someone@example.test"
    assert [c["name"] for c in data["discovered"]] == ["Home", "Work"]
    assert data["selected"] == []
    assert PASSWORD not in reply_.text and PASSWORD not in api.get("/api/caldav").text
    assert "password" not in api.get("/api/caldav").json()["data"]
    secrets = rt.settings.secrets_dir / "caldav"
    assert PASSWORD.encode() not in (secrets / "account.enc").read_bytes()
    assert not list(secrets.glob("*.json"))
    assert {p["id"] for p in data["providers"]} == {"icloud", "fastmail", "other", "google"}


def test_wrong_password_keeps_the_previous_account_and_returns_a_code(portal):
    api, http, _rt = portal
    connect(api)
    http.routes[("PROPFIND", "https://caldav.icloud.com/")] = reply(401)
    bad = connect(api, password="wrong-one")
    assert bad.status_code == 400 and bad.json()["code"] == "auth_failed"
    assert "wrong-one" not in bad.text and PASSWORD not in bad.text
    assert api.get("/api/caldav").json()["data"]["connected"] is True
    other = connect(api, provider="other", url="http://plain.example.test/")
    assert other.json()["code"] == "insecure_url"
    assert connect(api, provider="google").json()["code"] == "bad_provider"
    assert connect(api, username=" ").json()["code"] == "missing_fields"


def test_selection_only_accepts_discovered_urls_and_is_saved_in_config(portal, env):
    api, _http, _rt = portal
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    rogue = api.post(
        "/api/caldav/calendars",
        json={"urls": [home, "https://p01-caldav.icloud.com:443/12345/calendars/other/"]},
        headers=WRITE,
    )
    assert rogue.status_code == 400 and rogue.json()["code"] == "unknown_calendar"
    assert load_settings(env).source_config("caldav").get("calendars") is None
    by_name = api.post("/api/caldav/calendars", json={"urls": ["Home"]}, headers=WRITE)
    assert by_name.json()["code"] == "unknown_calendar"  # never by name
    empty = api.post("/api/caldav/calendars", json={"urls": []}, headers=WRITE)
    assert empty.json()["code"] == "no_selection"
    ok = api.post("/api/caldav/calendars", json={"urls": [home, home]}, headers=WRITE)
    assert ok.status_code == 200
    chosen = load_settings(env).source_config("caldav")
    assert chosen["enabled"] is True
    assert chosen["calendars"] == [
        {"url": home, "name": "Home", "color": "#ff2968", "classification": "unclassified"}
    ]
    status = api.get("/api/caldav").json()["data"]
    assert [c["selected"] for c in status["discovered"]] == [True, False]
    assert "restart_needed" not in status


def test_garmin_target_requires_selected_calendar_and_clears_with_selection(portal, env):
    api, _http, _rt = portal
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    work = "https://p01-caldav.icloud.com:443/12345/calendars/work/"
    endpoint = "/api/caldav/garmin-target"
    assert (
        api.post(endpoint, json={"url": home}, headers=WRITE).json()["code"] == "unknown_calendar"
    )
    assert connect(api).status_code == 200
    api.post("/api/caldav/calendars", json={"urls": [home, work]}, headers=WRITE)
    assert (
        api.post(endpoint, json={"url": "Home"}, headers=WRITE).json()["code"] == "unknown_calendar"
    )
    saved = api.post(endpoint, json={"url": home}, headers=WRITE)
    assert saved.status_code == 200
    assert saved.json()["data"]["garmin_target_calendar"] == home
    assert load_settings(env).source_config("caldav")["garmin_target_calendar"] == home
    api.post("/api/caldav/calendars", json={"urls": [work]}, headers=WRITE)
    assert api.get("/api/caldav").json()["data"]["garmin_target_calendar"] is None
    assert "garmin_target_calendar" not in load_settings(env).source_config("caldav")
    api.post(endpoint, json={"url": work}, headers=WRITE)
    assert api.post(endpoint, json={"url": None}, headers=WRITE).status_code == 200
    assert "garmin_target_calendar" not in load_settings(env).source_config("caldav")


def test_mirror_switch_requires_a_selected_writable_calendar(portal, env):
    api, _http, _rt = portal
    refused = api.post("/api/caldav/mirror", json={"enabled": True}, headers=WRITE)
    assert refused.status_code == 400 and refused.json()["code"] == "read_only_account"
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    assert api.post("/api/caldav/mirror", json={"enabled": True}, headers=WRITE).json()["code"] == (
        "no_target"
    )
    api.post("/api/caldav/garmin-target", json={"url": home}, headers=WRITE)
    saved = api.post("/api/caldav/mirror", json={"enabled": True}, headers=WRITE)
    assert saved.status_code == 200
    body = saved.json()["data"]
    assert body["mirror_enabled"] is True and body["mirror_active"] is True
    assert PASSWORD not in saved.text
    assert load_settings(env).source_config("caldav")["mirror_enabled"] is True
    assert api.post("/api/caldav/mirror", json={"enabled": False}, headers=WRITE).status_code == 200
    api.post("/api/caldav/mirror", json={"enabled": True}, headers=WRITE)
    cleared = api.post("/api/caldav/garmin-target", json={"url": None}, headers=WRITE)
    assert cleared.json()["data"]["mirror_active"] is False
    assert "mirror_enabled" not in load_settings(env).source_config("caldav")


def test_mirror_preview_lists_pending_changes(portal, monkeypatch):
    from datetime import date

    from sqlalchemy import insert

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.store.schema import dataset_state

    monkeypatch.setattr("vibesynergy.portal.caldav_api.today_in", lambda _: date(2026, 10, 5))
    api, http, rt = portal
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    api.post("/api/caldav/garmin-target", json={"url": home}, headers=WRITE)
    http.routes[("REPORT", home)] = reply(content=multistatus())
    raw = rt.raw.put(
        source="garmin",
        dataset="scheduled_workouts",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    rt.raw.mark_normalised(raw.id)
    with rt.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "workout:17",
                    date(2026, 10, 10),
                    "workout",
                    title="Tempo",
                    extra={"native_calendar_key": "garmin:workout:17"},
                )
            ],
            source="garmin",
            raw_item_id=raw.id,
        )
        conn.execute(
            insert(dataset_state).values(
                source="garmin",
                dataset="scheduled_workouts",
                last_attempt_at=utc_now(),
                last_success_at=utc_now(),
                synced_from=date(2026, 10, 1),
                synced_until=date(2026, 10, 31),
                cursor={
                    "calendar_coverage": {
                        "start": "2026-10-01",
                        "end": "2026-10-31",
                        "completed_at": utc_now().isoformat(),
                        "raw_ids": [raw.id],
                    }
                },
            )
        )
    preview = api.get("/api/caldav/mirror-preview").json()["data"]
    assert preview["error"] is None
    assert preview["counts"]["create"] == 1
    assert preview["counts"]["update"] == 0
    assert preview["counts"]["delete"] == 0
    assert preview["actions"][0]["op"] == "create"
    assert preview["actions"][0]["origin_id"] == "garmin:workout:17"


def test_config_rejects_a_target_outside_selected_calendars(env):
    env.write_text('[sources.caldav]\ngarmin_target_calendar = "https://example.test/other/"\n')
    with pytest.raises(ConfigError, match="Garmin target must be a selected calendar"):
        load_settings(env)


def test_config_rejects_copying_without_a_target(env):
    env.write_text("[sources.caldav]\nmirror_enabled = true\n")
    with pytest.raises(ConfigError, match="needs a selected calendar"):
        load_settings(env)


def test_garmin_sync_reads_live_target(portal, env):
    api, _http, rt = portal
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    connect(api)
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    api.post("/api/caldav/garmin-target", json={"url": home}, headers=WRITE)
    from datetime import date

    from vibesynergy.core.timeutil import Window
    from vibesynergy.sources.garmin.datasets import BY_NAME

    source = rt.registry.get("garmin", require_enabled=False)
    source._api = SimpleNamespace(get_scheduled_workouts=lambda year, month: {"calendarItems": []})
    item = next(
        source._range(
            BY_NAME["scheduled_workouts"].spec, Window(date(2026, 10, 1), date(2026, 10, 1))
        )
    )
    assert item.request["garmin_target_calendar"] == home
    api.post("/api/caldav/garmin-target", json={"url": None}, headers=WRITE)
    item = next(
        source._range(
            BY_NAME["scheduled_workouts"].spec, Window(date(2026, 10, 1), date(2026, 10, 1))
        )
    )
    assert item.request["garmin_target_calendar"] is None


def test_change_calendars_rediscovers_without_reentering_the_password(portal):
    api, http, _rt = portal
    connect(api)
    http.calls.clear()
    again = api.post("/api/caldav/discover", json={}, headers=WRITE)
    assert again.status_code == 200 and http.calls[0][2]["auth"][1] == PASSWORD


def test_disconnect_deletes_credentials_and_selection(portal, env):
    api, _http, rt = portal
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    api.post("/api/caldav/garmin-target", json={"url": home}, headers=WRITE)
    gone = api.post("/api/caldav/disconnect", json={}, headers=WRITE).json()["data"]
    assert gone["connected"] is False and gone["selected"] == [] and gone["discovered"] == []
    assert gone["garmin_target_calendar"] is None
    assert not list((rt.settings.secrets_dir / "caldav").glob("account.*"))
    chosen = load_settings(env).source_config("caldav")
    assert chosen["enabled"] is False and "calendars" not in chosen
    assert "garmin_target_calendar" not in chosen
    assert (
        api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE).status_code == 400
    )


def test_saved_selection_is_scheduled_by_the_running_service_without_restart(portal):
    api, _http, rt = portal
    scheduler = Scheduler(rt.settings, rt.registry, rt.jobs, clock=lambda: 0)
    assert not rt.settings.source_enabled("caldav")  # started without it
    assert "caldav" not in rt.registry.enabled_ids()
    connect(api)
    assert "caldav" not in scheduler.intervals()  # signed in, nothing chosen yet
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    assert "caldav" in rt.registry.enabled_ids()
    assert rt.registry.get("caldav").configured()
    assert scheduler.intervals()["caldav"] == 15
    assert "caldav" in scheduler.run_once()


def test_disconnect_stops_scheduling_without_restart(portal):
    api, _http, rt = portal
    scheduler = Scheduler(rt.settings, rt.registry, rt.jobs, clock=lambda: 0)
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    assert "caldav" in scheduler.intervals()
    api.post("/api/caldav/disconnect", json={}, headers=WRITE)
    assert "caldav" not in rt.registry.enabled_ids()
    assert "caldav" not in scheduler.intervals()
    assert "caldav" not in scheduler.run_once()


def test_writes_need_the_portal_header(portal):
    api, _http, _rt = portal
    body = {"provider": "icloud", "username": "u", "password": "p"}
    assert api.post("/api/caldav/connect", json=body).status_code in (400, 403)


# --- Google OAuth -------------------------------------------------------------------------


@pytest.fixture
def google_portal(runtime, env):
    add_public_url(env, "https://server.example.ts.net")
    http = FakeHTTP(
        {
            ("PROPFIND", "https://apidata.googleusercontent.com/caldav/v2/"): reply(
                content=multistatus(calendar_row("Me", "/caldav/v2/me%40example.test/events/"))
            ),
        }
    )
    rt = Runtime.open(load_settings(env))
    api = TestClient(
        create_portal_app(rt, caldav_http=http), base_url="https://server.example.ts.net:8447"
    )
    api.headers.update({"x-vibesynergy": "portal", "origin": "https://server.example.ts.net:8447"})
    yield api, http, rt
    rt.close()


def token_reply(**over):
    body = {"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600, **over}
    return SimpleNamespace(status_code=200, json=lambda: body)


def start_google(api):
    api.post("/api/caldav/google/app", json={"client_id": "cid", "client_secret": "csecret"})
    url = api.post("/api/caldav/google/start", json={}).json()["data"]["authorization_url"]
    return parse_qs(urlsplit(url).query), url


def test_google_app_secret_is_write_only_and_redirect_uri_shown(google_portal):
    api, _http, rt = google_portal
    api.post("/api/caldav/google/app", json={"client_id": "cid", "client_secret": "csecret"})
    text = api.get("/api/caldav").text
    data = json.loads(text)["data"]
    assert data["google"] == {
        "client_set": True,
        "redirect_uri": "https://server.example.ts.net:8447/api/caldav/google/callback",
    }
    assert "csecret" not in text
    assert b"csecret" not in (rt.settings.secrets_dir / "caldav" / "google_app.enc").read_bytes()


def test_google_start_uses_pkce_state_and_the_narrow_scope(google_portal):
    api, _http, _rt = google_portal
    assert api.post("/api/caldav/google/start", json={}).json()["code"] == "no_google_app"
    query, url = start_google(api)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query["code_challenge_method"] == ["S256"] and query["response_type"] == ["code"]
    assert query["scope"] == ["https://www.googleapis.com/auth/calendar.readonly"]
    assert query["redirect_uri"] == [
        "https://server.example.ts.net:8447/api/caldav/google/callback"
    ]
    assert len(query["state"][0]) >= 40 and len(query["code_challenge"][0]) == 43
    assert query["access_type"] == ["offline"]


def test_google_callback_exchanges_code_with_verifier_and_discovers(google_portal):
    import base64
    import hashlib

    api, http, rt = google_portal
    query, _ = start_google(api)
    http.post_reply = token_reply()
    done = api.get(
        "/api/caldav/google/callback",
        params={"code": "the-code", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/#/settings/calendar?google=ok"
    url, data = http.posts[0]
    assert url == "https://oauth2.googleapis.com/token"
    assert data["grant_type"] == "authorization_code" and data["code"] == "the-code"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(data["code_verifier"].encode()).digest())
    assert challenge.rstrip(b"=").decode() == query["code_challenge"][0]
    discovery_calls = [c for c in http.calls if c[0] == "PROPFIND"]
    assert discovery_calls[0][2]["headers"]["Authorization"] == "Bearer access-1"
    status = api.get("/api/caldav").json()["data"]
    assert status["connected"] and status["provider"] == "google" and status["kind"] == "oauth"
    assert status["discovered"][0]["name"] == "Me"
    assert "access-1" not in api.get("/api/caldav").text
    store = SecretStore(rt.settings.secrets_dir, "caldav")
    assert store.get("tokens")["refresh_token"] == "refresh-1"
    assert not list((rt.settings.secrets_dir / "caldav").glob("*.json"))


def test_google_callback_rejects_bad_state_and_replays(google_portal):
    api, http, _rt = google_portal
    query, _ = start_google(api)
    wrong = api.get(
        "/api/caldav/google/callback",
        params={"code": "c", "state": "forged"},
        follow_redirects=False,
    )
    assert wrong.headers["location"].endswith("google=state_invalid") and not http.posts
    # The login is single-use: even the right state fails after a failed attempt.
    replay = api.get(
        "/api/caldav/google/callback",
        params={"code": "c", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert replay.headers["location"].endswith("google=state_invalid") and not http.posts
    nothing = api.get("/api/caldav/google/callback", params={"code": "c"}, follow_redirects=False)
    assert nothing.headers["location"].endswith("google=state_invalid")


def test_google_callback_expired_login_and_denied(google_portal):
    api, http, rt = google_portal
    query, _ = start_google(api)
    store = account.store_for(rt.settings)
    pending = store.get(google.LOGIN)
    store.set(google.LOGIN, {**pending, "created": time.time() - 3600})
    late = api.get(
        "/api/caldav/google/callback",
        params={"code": "c", "state": query["state"][0]},
        follow_redirects=False,
    )
    assert late.headers["location"].endswith("google=state_invalid")
    start_google(api)
    denied = api.get(
        "/api/caldav/google/callback", params={"error": "access_denied"}, follow_redirects=False
    )
    assert denied.headers["location"].endswith("google=google_denied") and not http.posts
    assert store.get(google.LOGIN) is None


def test_google_token_refresh_is_automatic_and_keeps_the_refresh_token(runtime):
    store = account.store_for(runtime.settings)
    store.set(google.APP, {"client_id": "cid", "client_secret": "csecret"})
    store.set(
        google.TOKENS,
        {"access_token": "old", "refresh_token": "refresh-1", "expires_at": time.time() + 3600},
    )
    http = FakeHTTP()
    assert google.access_token(store, http) == "old" and not http.posts  # still fresh
    store.set(
        google.TOKENS,
        {"access_token": "old", "refresh_token": "refresh-1", "expires_at": time.time() + 10},
    )
    http.post_reply = SimpleNamespace(
        status_code=200, json=lambda: {"access_token": "new", "expires_in": 3600}
    )
    assert google.access_token(store, http) == "new"
    _url, data = http.posts[0]
    assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "refresh-1"
    saved = store.get(google.TOKENS)
    assert saved["access_token"] == "new" and saved["refresh_token"] == "refresh-1"
    assert saved["expires_at"] > time.time() + 3000
    # A revoked grant asks for a new sign-in instead of failing silently.
    store.set(google.TOKENS, {**saved, "expires_at": 0})
    http.post_reply = SimpleNamespace(status_code=400, json=lambda: {"error": "invalid_grant"})
    with pytest.raises(CalDAVError) as exc:
        google.access_token(store, http)
    assert exc.value.code == "reauth"


def test_disconnect_google_removes_tokens(google_portal):
    api, http, rt = google_portal
    query, _ = start_google(api)
    http.post_reply = token_reply()
    api.get(
        "/api/caldav/google/callback",
        params={"code": "c", "state": query["state"][0]},
        follow_redirects=False,
    )
    api.post("/api/caldav/disconnect", json={})
    store = SecretStore(rt.settings.secrets_dir, "caldav")
    assert store.get("tokens") is None and store.get("account") is None


# --- CLI and legacy config ----------------------------------------------------------------


@pytest.mark.parametrize("name", ["caldav"])
def test_cli_auth_for_calendars_points_to_the_portal_and_never_prompts(env, monkeypatch, name):
    def refuse(*_a, **_k):
        raise AssertionError("must not prompt")

    monkeypatch.setattr("builtins.input", refuse)
    monkeypatch.setattr("getpass.getpass", refuse)
    result = CliRunner().invoke(cli, ["auth", name])
    assert result.exit_code != 0 and "Settings → Sources → Calendar" in result.output
    assert not (env.parent / "secrets").exists()


def test_source_authenticate_does_not_prompt(runtime):
    with pytest.raises(ValueError, match="portal"):
        runtime.registry.get("caldav", require_enabled=False).authenticate()


def test_retired_icloud_section_is_ignored_with_one_warning(tmp_path, caplog, monkeypatch):
    from vibesynergy.core import config

    monkeypatch.setattr(config, "_RETIRED_SOURCES_WARNED", set())
    path = tmp_path / "config.toml"
    path.write_text("[sources.icloud]\nenabled = false\n[schedule]\nicloud = 20\n")
    with caplog.at_level("WARNING", logger="vibesynergy.core.config"):
        settings = load_settings(path)
        load_settings(path)
    assert "icloud" not in settings.sources and settings.schedule == {}
    assert not settings.source_enabled("caldav")  # no longer an alias
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 2 and all("icloud is ignored" in w for w in warnings)  # one each
    # The CalDAV section is read as before, next to the ignored one.
    path.write_text("[sources.icloud]\nenabled = false\n[sources.caldav]\nenabled = true\n")
    assert load_settings(path).source_enabled("caldav")
    rt = Runtime.open(replace(load_settings(tmp_path / "config.toml"), data_dir=tmp_path / "d"))
    try:
        assert "caldav" in rt.registry.available() and "icloud" not in rt.registry.available()
    finally:
        rt.close()


def test_retired_icloud_leftovers_are_not_sync_problems(runtime):
    """An old icloud/calendar failure in dataset_state is not shown, and a queued sync job for
    icloud ends quietly instead of failing on an unknown source."""
    from sqlalchemy import insert, select

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.query import views
    from vibesynergy.store.schema import dataset_state, job

    with runtime.engine.begin() as conn:
        conn.execute(
            insert(dataset_state).values(
                source="icloud",
                dataset="calendar",
                last_attempt_at=utc_now(),
                last_error="ValueError: Select at least one iCloud calendar and a date window",
            )
        )
    status = views.sync_status(runtime.engine)
    assert all(d["source"] != "icloud" for d in status["datasets"])
    assert all(d["source"] != "icloud" for d in status["problems"])
    assert "icloud" not in views.freshness(runtime.engine)
    assert all(s["source"] != "icloud" for s in runtime.sync.states())

    job_id = runtime.jobs.enqueue("sync", {"source": "icloud", "dataset": None, "window": None})
    assert runtime.sync.skip_reason("icloud") == "retired"
    assert runtime.worker().run_once()
    with runtime.engine.connect() as conn:
        assert conn.execute(select(job.c.status).where(job.c.id == job_id)).scalar() == "done"


def test_source_syncs_with_the_stored_encrypted_account(runtime, env):
    """The scheduler's source builds its client from the encrypted store, not from config."""
    import requests as real

    store = account.store_for(runtime.settings)
    store.set(
        account.ACCOUNT,
        {"provider": "icloud", "kind": "password", "username": "u", "password": PASSWORD},
    )
    source = runtime.registry.get("caldav", require_enabled=False)
    seen = {}

    class Session:
        def __init__(self):
            seen["session"] = self

        def request(self, method, url, **kwargs):
            seen["auth"] = kwargs["auth"]
            return reply(207, multistatus())

    real_session = real.Session
    real.Session = Session
    try:
        assert source.client.snapshot("https://c.example.test/cal/") == []
    finally:
        real.Session = real_session
    assert seen["auth"] == ("u", PASSWORD)
    store.delete(account.ACCOUNT)
    with pytest.raises(ValueError, match="Settings → Sources → Calendar"):
        source.client  # noqa: B018


def test_classification_saves_directly_preserves_mirrors_and_never_counts_load(portal):
    from datetime import date

    from vibesynergy.core.timeutil import Window
    from vibesynergy.model import PlannedItem
    from vibesynergy.normalize.normaliser import write_records
    from vibesynergy.query.plan import plan

    api, _http, rt = portal
    connect(api)
    home = "https://p01-caldav.icloud.com:443/12345/calendars/home/"
    api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE)
    raw = rt.raw.put(
        source="caldav",
        dataset="calendar",
        item_key="synthetic",
        payload=b"{}",
        content_type="application/json",
    )
    day = date(2026, 10, 10)
    with rt.engine.begin() as conn:
        write_records(
            conn,
            [
                PlannedItem(
                    "personal",
                    day,
                    "commitment",
                    title="Synthetic Pilates",
                    extra={
                        "calendar_url": home,
                        "classification": "unclassified",
                        "counts_as_exercise": False,
                    },
                ),
                PlannedItem(
                    "mirror",
                    day,
                    "mirror",
                    title="Synthetic Garmin",
                    extra={
                        "calendar_url": home,
                        "mirror_key": "garmin:workout:1",
                        "classification": "mirror",
                    },
                ),
            ],
            source="caldav",
            raw_item_id=raw.id,
        )
    response = api.post(
        "/api/caldav/classification",
        json={
            "url": home,
            "classification": "exercise",
        },
        headers=WRITE,
    )
    assert response.status_code == 200
    assert response.json()["data"]["selected"][0]["classification"] == "exercise"
    result = plan(rt.engine, Window(day, day), day)
    personal = next(i for i in result["items"] if i["source_id"] == "personal")
    assert personal["extra"]["classification"] == "exercise"
    assert personal["extra"]["counts_as_exercise"] is False
    assert personal["kind"] == "commitment" and personal["write_authority"] is False
    mirror = next(i for i in result["items"] if i["source_id"] == "mirror")
    assert mirror["extra"]["classification"] == "mirror"
    saved = api.post("/api/caldav/calendars", json={"urls": [home]}, headers=WRITE).json()
    assert saved["data"]["selected"][0]["classification"] == "exercise"
    assert (
        api.post(
            "/api/caldav/classification",
            json={
                "url": home,
                "classification": "mirror",
            },
            headers=WRITE,
        ).status_code
        == 400
    )
    assert (
        api.post(
            "/api/caldav/classification",
            json={
                "url": "https://unknown.test/calendar/",
                "classification": "exercise",
            },
            headers=WRITE,
        ).status_code
        == 400
    )
