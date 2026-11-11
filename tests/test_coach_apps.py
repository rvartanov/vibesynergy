"""Coach apps in the portal: tokens, passphrase and activity; the secret link is gone."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from fastapi.testclient import TestClient

from vibesynergy.api.app import create_app
from vibesynergy.api.oauth import create_personal_access_token
from vibesynergy.core.config import load_settings, write_default_config
from vibesynergy.mcp.identity import reset_connection, set_connection
from vibesynergy.mcp.server import build_server
from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import coach_workouts
from vibesynergy.runtime import Runtime
from vibesynergy.store.schema import audit_log

HOST = "https://server.example.ts.net"
WRITE = {"x-vibesynergy": "portal", "origin": HOST}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "1"},
    },
}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


def _portal(runtime):
    settings = replace(runtime.settings, server={"public_url": HOST})
    rt = Runtime.open(settings)
    return rt, TestClient(create_portal_app(rt), base_url=HOST)


def test_secret_link_setting_is_gone_from_new_configs_and_ignored(tmp_path, caplog) -> None:
    from vibesynergy.core import config

    path = tmp_path / "config.toml"
    write_default_config(path, "Europe/Amsterdam")
    assert "mcp_secret_url" not in path.read_text()
    assert "mcp_secret_url" not in load_settings(path).server
    path.write_text(path.read_text().replace("[server]", "[server]\nmcp_secret_url = true"))
    config._SECRET_URL_WARNED = False
    with caplog.at_level("WARNING", logger="vibesynergy.core.config"):
        load_settings(path)
        load_settings(path)
    warnings = [r for r in caplog.records if "mcp_secret_url is ignored" in r.getMessage()]
    assert len(warnings) == 1


def test_pat_created_in_the_ui_is_listed_without_the_secret(runtime) -> None:
    rt, client = _portal(runtime)
    try:
        phrase = client.get("/api/mcp/passphrase").json()["data"]
        assert phrase == {"set": False}
        saved = client.post(
            "/api/mcp/passphrase",
            json={"new": "correct horse battery"},
            headers=WRITE,
        )
        assert saved.status_code == 200, saved.text
        created = client.post(
            "/api/mcp/pats",
            json={
                "name": "Claude",
                "scopes": ["read", "coach.write"],
                "days": 90,
                "passphrase": "correct horse battery",
            },
            headers=WRITE,
        )
        assert created.status_code == 200, created.text
        token = created.json()["data"]["token"]
        assert token.startswith("vit_pat_")
        listed = client.get("/api/mcp/pats")
        body = listed.json()["data"]
        assert token not in listed.text
        row = body["tokens"][0]
        assert row["name"] == "Claude"
        assert row["last_used"] is None
        assert row["expires_at"]
        assert "publish" not in row["scopes"]
        assert "sql" not in row["scopes"]
        names = body["names"]
        assert names[f"client:pat:{row['id']}"] == "Claude"
        assert client.get("/api/mcp/passphrase").json()["data"] == {"set": True}
        assert "correct horse battery" not in client.get("/api/mcp/passphrase").text
        wrong = client.post(
            "/api/mcp/passphrase",
            json={"new": "another horse battery", "current": "nope nope nope"},
            headers=WRITE,
        )
        assert wrong.status_code == 400
        with TestClient(create_app(rt), base_url=HOST) as api:
            auth = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
            assert api.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 200
            again = client.get("/api/mcp/pats").json()["data"]["tokens"][0]
            assert again["last_used"]
            revoked = client.post("/api/mcp/pats/revoke", json={"id": row["id"]}, headers=WRITE)
            assert revoked.status_code == 200
            assert api.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 401
    finally:
        rt.close()


def test_creating_a_token_needs_the_passphrase_with_a_rate_limit(runtime) -> None:
    """the tailnet alone cannot mint a token; 5 wrong tries lock it for 15 minutes."""
    from sqlalchemy import select

    from vibesynergy.api import oauth

    rt, client = _portal(runtime)
    body = {"name": "Mine", "scopes": ["read"], "days": 30}
    try:
        no_phrase = client.post("/api/mcp/pats", json=body, headers=WRITE)
        assert no_phrase.status_code == 400
        assert "Set the coach passphrase first." in no_phrase.text
        client.post("/api/mcp/passphrase", json={"new": "correct horse battery"}, headers=WRITE)
        missing = client.post("/api/mcp/pats", json=body, headers=WRITE)
        assert missing.status_code == 400  # an empty passphrase counts as a wrong one
        for attempt in range(oauth.MAX_FAILURES - 1):
            wrong = client.post(
                "/api/mcp/pats", json={**body, "passphrase": f"wrong {attempt}"}, headers=WRITE
            )
            assert wrong.status_code == 400
            assert "does not match" in wrong.text
        locked = client.post(
            "/api/mcp/pats", json={**body, "passphrase": "correct horse battery"}, headers=WRITE
        )
        assert locked.status_code == 400
        assert "Too many wrong attempts" in locked.text  # even the right one
        assert client.get("/api/mcp/pats").json()["data"]["tokens"] == []
        # Reading Settings is unaffected.
        assert client.get("/api/mcp_clients").status_code == 200
        with rt.engine.connect() as conn:
            actions = conn.execute(
                select(audit_log.c.action).where(audit_log.c.actor == "portal")
            ).scalars()
            assert "portal.passphrase.denied" in set(actions)
    finally:
        rt.close()


def test_a_token_is_created_with_the_right_passphrase(runtime) -> None:
    rt, client = _portal(runtime)
    try:
        client.post("/api/mcp/passphrase", json={"new": "correct horse battery"}, headers=WRITE)
        created = client.post(
            "/api/mcp/pats",
            json={
                "name": "Coach",
                "scopes": ["read"],
                "days": 30,
                "passphrase": "correct horse battery",
            },
            headers=WRITE,
        )
        assert created.status_code == 200, created.text
        assert "correct horse battery" not in created.text
    finally:
        rt.close()


def test_activity_names_the_client_and_can_revoke(runtime) -> None:
    rt, client = _portal(runtime)
    try:
        token = create_personal_access_token(
            rt.engine, "AI coach", scopes=["coach.write", "read"], days=90, display_name="AI coach"
        )
        listed = client.get("/api/mcp_clients").json()["data"]
        pat = next(row for row in listed["tokens"] if row["name"] == "AI coach")
        tokens = set_connection(
            f"pat:{pat['id']}", label="AI coach", scopes=frozenset({"coach.write"})
        )
        try:
            coach_workouts.coach_write(
                rt.engine,
                entry_type="note",
                summary="Skip the tempo.",
                idempotency_key="task-d-note",
                for_date=date(2026, 10, 16),
            )
        finally:
            reset_connection(tokens)
        # The audit row is written by the MCP tool wrapper, so call the tool too.
        import anyio
        from mcp import Client

        async def go():
            bound = set_connection(
                f"pat:{pat['id']}", label="AI coach", scopes=frozenset({"coach.write", "read"})
            )
            try:
                async with Client(build_server(rt)) as mcp:
                    return await mcp.call_tool(
                        "vibesynergy_coach_log", {"text": "Keep Saturday easy."}
                    )
            finally:
                reset_connection(bound)

        result = anyio.run(go)
        assert not result.is_error
        with rt.engine.connect() as conn:
            rows = (
                conn.execute(
                    audit_log.select().where(audit_log.c.action == "vibesynergy_coach_log")
                )
                .mappings()
                .all()
            )
        assert rows
        assert rows[-1]["client_id"] == f"pat:{pat['id']}"
        assert rows[-1]["author"] == f"client:pat:{pat['id']}"
        assert rows[-1]["scope"] == "coach.write"
        activity = client.get("/api/mcp/activity").json()["data"]["events"]
        match = next(event for event in activity if event["action"] == "vibesynergy_coach_log")
        assert match["client_id"] == f"pat:{pat['id']}"
        assert match["author"] == f"client:pat:{pat['id']}"
        revoked = client.post("/api/mcp/pats/revoke", json={"id": pat["id"]}, headers=WRITE)
        assert revoked.status_code == 200
        with TestClient(create_app(rt), base_url=HOST) as api:
            auth = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
            assert api.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 401
    finally:
        rt.close()


def test_secret_path_mcp_is_404_and_oauth_pat_endpoint_still_works(runtime) -> None:
    """Even a config that still says ``mcp_secret_url = true`` mounts no secret path."""
    settings = replace(runtime.settings, server={"public_url": HOST, "mcp_secret_url": True})
    rt = Runtime.open(settings)
    try:
        token = create_personal_access_token(rt.engine, "Claude", scopes=["read"], days=30)
        with TestClient(create_app(rt), base_url=HOST) as api:
            for path in ("/vibesynergy/anything/mcp", "/anything/mcp", "/vibesynergy/x/y/mcp"):
                reply = api.post(path, json=INIT, headers=MCP_HEADERS)
                assert reply.status_code == 404, path
            assert api.post("/vibesynergy/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
            auth = {**MCP_HEADERS, "authorization": f"Bearer {token}"}
            assert api.post("/vibesynergy/mcp", json=INIT, headers=auth).status_code == 200
            assert api.get("/vibesynergy/.well-known/oauth-authorization-server").status_code == 200
        portal = TestClient(create_portal_app(rt), base_url=HOST)
        data = portal.get("/api/mcp_clients").json()["data"]
        assert "legacy" not in data and "path_clients" not in data
        for gone in ("/api/mcp/legacy", "/api/mcp/sherpa-token"):
            assert portal.post(gone, json={"enabled": True}, headers=WRITE).status_code in {
                404,
                405,
            }
    finally:
        rt.close()


def test_token_expiry_alert_is_the_seven_days_before(runtime) -> None:
    from sqlalchemy import update

    from vibesynergy import alerts
    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.store.schema import mcp_pat

    create_personal_access_token(runtime.engine, "Soon", scopes=["read"], days=30)
    create_personal_access_token(runtime.engine, "Later", scopes=["read"], days=90)
    now = utc_now()
    with runtime.engine.begin() as conn:
        conn.execute(
            update(mcp_pat)
            .where(mcp_pat.c.name == "Soon")
            .values(expires_at=now + timedelta(days=6))
        )
        conn.execute(
            update(mcp_pat)
            .where(mcp_pat.c.name == "Later")
            .values(expires_at=now + timedelta(days=40))
        )
    found = alerts._expiring_tokens(runtime)
    assert [alert.kind for alert in found] == ["token"]
    assert found[0].link == "#/settings/sharing"
    assert "Soon" in found[0].text
    assert "Later" not in found[0].text
    assert "-" in found[0].text  # d-m-yyyy, not an ISO date


def test_sharing_lists_only_live_access_and_logs_the_ends(runtime) -> None:
    """ended tokens, apps and share links leave the lists; the log names each end."""
    from vibesynergy.api import oauth
    from vibesynergy.portal import share

    rt, client = _portal(runtime)
    try:
        oauth.create_personal_access_token(rt.engine, "Old coach", days=30)
        assert oauth.revoke_personal_access_token(rt.engine, "Old coach")
        _, live = share.create(rt.engine, "Coach Anna", ["training"])
        _, gone = share.create(rt.engine, "Old physio", ["training"])
        assert share.revoke(rt.engine, gone["id"])
        links = client.get("/api/share_links").json()["data"]["links"]
        assert [link["title"] for link in links] == ["Coach Anna"]
        events = client.get("/api/mcp/activity").json()["data"]["events"]
        ends = {(e["action"], e["author"]) for e in events if e["action"].startswith("access.")}
        assert ends == {
            ("access.token_revoked", "Old coach"),
            ("access.link_revoked", "Old physio"),
        }
        assert live["state"] == "active"
    finally:
        rt.close()
