from typer.testing import CliRunner

from vibesynergy.cli.main import app

runner = CliRunner()


def run(*args: str):
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_init_sync_status(env) -> None:
    assert "schema at revision 0047" in run("init")
    assert "revision 0047" in run("init")  # idempotent
    out = run("sync", "demo", "--start", "2026-03-01", "--end", "2026-03-03")
    assert "demo.daily" in out and "ok" in out
    status = run("status")
    assert "demo.daily" in status and "Raw archive" in status


def test_commands_refuse_unmigrated_db(env) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "vibesynergy init" in result.output


def test_queue_then_worker(env) -> None:
    run("init")
    assert "job 1" in run("sync", "demo", "--days", "2", "--queue")
    assert "already queued" in run("sync", "demo", "--days", "2", "--queue")
    assert "ran 1 jobs" in run("worker", "--once")
    assert "done" in run("jobs")


def test_mcp_token_scopes_change_without_reissuing(env) -> None:
    from sqlalchemy import select

    from vibesynergy.store.schema import audit_log, mcp_pat

    run("init")
    created = run("mcp", "token", "create", "agent", "--scope", "read", "--scope", "sql")
    token = next(
        line.strip() for line in created.splitlines() if line.strip().startswith("vit_pat_")
    )
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(load_settings())
    try:
        with rt.engine.connect() as conn:
            before = conn.execute(select(mcp_pat).where(mcp_pat.c.name == "agent")).one()
        assert before.scopes == ["read", "sql"]
        assert before.token_hash
        listed = run("mcp", "token", "scopes", "agent", "--add", "plan.write", "--remove", "sql")
        assert listed.strip().split() == ["read", "plan.write"]
        assert token not in listed
        with rt.engine.connect() as conn:
            after = conn.execute(select(mcp_pat).where(mcp_pat.c.name == "agent")).one()
            audit = conn.execute(
                select(audit_log.c.detail).where(audit_log.c.action == "oauth.pat.scopes")
            ).scalar_one()
        assert after.token_hash == before.token_hash
        assert after.scopes == ["read", "plan.write"]
        assert audit["before"] == ["read", "sql"]
        assert audit["after"] == ["read", "plan.write"]
        assert token not in str(audit)
        unknown = runner.invoke(app, ["mcp", "token", "scopes", "agent", "--add", "nope"])
        assert unknown.exit_code == 1
        assert "unknown scope" in unknown.output
        empty = runner.invoke(
            app,
            ["mcp", "token", "scopes", "agent", "--remove", "read", "--remove", "plan.write"],
        )
        assert empty.exit_code == 1
        assert "at least one" in empty.output
        with rt.engine.connect() as conn:
            kept = conn.execute(select(mcp_pat.c.scopes, mcp_pat.c.token_hash)).one()
        assert kept.scopes == ["read", "plan.write"]
        assert kept.token_hash == before.token_hash
    finally:
        rt.close()


def test_mcp_client_scopes_update_live_tokens(env) -> None:
    from datetime import timedelta

    from sqlalchemy import select

    from vibesynergy.core.timeutil import utc_now
    from vibesynergy.store.schema import audit_log, oauth_client, oauth_grant

    run("init")
    from vibesynergy.core.config import load_settings
    from vibesynergy.runtime import Runtime

    rt = Runtime.open(load_settings())
    now = utc_now()
    old = ["read", "plan.propose"]
    try:
        with rt.engine.begin() as conn:
            conn.execute(
                oauth_client.insert().values(
                    client_id="app-1",
                    info={"client_id": "app-1", "client_name": "App", "scope": " ".join(old)},
                    created_at=now,
                    approved_at=now,
                )
            )
            for token_hash, kind, used, expires in (
                ("live-a", "access", None, now + timedelta(hours=1)),
                ("live-r", "refresh", None, now + timedelta(days=30)),
                ("used-r", "refresh", now, now + timedelta(days=30)),
                ("old-a", "access", None, now - timedelta(hours=1)),
            ):
                conn.execute(
                    oauth_grant.insert().values(
                        token_hash=token_hash,
                        kind=kind,
                        client_id="app-1",
                        data={"scopes": old},
                        expires_at=expires,
                        created_at=now,
                        used_at=used,
                    )
                )
        listed = run("mcp", "scopes", "app-1", "--add", "plan.write")
        assert listed.strip().split() == ["read", "plan.propose", "plan.write"]
        with rt.engine.connect() as conn:
            info = conn.execute(select(oauth_client.c.info)).scalar_one()
            grants = dict(conn.execute(select(oauth_grant.c.token_hash, oauth_grant.c.data)).all())
            audit = conn.execute(
                select(audit_log.c.detail).where(audit_log.c.action == "oauth.client.scopes")
            ).scalar_one()
        assert info["scope"] == "read plan.propose plan.write"
        assert grants["live-a"]["scopes"] == ["read", "plan.propose", "plan.write"]
        assert grants["live-r"]["scopes"] == ["read", "plan.propose", "plan.write"]
        assert grants["used-r"]["scopes"] == old
        assert grants["old-a"]["scopes"] == old
        assert audit["before"] == old
        assert audit["tokens"] == 2
        missing = runner.invoke(app, ["mcp", "scopes", "nope", "--add", "read"])
        assert missing.exit_code == 1
        assert "No approved app" in missing.output
    finally:
        rt.close()


def test_unknown_source(env) -> None:
    run("init")
    result = runner.invoke(app, ["sync", "nope"])
    assert result.exit_code == 1 and "unknown source" in result.output


def test_backfill_month_by_month(env) -> None:
    run("init")
    out = run("backfill", "demo", "--start", "2026-01-30", "--end", "2026-03-02")
    assert "2026-01-30..2026-01-31: 1 ok" in out and "2026-03-01..2026-03-02: 1 ok" in out


def test_import_files_command(env, tmp_path) -> None:
    from fitgen import activity_fit, utc

    run("init")
    (tmp_path / "ride.fit").write_bytes(activity_fit(utc(2015, 4, 1, 9, 0, 0), 5))
    out = run("import", "files", str(tmp_path / "ride.fit"))
    assert "files.fit" in out and "ok" in out
    assert "files.fit" in run("status")


def test_secret_link_cli_is_gone(env) -> None:
    run("init")
    assert runner.invoke(app, ["mcp", "--secret-url"]).exit_code == 2
    assert runner.invoke(app, ["mcp", "path", "list"]).exit_code == 2


def test_mcp_oauth_url_and_passphrase(env, monkeypatch) -> None:
    from tests_helpers import add_public_url

    add_public_url(env)
    run("init")
    out = run("mcp", "--url")
    assert "https://server.example.ts.net/vibesynergy/mcp" in out and "Coach apps" in out
    line = next(line for line in out.splitlines() if "/mcp" in line)
    assert line == "https://server.example.ts.net/vibesynergy/mcp"  # one line, copyable
    answers = iter(["short", "short"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    result = runner.invoke(app, ["mcp", "set-passphrase"])
    assert result.exit_code == 1 and "at least 12" in result.output
    answers = iter(["correct horse battery", "correct horse battery"])
    assert "Passphrase set" in run("mcp", "set-passphrase")
    assert "set-passphrase" not in run("mcp", "--url")


def test_mcp_token_create_duplicate_name_reports_clean_error(env) -> None:
    run("init")
    out = run("mcp", "token", "create", "ci-agent")
    token = next(line for line in out.splitlines() if line.startswith("vit_pat_"))
    assert "Coach apps" in out and token.startswith("vit_pat_")
    result = runner.invoke(app, ["mcp", "token", "create", "ci-agent"])
    assert result.exit_code == 1
    assert "name already used" in result.output
    assert "IntegrityError" not in result.output
    assert "token_hash" not in result.output


def test_init_with_email_makes_the_account_and_a_first_login_link(env) -> None:
    out = run("init", "--email", "Athlete@Example.com")
    assert "Account created for athlete@example.com" in out and "/login#link=" in out
    again = run("init", "--email", "other@example.com")
    assert "Account created" not in again  # one account per installation
    link = run("login-link")
    assert "Login link for athlete@example.com" in link and "/login#link=" in link
