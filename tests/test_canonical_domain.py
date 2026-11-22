"""The app's own domain sets every link; other names are aliases (#117).

Links in mail, notices, push, on Settings and in MCP results use the canonical address,
whatever name the request came in on. Aliases are an explicit list from the config: they are
accepted as host and origin (DNS rebinding, CSRF, OAuth return), but never used for links. A
Host header outside the list is refused before anything is built from it.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from vibesynergy import alerts, webpush
from vibesynergy.core.config import ConfigError, load_settings
from vibesynergy.portal.app import create_portal_app
from vibesynergy.portal.hosts import (
    aliases,
    allowed_hosts,
    canonical_url,
    redirect_target,
    validate_portal_address,
)
from vibesynergy.query import auth
from vibesynergy.runtime import Runtime

OWN = "app.example.org"
TAILNET = "box.example.ts.net"
ALIAS = "old.example.net"
EVIL = "evil.example.com"
EMAIL = "athlete@example.com"
WRITE = {"x-vibesynergy": "portal"}


def _config(tmp_path: Path, server: str = "", portal: str = "") -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '[user]\ntimezone = "Europe/Amsterdam"\n\n'
        f'[server]\npublic_url = "https://{TAILNET}"\n{server}\n'
        f'[portal]\nport = 8467\nhosts = ["{OWN}"]\n{portal}\n'
    )
    return path


@pytest.fixture
def make_rt(runtime, tmp_path):
    opened = []

    def make(server: str = "", portal: str = "") -> Runtime:
        settings = load_settings(_config(tmp_path, server, portal))
        rt = Runtime.open(replace(settings, data_dir=runtime.settings.data_dir))
        opened.append(rt)
        return rt

    yield make
    for rt in opened:
        rt.close()


@pytest.fixture
def mail(monkeypatch):
    from vibesynergy import mailer

    sent = []
    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda settings, to, subject, body, html=None: sent.append((to, subject, body)),
    )
    return sent


def _client(rt: Runtime, host: str) -> TestClient:
    return TestClient(create_portal_app(rt), base_url=f"https://{host}")


# --- the addresses ------------------------------------------------------------------------


def _settings(server=None, public_url=f"https://{TAILNET}", **portal):
    return SimpleNamespace(server=server or {}, public_url=public_url, portal=portal)


def test_canonical_is_the_explicit_setting_then_the_own_domain_then_the_tailnet() -> None:
    explicit = _settings({"canonical_url": "https://vibe.example.org/"}, hosts=[OWN])
    assert canonical_url(explicit) == "https://vibe.example.org"
    assert canonical_url(_settings(hosts=["localhost", OWN])) == f"https://{OWN}"
    assert canonical_url(_settings(port=8467)) == f"https://{TAILNET}:8467"
    assert canonical_url(_settings(public_url=None)) is None


def test_aliases_are_the_other_configured_names_never_the_canonical_one() -> None:
    settings = _settings({"aliases": [ALIAS, OWN.upper()]}, port=8467, hosts=[OWN, "b.example.org"])
    assert aliases(settings) == [
        f"https://{TAILNET}:8467",
        "https://b.example.org",
        f"https://{ALIAS}",
    ]
    assert allowed_hosts(settings) >= {OWN, TAILNET, ALIAS, "b.example.org", "localhost"}
    assert EVIL not in allowed_hosts(settings)
    # An explicit canonical address is accepted as a host too, and the domain becomes an alias.
    explicit = _settings({"canonical_url": "https://vibe.example.org"}, hosts=[OWN])
    assert "vibe.example.org" in allowed_hosts(explicit)
    assert f"https://{OWN}" in aliases(explicit)


def test_the_redirect_only_moves_aliases_and_only_when_switched_on() -> None:
    off = _settings({"aliases": [ALIAS]}, hosts=[OWN])
    on = _settings({"aliases": [ALIAS], "redirect_aliases": True}, hosts=[OWN])
    assert redirect_target(off, ALIAS) is None
    assert redirect_target(on, ALIAS) == redirect_target(on, TAILNET) == f"https://{OWN}"
    assert redirect_target(on, OWN) is None
    assert redirect_target(on, "localhost") is redirect_target(on, "127.0.0.1") is None


@pytest.mark.parametrize(
    ("server", "message"),
    [
        ('canonical_url = "http://app.example.org"', "canonical_url"),
        ('canonical_url = "https://app.example.org/portal"', "canonical_url"),
        ('canonical_url = "https://user:pw@app.example.org"', "canonical_url"),
        ('aliases = "box.example.ts.net"', "aliases"),
        ('aliases = ["https://box.example.ts.net"]', "aliases"),
        ('aliases = ["box.example.ts.net:8447"]', "aliases"),
        ('redirect_aliases = "yes"', "redirect_aliases"),
    ],
)
def test_bad_address_settings_are_refused(tmp_path, server, message) -> None:
    with pytest.raises(ConfigError, match=message):
        load_settings(_config(tmp_path, server))


def test_good_address_settings_load(tmp_path) -> None:
    server = (
        f'canonical_url = "https://{OWN}"\naliases = ["{TAILNET}", "{ALIAS}"]\n'
        "redirect_aliases = true"
    )
    settings = load_settings(_config(tmp_path, server))
    assert canonical_url(settings) == f"https://{OWN}"


# --- links built on an alias request ------------------------------------------------------


def test_login_and_reset_mails_from_an_alias_point_to_the_own_domain(make_rt, mail) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    acc = auth.create_account(rt.engine, EMAIL)
    auth.set_password(rt.engine, acc["id"], "lange-zin-met-fietsen-2027")
    for host in (f"{TAILNET}:8467", ALIAS, "localhost:8467"):
        client = _client(rt, host)
        assert client.post("/auth/link", json={"email": EMAIL}, headers=WRITE).status_code == 200
        assert f"https://{OWN}/login#link=" in mail[-1][2]
        assert TAILNET not in mail[-1][2] and ALIAS not in mail[-1][2]
        client.post("/auth/reset", json={"email": EMAIL}, headers=WRITE)
        assert f"https://{OWN}/login#reset=" in mail[-1][2]


def test_a_link_mailed_from_the_tailnet_logs_you_in_on_the_own_domain(make_rt, mail) -> None:
    rt = make_rt()
    auth.create_account(rt.engine, EMAIL)
    _client(rt, f"{TAILNET}:8467").post("/auth/link", json={"email": EMAIL}, headers=WRITE)
    token = mail[-1][2].split("#link=", 1)[1].split()[0]
    own = _client(rt, OWN)
    used = own.post(
        "/auth/link/use",
        json={"token": token},
        headers={**WRITE, "origin": f"https://{OWN}", "x-forwarded-proto": "https"},
    )
    assert used.status_code == 200
    cookie = used.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "domain=" not in cookie


def test_a_spoofed_host_is_refused_and_gets_no_mail(make_rt, mail) -> None:
    rt = make_rt()
    auth.create_account(rt.engine, EMAIL)
    reply = _client(rt, EVIL).post("/auth/link", json={"email": EMAIL}, headers=WRITE)
    assert reply.status_code == 421 and not mail
    # A forwarded host does not count either: links never read the request's names.
    sneaky = _client(rt, OWN).post(
        "/auth/link",
        json={"email": EMAIL},
        headers={**WRITE, "x-forwarded-host": EVIL, "x-forwarded-proto": "http"},
    )
    assert sneaky.status_code == 200 and mail[-1][2].count(f"https://{OWN}/login#link=") == 1
    assert EVIL not in mail[-1][2]


def test_settings_on_an_alias_show_the_own_domain_and_the_aliases(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    data = _client(rt, ALIAS).get("/api/settings").json()["data"]
    ro = data["read_only"]
    assert ro["canonical_url"] == ro["portal_url"] == f"https://{OWN}"
    assert ro["aliases"] == [f"https://{TAILNET}:8467", f"https://{ALIAS}"]
    assert ro["redirect_aliases"] is False
    # The public gateway for outside services is the Funnel; it is shown as such.
    assert ro["mcp_url"] == f"https://{TAILNET}/vibesynergy/mcp"


def test_notices_mails_and_push_use_the_own_domain(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    assert alerts.portal_url(rt, "#/inbox") == f"https://{OWN}/#/inbox"
    assert alerts._footer(rt) == f"\n\n— VibeSynergy · https://{OWN}/#/dashboard"
    assert webpush.absolute_url(rt.settings, "/#/inbox") == f"https://{OWN}/#/inbox"
    # Only the portal's own pages: anything else opens the start page.
    assert webpush.absolute_url(rt.settings, "https://evil.example.com/") == f"https://{OWN}/"
    assert webpush.absolute_url(rt.settings, "//evil.example.com/") == f"https://{OWN}/"
    no_address = SimpleNamespace(server={}, public_url=None, portal={})
    assert webpush.absolute_url(no_address, "/#/inbox") == "/#/inbox"


def test_push_payload_carries_the_canonical_address(make_rt, monkeypatch) -> None:
    rt = make_rt()
    seen = []
    monkeypatch.setattr(
        webpush,
        "subscriptions",
        lambda engine: [{"id": 1, "endpoint": "https://push.example/1", "p256dh": "", "auth": ""}],
    )
    monkeypatch.setattr(webpush, "unb64u", lambda value: b"")
    monkeypatch.setattr(
        webpush, "encrypt", lambda payload, *keys: seen.append(json.loads(payload)) or b""
    )
    webpush.send_all(
        rt, "t", "b", url="/#/inbox", http=lambda *a, **k: SimpleNamespace(status_code=201)
    )
    assert seen and seen[0]["url"] == f"https://{OWN}/#/inbox"


def test_mcp_results_carry_the_canonical_portal_url(make_rt) -> None:
    from mcp import Client

    from vibesynergy.mcp.server import build_server

    rt = make_rt(f'aliases = ["{ALIAS}"]')

    async def go():
        async with Client(build_server(rt)) as client:
            return await client.call_tool("vibesynergy_sync_status", {})

    result = anyio.run(go)
    content = result.structured_content or json.loads(result.content[0].text)
    assert content["meta"]["portal_url"] == f"https://{OWN}"


def test_the_cli_login_link_uses_the_own_domain(make_rt) -> None:
    from vibesynergy.cli.main import _portal_base

    assert _portal_base(make_rt()) == f"https://{OWN}"
    explicit = make_rt('canonical_url = "https://vibe.example.org"')
    assert _portal_base(explicit) == "https://vibe.example.org"


# --- aliases are accepted for CSRF / Origin checks and OAuth return -----------------------


def test_an_alias_origin_may_write_and_a_foreign_one_may_not(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    change = {"changes": {"user": {"units": "metric"}}}
    for host in (OWN, ALIAS, TAILNET):
        client = _client(rt, host)
        ok = client.post(
            "/api/settings", json=change, headers={**WRITE, "origin": f"https://{host}"}
        )
        assert ok.status_code == 200, host
    foreign = _client(rt, ALIAS).post(
        "/api/settings", json=change, headers={**WRITE, "origin": f"https://{EVIL}"}
    )
    assert foreign.status_code == 403
    assert _client(rt, EVIL).get("/api/settings").status_code == 421


def test_oauth_returns_to_an_alias_but_never_to_an_unlisted_host(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    assert validate_portal_address(rt.settings, f"https://{ALIAS}/#/settings") == (
        f"https://{ALIAS}/"
    )
    assert validate_portal_address(rt.settings, f"https://{OWN}/") == f"https://{OWN}/"
    with pytest.raises(ValueError, match="not allowed"):
        validate_portal_address(rt.settings, f"https://{EVIL}/")


def test_the_mcp_host_check_accepts_listed_aliases_only(make_rt) -> None:
    from vibesynergy.api.app import mcp_allowed_hosts

    hosts = mcp_allowed_hosts(make_rt(f'aliases = ["{ALIAS}", "{TAILNET}"]').settings)
    assert hosts == ["127.0.0.1:*", "localhost:*", TAILNET, ALIAS]
    assert EVIL not in hosts and OWN not in hosts  # the portal's domain does not serve MCP


# --- the optional redirect to the canonical address ---------------------------------------


def test_portal_pages_on_an_alias_redirect_when_switched_on(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]\nredirect_aliases = true')
    alias = _client(rt, ALIAS)
    page = alias.get("/login?next=/", follow_redirects=False)
    assert page.status_code == 308
    assert page.headers["location"] == f"https://{OWN}/login?next=/"
    assert alias.get("/", follow_redirects=False).headers["location"] == f"https://{OWN}/"
    # The API, files and the Google return address keep working on the alias.
    assert alias.get("/version").status_code == 200
    assert alias.get("/api/settings").status_code == 200
    # The own domain and this computer are served as they are; a spoofed host is refused.
    assert _client(rt, OWN).get("/login").status_code == 200
    assert _client(rt, "localhost:8467").get("/login").status_code == 200
    spoofed = _client(rt, EVIL).get("/login", follow_redirects=False)
    assert spoofed.status_code == 421 and "location" not in spoofed.headers


def test_without_the_setting_an_alias_is_served_as_before(make_rt) -> None:
    rt = make_rt(f'aliases = ["{ALIAS}"]')
    assert _client(rt, ALIAS).get("/login", follow_redirects=False).status_code == 200
