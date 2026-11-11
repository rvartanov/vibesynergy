"""Settings -> System: portal (sub)domains in ``[portal] hosts``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vibesynergy.core.config import load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.portal.hosts import normalize_hostname
from vibesynergy.runtime import Runtime

HERE = "vibesynergy.example.org"
OTHER = "vibesynergy.example.net"


@pytest.fixture
def hosts_portal(env):
    env.write_text(
        env.read_text()
        + '\n[server]\npublic_url = "https://server.example.ts.net"\n'
        + f'\n[portal]\nport = 8447  # keep this comment\nhosts = ["{HERE}", "{OTHER}"]\n'
    )
    rt = Runtime.open(load_settings(env))
    rt.migrate()
    client = TestClient(create_portal_app(rt), base_url=f"https://{HERE}")
    yield env, client
    rt.close()


def _post(client, body):
    write = {"x-vibesynergy": "portal", "origin": f"https://{HERE}"}
    return client.post("/api/settings/portal_hosts", json=body, headers=write)


def test_settings_show_every_address(hosts_portal) -> None:
    _, client = hosts_portal
    data = client.get("/api/settings").json()["data"]
    assert data["portal_hosts"] == [HERE, OTHER]
    assert data["read_only"]["public_url"] == "https://server.example.ts.net"
    assert data["read_only"]["local_url"] == "http://127.0.0.1:8447"


def test_add_and_remove_a_domain(hosts_portal) -> None:
    path, client = hosts_portal
    reply = _post(client, {"action": "add", "host": " Coach.Example.ORG. "})
    assert reply.status_code == 200, reply.text
    assert reply.json()["data"]["portal_hosts"] == [HERE, OTHER, "coach.example.org"]
    assert "# keep this comment" in path.read_text()  # tomlkit, not a rewrite
    assert "system" in reply.json()["data"]["restart_tabs"]  # applies after a restart
    reply = _post(client, {"action": "remove", "host": OTHER})
    assert reply.status_code == 200, reply.text
    assert load_settings(path).portal["hosts"] == [HERE, "coach.example.org"]
    assert list(path.parent.glob("config.toml.bak-*"))  # the previous file is kept


@pytest.mark.parametrize(
    ("host", "message"),
    [
        ("", "Enter a domain name"),
        ("https://x.example.org/", "not a valid domain name"),
        ("x.example.org:8443", "not a valid domain name"),
        ("10.0.0.7", "not an IP address"),
        ("localhost", "not a valid domain name"),
        ("bad_name.example.org", "not a valid domain name"),
        (HERE, "already allowed"),
        ("server.example.ts.net", "already allowed"),
    ],
)
def test_bad_domains_are_refused(hosts_portal, host, message) -> None:
    path, client = hosts_portal
    before = path.read_text()
    reply = _post(client, {"action": "add", "host": host})
    assert reply.status_code == 400 and message in reply.json()["error"]
    assert path.read_text() == before


def test_removing_the_host_in_use_needs_a_second_yes(hosts_portal) -> None:
    path, client = hosts_portal
    reply = _post(client, {"action": "remove", "host": HERE})
    assert reply.status_code == 400
    assert "You opened this page on that address" in reply.json()["error"]
    assert load_settings(path).portal["hosts"] == [HERE, OTHER]
    reply = _post(client, {"action": "remove", "host": HERE, "confirm": True})
    assert reply.status_code == 200, reply.text
    assert load_settings(path).portal["hosts"] == [OTHER]
    assert _post(client, {"action": "remove", "host": "nope.example.org"}).status_code == 400


def test_hostname_rules() -> None:
    assert normalize_hostname("münchen.example") == "xn--mnchen-3ya.example"
    for bad in ("a..b", "-a.example", "a.123", "x" * 64 + ".example"):
        with pytest.raises(ValueError):
            normalize_hostname(bad)
