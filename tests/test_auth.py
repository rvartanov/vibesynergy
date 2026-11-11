"""Portal login core (query/auth.py): passwords, one-time links, 2FA, sessions, the brake."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from vibesynergy.query import auth
from vibesynergy.store.schema import auth_session, auth_token

EMAIL = "athlete@example.com"
GOOD = "lange-zin-met-fietsen-2027"


@pytest.fixture
def acc(runtime):
    return auth.create_account(runtime.engine, EMAIL)


def test_one_account_by_email_and_no_password_yet(runtime, acc):
    assert acc["email"] == EMAIL and acc["password_hash"] is None and acc["mfa"] == "none"
    with pytest.raises(auth.AuthError):
        auth.create_account(runtime.engine, "other@example.com")
    with pytest.raises(auth.AuthError):
        auth.set_email(runtime.engine, acc["id"], "nope")


def test_passwords_are_hashed_and_checked(runtime, acc):
    stored = auth.hash_password(GOOD)
    assert stored.startswith("scrypt$") and GOOD not in stored
    assert auth.verify_password(GOOD, stored) and not auth.verify_password("wrong", stored)
    assert not auth.verify_password(GOOD, None)
    for weak in ("kort", "password1234", "aaaaaaaaaaaaaaaa", EMAIL, "athlete"):
        with pytest.raises(auth.AuthError):
            auth.check_password(weak, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    assert auth.verify_password(GOOD, auth.account(runtime.engine)["password_hash"])


def test_a_link_works_once_and_only_while_fresh(runtime, acc):
    token = auth.issue_token(runtime.engine, acc["id"], "login", auth.LINK_TTL)
    with runtime.engine.connect() as conn:
        stored = conn.execute(select(auth_token.c.hash)).scalar_one()
    assert token not in stored  # only the hash is kept
    assert auth.peek_token(runtime.engine, token, "login")
    assert auth.use_token(runtime.engine, token, "reset") is None  # wrong purpose
    assert auth.use_token(runtime.engine, token, "login") == acc["id"]
    assert auth.use_token(runtime.engine, token, "login") is None  # spent
    old = auth.issue_token(runtime.engine, acc["id"], "login", auth.LINK_TTL)
    new = auth.issue_token(runtime.engine, acc["id"], "login", auth.LINK_TTL)
    assert auth.use_token(runtime.engine, old, "login") is None  # a newer link replaces it
    with runtime.engine.begin() as conn:
        conn.execute(update(auth_token).values(expires_at=auth.utc_now() - timedelta(seconds=1)))
    assert auth.use_token(runtime.engine, new, "login") is None  # expired


def test_totp_codes_and_no_replay():
    secret = auth.new_totp_secret()
    now = 1_800_000_000.0
    code = auth._totp(secret, int(now // 30))
    counter = auth.totp_check(secret, code, now=now)
    assert counter == int(now // 30)
    assert auth.totp_check(secret, code, last=counter, now=now) is None  # used once
    assert auth.totp_check(secret, "000000", now=now) in (None, counter)  # almost never valid
    assert auth.totp_check(secret, "12345", now=now) is None
    # RFC 6238 test vector (SHA-1, secret "12345678901234567890", T=59 -> 94287082, 8 digits).
    rfc = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth._totp(rfc, 1) == "287082"


def test_totp_setup_backup_codes_and_second_factor(runtime, acc):
    settings = runtime.settings
    secret = auth.start_totp(settings)
    with pytest.raises(auth.AuthError):
        auth.confirm_totp(runtime.engine, settings, acc["id"], "000001")
    code = auth._totp(secret, int(auth.time.time() // 30))
    codes = auth.confirm_totp(runtime.engine, settings, acc["id"], code)
    assert len(codes) == 10 and auth.account(runtime.engine)["mfa"] == "totp"
    assert not auth.check_second_factor(runtime.engine, settings, acc["id"], code)  # replay
    assert auth.check_second_factor(runtime.engine, settings, acc["id"], codes[0])
    assert not auth.check_second_factor(runtime.engine, settings, acc["id"], codes[0])  # spent
    assert auth.backup_codes_left(settings) == 9


def test_mailed_code_as_second_factor(runtime, acc, monkeypatch):
    from vibesynergy import mailer

    monkeypatch.setattr(mailer, "configured", lambda settings: True)
    auth.set_mail_mfa(runtime.engine, runtime.settings, acc["id"], True)
    code = auth.issue_token(runtime.engine, acc["id"], "mfa_mail", auth.CODE_TTL)
    assert len(code) == 6 and code.isdigit()
    assert not auth.check_second_factor(runtime.engine, runtime.settings, acc["id"], "999999x")
    assert auth.check_second_factor(runtime.engine, runtime.settings, acc["id"], code)
    assert not auth.check_second_factor(runtime.engine, runtime.settings, acc["id"], code)


def test_sessions_hashed_idle_and_log_out_everywhere(runtime, acc):
    token = auth.new_session(runtime.engine, acc["id"], mfa_ok=True, client="1.2.3.4")
    with runtime.engine.connect() as conn:
        assert conn.execute(select(auth_session.c.id)).scalar_one() != token
    assert auth.session(runtime.engine, token).mfa_ok
    other = auth.new_session(runtime.engine, acc["id"], mfa_ok=True)
    assert len(auth.sessions(runtime.engine, acc["id"], token)) == 2
    assert auth.end_all_sessions(runtime.engine, acc["id"], keep=token) == 1
    assert auth.session(runtime.engine, other) is None and auth.session(runtime.engine, token)
    with runtime.engine.begin() as conn:
        conn.execute(update(auth_session).values(last_seen=auth.utc_now() - timedelta(days=15)))
    assert auth.session(runtime.engine, token) is None  # idle too long
    auth.set_password(runtime.engine, acc["id"], GOOD)  # a new password ends sessions


def test_waiting_for_the_second_factor_is_not_logged_in(runtime, acc):
    pending = auth.new_session(runtime.engine, acc["id"], mfa_ok=False)
    assert auth.session(runtime.engine, pending).mfa_ok is False
    full = auth.complete_mfa(runtime.engine, pending)
    assert auth.session(runtime.engine, pending) is None  # replaced, not upgraded in place
    assert auth.session(runtime.engine, full).mfa_ok


def test_the_brake_after_repeated_failures(runtime, acc):
    for _ in range(auth.MAX_FAILURES_EMAIL):
        assert not auth.locked(runtime.engine, EMAIL, "1.1.1.1")
        auth.note_attempt(runtime.engine, EMAIL, "1.1.1.1", ok=False)
    assert auth.locked(runtime.engine, EMAIL, "9.9.9.9")  # the address is locked
    assert auth.locked(runtime.engine, EMAIL.upper(), "9.9.9.9")
    assert not auth.locked(runtime.engine, "else@example.com", "9.9.9.9")
