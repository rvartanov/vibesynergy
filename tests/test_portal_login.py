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


def test_too_many_wrong_passwords_lock_the_login(runtime, portal):
    acc = auth.create_account(runtime.engine, EMAIL)
    auth.set_password(runtime.engine, acc["id"], GOOD)
    for _ in range(auth.MAX_FAILURES_EMAIL):
        portal.post(
            "/auth/login", json={"email": EMAIL, "password": "wrong-wrong-wrong"}, headers=WRITE
        )
    locked = portal.post("/auth/login", json={"email": EMAIL, "password": GOOD}, headers=WRITE)
    assert locked.status_code == 429


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
    secret = portal.post("/auth/totp/start", headers=WRITE).json()["secret"]
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
    portal.post("/auth/mfa/mail", json={"on": True}, headers=WRITE)
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
