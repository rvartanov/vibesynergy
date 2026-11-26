"""The portal behind the login: the gate, the login API, links, 2FA and sessions."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from vibesynergy.portal.app import create_portal_app
from vibesynergy.query import auth

pytestmark = pytest.mark.auth

EMAIL = "athlete@example.com"
GOOD = "lange-zin-met-fietsen-2027"
WRITE = {"x-vibesynergy": "portal"}


@pytest.fixture
def portal(runtime):
    return TestClient(create_portal_app(runtime), base_url="http://localhost")


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


def _token_from(body: str, key: str) -> str:
    return body.split(f"#{key}=", 1)[1].split()[0]


def test_everything_is_behind_the_login(portal):
    assert portal.get("/api/dashboard").status_code == 401
    page = portal.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login?next=/"
    assert portal.get("/login").status_code == 200
    assert portal.get("/auth/state").json()["logged_in"] is False


def test_first_login_by_link_then_choose_a_password(runtime, portal, mail):
    acc = auth.create_account(runtime.engine, EMAIL)
    token = auth.issue_token(runtime.engine, acc["id"], "setup", auth.SETUP_TTL)
    reply = portal.post("/auth/link/use", json={"token": token}, headers=WRITE)
    assert reply.status_code == 200 and reply.json() == {"next": "password"}
    assert "httponly" in reply.headers["set-cookie"].lower()
    assert portal.post("/auth/link/use", json={"token": token}, headers=WRITE).status_code == 401
    assert (
        portal.post("/auth/password", json={"password": "kort"}, headers=WRITE).status_code == 400
    )
    assert portal.post("/auth/password", json={"password": GOOD}, headers=WRITE).status_code == 200
    assert portal.get("/api/meta").status_code == 200  # logged in
    portal.post("/auth/logout", headers=WRITE)
    assert portal.get("/api/meta").status_code == 401


def test_password_login_and_the_same_answer_for_unknown_addresses(runtime, portal, mail):
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    bad = portal.post(
        "/auth/login", json={"email": EMAIL, "password": "wrong-wrong-wrong"}, headers=WRITE
    )
    unknown = portal.post(
        "/auth/login", json={"email": "x@example.com", "password": GOOD}, headers=WRITE
    )
    assert bad.status_code == unknown.status_code == 401 and bad.json() == unknown.json()
    ok = portal.post("/auth/login", json={"email": EMAIL.upper(), "password": GOOD}, headers=WRITE)
    assert ok.json() == {"next": "done"}
    assert any("Nieuwe login" in s or "New login" in s for _, s, _ in mail)
    assert portal.post("/auth/link", json={"email": "x@example.com"}, headers=WRITE).json() == {
        "sent": True
    }


def _age_attempts(runtime, seconds: float) -> None:
    """Move every recorded attempt this far into the past."""
    from datetime import timedelta

    from vibesynergy.store.schema import auth_attempt

    with runtime.engine.begin() as conn:
        for row in conn.execute(auth_attempt.select()).mappings().all():
            conn.execute(
                auth_attempt.update()
                .where(auth_attempt.c.id == row["id"])
                .values(at=row["at"] - timedelta(seconds=seconds))
            )


def test_wrong_passwords_for_the_email_slow_down_but_do_not_lock_out(runtime, portal):
    """Past the limit the email waits a little longer per failure; once that wait has
    passed, the right password works again (a stranger cannot keep the owner out)."""
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    for _ in range(auth.MAX_FAILURES_EMAIL):
        portal.post(
            "/auth/login", json={"email": EMAIL, "password": "wrong-wrong-wrong"}, headers=WRITE
        )
    waiting = portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE)
    assert waiting.status_code == 429 and "Wait a minute" in waiting.json()["error"]
    _age_attempts(runtime, auth.EMAIL_DELAY.total_seconds() + 1)
    ok = portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE)
    assert ok.status_code == 200, ok.text


def test_the_email_wait_grows_up_to_a_minute(runtime):
    from datetime import timedelta

    for _ in range(auth.MAX_FAILURES_EMAIL - 1):
        auth.note_attempt(runtime.engine, EMAIL, "198.51.100.1", False)
    assert auth.email_wait(runtime.engine, EMAIL) == timedelta(0)
    waits = []
    for n in range(12):
        auth.note_attempt(runtime.engine, EMAIL, f"198.51.100.{n + 2}", False)
        waits.append(auth.email_wait(runtime.engine, EMAIL))
    assert waits[0] <= auth.EMAIL_DELAY < waits[1] < waits[2] < waits[3]  # doubling
    assert all(w <= auth.EMAIL_DELAY_MAX for w in waits) and waits[-1] > timedelta(seconds=30)


def test_a_client_address_is_still_locked_hard(runtime):
    client = "198.51.100.7"
    for n in range(auth.MAX_FAILURES_CLIENT):
        auth.note_attempt(runtime.engine, f"other{n}@example.com", client, False)
    assert auth.lock_reason(runtime.engine, EMAIL, client) == "client"
    _age_attempts(runtime, 120)  # the email waits are long over; the lock is not
    assert auth.lock_reason(runtime.engine, EMAIL, client) == "client"
    assert auth.lock_reason(runtime.engine, EMAIL, "198.51.100.8") is None


def test_an_address_that_logged_in_before_is_not_slowed_by_strangers(runtime):
    home = "198.51.100.20"
    auth.note_attempt(runtime.engine, EMAIL, home, True)
    for n in range(auth.MAX_FAILURES_EMAIL + 6):
        auth.note_attempt(runtime.engine, EMAIL, f"203.0.113.{n}", False)
    assert auth.lock_reason(runtime.engine, EMAIL, "198.51.100.99") == "email"
    assert auth.lock_reason(runtime.engine, EMAIL, home) is None


def test_strangers_cannot_use_up_the_mail_quota_of_a_known_address(runtime):
    home = "198.51.100.30"
    auth.note_attempt(runtime.engine, EMAIL, home, True)
    for n in range(auth.MAX_MAILS_ALL):
        auth.mail_allowed(runtime.engine, "link", f"203.0.113.{n}")
    assert not auth.mail_allowed(runtime.engine, "link", "203.0.113.200")  # the total is used
    assert auth.mail_allowed(runtime.engine, "link", home)  # its own count still has room


def test_link_by_mail_and_password_reset(runtime, portal, mail):
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    portal.post("/auth/link", json={"email": EMAIL}, headers=WRITE)
    link = _token_from(mail[-1][2], "link")
    assert portal.post("/auth/link/use", json={"token": link}, headers=WRITE).json() == {
        "next": "done"
    }
    portal.post("/auth/logout", headers=WRITE)
    portal.post("/auth/reset", json={"email": EMAIL}, headers=WRITE)
    reset = _token_from(mail[-1][2], "reset")
    new = "een-heel-nieuw-wachtwoord-11"
    assert (
        portal.post(
            "/auth/reset/use", json={"token": reset, "password": "kort"}, headers=WRITE
        ).status_code
        == 400
    )
    assert portal.post(
        "/auth/reset/use", json={"token": reset, "password": new}, headers=WRITE
    ).json() == {"next": "done"}
    assert auth.verify_password(new, auth.account(runtime.engine)["password_hash"])


def test_second_factor_by_app_and_by_mail(runtime, portal, mail):
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE)
    # Changing how you log in asks for the password again.
    assert portal.post("/auth/totp/start", json={}, headers=WRITE).status_code == 400
    secret = portal.post("/auth/totp/start", json={"current": GOOD}, headers=WRITE).json()["secret"]
    code = auth._totp(secret, int(auth.time.time() // 30))
    codes = portal.post("/auth/totp/confirm", json={"code": code}, headers=WRITE).json()[
        "backup_codes"
    ]
    portal.post("/auth/logout", headers=WRITE)
    step = portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE).json()
    assert step == {"next": "mfa", "method": "totp"}
    assert portal.get("/api/meta").status_code == 401  # not in yet
    assert portal.post("/auth/mfa", json={"code": "000000x"}, headers=WRITE).status_code == 401
    assert portal.post("/auth/mfa", json={"code": codes[0]}, headers=WRITE).json() == {
        "next": "done"
    }
    assert portal.get("/api/meta").status_code == 200
    off = {"on": False, "current": "wrong-password-here"}
    assert portal.post("/auth/mfa/mail", json=off, headers=WRITE).status_code == 400
    assert auth.account(runtime.engine)["mfa"] == "totp"
    portal.post("/auth/mfa/mail", json={"on": True, "current": GOOD}, headers=WRITE)
    portal.post("/auth/logout", headers=WRITE)
    assert (
        portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE).json()[
            "method"
        ]
        == "mail"
    )
    mailed = mail[-1][2].split("code: ", 1)[1][:6]
    assert portal.post("/auth/mfa", json={"code": mailed}, headers=WRITE).json() == {"next": "done"}


def test_writes_still_need_the_portal_header(runtime, portal):
    assert portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}).status_code == 403


def test_a_login_link_still_comes_when_the_password_is_locked(runtime, portal, mail):
    # Someone guessing passwords must not close the one way back in.
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    for _ in range(auth.MAX_FAILURES_EMAIL):
        portal.post("/auth/login", json={"email": EMAIL, "password": "wrong-wrong"}, headers=WRITE)
    portal.post("/auth/link", json={"email": EMAIL}, headers=WRITE)
    assert len(mail) == 1
    link = _token_from(mail[0][2], "link")
    assert portal.post("/auth/link/use", json={"token": link}, headers=WRITE).status_code == 200


def test_link_and_reset_mails_are_limited(runtime, portal, mail):
    auth.create_account(runtime.engine, EMAIL)
    for _ in range(auth.MAX_MAILS_CLIENT + 3):
        reply = portal.post("/auth/link", json={"email": EMAIL}, headers=WRITE)
        assert reply.json() == {"sent": True}  # the same answer, sent or not
    assert len(mail) == auth.MAX_MAILS_CLIENT
    # Every link that was sent still works: a later request does not cancel an earlier one.
    first = _token_from(mail[0][2], "link")
    assert portal.post("/auth/link/use", json={"token": first}, headers=WRITE).status_code == 200
    for _ in range(auth.MAX_MAILS_CLIENT + 3):
        portal.post("/auth/reset", json={"email": EMAIL}, headers=WRITE)
    assert len(mail) == 2 * auth.MAX_MAILS_CLIENT + 1  # + the "new login" notice


def test_update_now_needs_only_the_login(runtime, portal, monkeypatch):
    """Logged in, Update now starts without a password; logged out, the gate refuses it."""
    from vibesynergy.query import about

    started = []
    monkeypatch.setattr(about, "start_update", lambda rt: started.append(1) or {"started": "u"})
    assert portal.post("/api/settings/update", json={}, headers=WRITE).status_code == 401
    assert not started
    acc = auth.create_account(runtime.engine, EMAIL)
    token = auth.issue_token(runtime.engine, acc["id"], "setup", auth.SETUP_TTL)
    assert portal.post("/auth/link/use", json={"token": token}, headers=WRITE).status_code == 200
    assert portal.post("/auth/password", json={"password": GOOD}, headers=WRITE).status_code == 200
    reply = portal.post("/api/settings/update", json={}, headers=WRITE)
    assert reply.status_code == 200 and started == [1]
    assert about.about(runtime)["confirm"] == "none"
