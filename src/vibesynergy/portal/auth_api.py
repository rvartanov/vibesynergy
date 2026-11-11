"""Login API for the portal (query/auth.py). JSON in and out; the page is ``/login``.

Links in mail carry their token after ``#`` (``/login#link=…``, ``/login#reset=…``): the
token never reaches a server log, and a mail scanner that opens links cannot spend it. The
page posts it here. Answers to "send me a link" are the same whether the address is known
or not.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from vibesynergy.portal.auth_gate import COOKIE, auth_required, cookie_value
from vibesynergy.query import auth


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(_Body):
    email: str
    password: str


class Email(_Body):
    email: str


class Code(_Body):
    code: str


class Token(_Body):
    token: str


class Reset(_Body):
    token: str
    password: str


class Password(_Body):
    password: str
    current: str | None = None


class MailOn(_Body):
    on: bool


class SessionId(_Body):
    id: str


def _client(request: Request) -> str:
    # Behind the local reverse proxy the real address is in X-Forwarded-For.
    peer = request.client.host if request.client else ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and peer in ("127.0.0.1", "::1"):
        return forwarded.split(",")[0].strip()[:64]
    return peer[:64]


def _secure(request: Request) -> bool:
    host = (request.url.hostname or "").lower()
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto == "https" or host not in ("localhost", "127.0.0.1", "::1")


def _with_session(request: Request, body: dict[str, Any], token: str) -> JSONResponse:
    response = JSONResponse(body)
    response.set_cookie(
        COOKIE,
        token,
        max_age=int(auth.SESSION_MAX.total_seconds()),
        httponly=True,
        secure=_secure(request),
        samesite="lax",
        path="/",
    )
    return response


def _cleared(body: dict[str, Any]) -> JSONResponse:
    response = JSONResponse(body)
    response.delete_cookie(COOKIE, path="/")
    return response


def _base_url(rt, request: Request) -> str:
    """Where links in mail point: the address this request came in on (a known host)."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def _mail(rt, to: str, subject: str, body: str) -> bool:
    from vibesynergy import mailer

    if not mailer.configured(rt.settings):
        return False
    try:
        mailer.send(rt.settings, to, subject, body)
        return True
    except mailer.MailError:
        return False


def _lang(request: Request) -> str:
    from vibesynergy.i18n import request_language

    return "nl" if request_language(request) == "nl" else "en"


TEXT = {
    "link": {
        "nl": (
            "Inloggen bij VibeSynergy",
            "Log in met deze link (15 minuten geldig, één keer te gebruiken):\n\n{url}\n\n"
            "Heb je dit niet aangevraagd? Dan kun je deze mail negeren.",
        ),
        "en": (
            "Log in to VibeSynergy",
            "Log in with this link (valid for 15 minutes, once):\n\n{url}\n\n"
            "Didn't ask for this? Then you can ignore this mail.",
        ),
    },
    "reset": {
        "nl": (
            "Nieuw wachtwoord voor VibeSynergy",
            "Kies een nieuw wachtwoord met deze link (15 minuten geldig):\n\n{url}\n\n"
            "Heb je dit niet aangevraagd? Dan blijft je wachtwoord gewoon zoals het is.",
        ),
        "en": (
            "New password for VibeSynergy",
            "Choose a new password with this link (valid for 15 minutes):\n\n{url}\n\n"
            "Didn't ask for this? Then your password stays as it is.",
        ),
    },
    "code": {
        "nl": (
            "Je inlogcode voor VibeSynergy",
            "Je code: {code}\n\nDeze code is 10 minuten geldig.",
        ),
        "en": (
            "Your VibeSynergy login code",
            "Your code: {code}\n\nThis code is valid for 10 minutes.",
        ),
    },
    "new_login": {
        "nl": (
            "Nieuwe login bij VibeSynergy",
            "Er is net ingelogd bij VibeSynergy vanaf {client}.\n\nWas jij dit niet? Kies dan "
            "een nieuw wachtwoord en log overal uit (Instellingen → Account).",
        ),
        "en": (
            "New login to VibeSynergy",
            "Someone just logged in to VibeSynergy from {client}.\n\nWasn't this you? Choose a "
            "new password and log out everywhere (Settings → Account).",
        ),
    },
}


def _send(rt, request: Request, kind: str, to: str, **values: str) -> bool:
    subject, body = TEXT[kind][_lang(request)]
    return _mail(rt, to, subject, body.format(**values))


def build_auth_router(rt) -> APIRouter:
    router = APIRouter()
    engine = rt.engine

    def audit(action: str, request: Request, outcome: str = "ok") -> None:
        auth.audit(engine, action, _client(request), outcome)

    def finish(
        request: Request, account_id: int, *, pending_token: str | None = None
    ) -> JSONResponse:
        """A password, link or code passed: a full session, or a wait for the second factor."""
        acc = auth.account(engine)
        assert acc is not None
        client, agent = _client(request), request.headers.get("user-agent", "")
        if pending_token is None and acc["mfa"] != "none":
            token = auth.new_session(engine, account_id, mfa_ok=False, client=client, agent=agent)
            if acc["mfa"] == "mail":
                code = auth.issue_token(engine, account_id, "mfa_mail", auth.CODE_TTL)
                _send(rt, request, "code", acc["email"], code=code)
            return _with_session(request, {"next": "mfa", "method": acc["mfa"]}, token)
        if pending_token is not None:
            token = auth.complete_mfa(engine, pending_token, client=client, agent=agent)
        else:
            token = auth.new_session(engine, account_id, mfa_ok=True, client=client, agent=agent)
        audit("auth.login", request)
        _send(rt, request, "new_login", acc["email"], client=client or "?")
        nxt = "password" if not acc["password_hash"] else "done"
        return _with_session(request, {"next": nxt}, token)

    @router.get("/auth/state")
    def state(request: Request) -> dict[str, Any]:
        acc = auth.account(engine)
        current = auth.session(engine, cookie_value(request.scope), touch=False)
        return {
            "required": auth_required(rt.settings),
            "account": acc is not None,
            "logged_in": bool(current and current.mfa_ok),
            "mfa_pending": bool(current and not current.mfa_ok),
            "method": acc["mfa"] if acc and current else None,
            "password_set": bool(acc and acc["password_hash"]) if current else None,
        }

    @router.post("/auth/login")
    def login(body: Login, request: Request) -> JSONResponse:
        client = _client(request)
        if auth.locked(engine, body.email, client):
            audit("auth.login", request, "locked")
            return JSONResponse({"error": "Too many attempts. Try again in 15 minutes."}, 429)
        acc = auth.account(engine)
        ok = bool(
            acc
            and acc["email"] == body.email.strip().lower()
            and auth.verify_password(body.password, acc["password_hash"])
        )
        if not acc or acc["email"] != body.email.strip().lower():
            auth.verify_password(body.password, None)  # same time as a real check
        auth.note_attempt(engine, body.email, client, ok)
        if not ok:
            audit("auth.login", request, "refused")
            return JSONResponse({"error": "That email address or password is not right."}, 401)
        assert acc is not None
        return finish(request, acc["id"])

    @router.post("/auth/mfa")
    def mfa(body: Code, request: Request) -> JSONResponse:
        token = cookie_value(request.scope)
        current = auth.session(engine, token, touch=False)
        acc = auth.account(engine)
        if current is None or current.mfa_ok or acc is None:
            return JSONResponse({"error": "The login has expired. Start again."}, 401)
        client = _client(request)
        if auth.locked(engine, acc["email"], client):
            return JSONResponse({"error": "Too many attempts. Try again in 15 minutes."}, 429)
        ok = auth.check_second_factor(engine, rt.settings, acc["id"], body.code)
        auth.note_attempt(engine, acc["email"], client, ok)
        if not ok:
            audit("auth.mfa", request, "refused")
            return JSONResponse({"error": "That code is not right."}, 401)
        return finish(request, acc["id"], pending_token=token)

    @router.post("/auth/link")
    def send_link(body: Email, request: Request) -> dict[str, Any]:
        acc = auth.account(engine)
        client = _client(request)
        if (
            acc
            and acc["email"] == body.email.strip().lower()
            and not auth.locked(engine, body.email, client)
        ):
            token = auth.issue_token(engine, acc["id"], "login", auth.LINK_TTL)
            _send(
                rt,
                request,
                "link",
                acc["email"],
                url=f"{_base_url(rt, request)}/login#link={token}",
            )
            audit("auth.link.sent", request)
        auth.note_attempt(engine, body.email, client, True)
        return {"sent": True}  # the same answer for any address

    @router.post("/auth/link/use")
    def use_link(body: Token, request: Request) -> JSONResponse:
        account_id = auth.use_token(engine, body.token, "login") or auth.use_token(
            engine, body.token, "setup"
        )
        if account_id is None:
            audit("auth.link.used", request, "refused")
            return JSONResponse(
                {"error": "This link has expired or was used. Ask for a new one."}, 401
            )
        return finish(request, account_id)

    @router.post("/auth/reset")
    def send_reset(body: Email, request: Request) -> dict[str, Any]:
        acc = auth.account(engine)
        if (
            acc
            and acc["email"] == body.email.strip().lower()
            and not auth.locked(engine, body.email, _client(request))
        ):
            token = auth.issue_token(engine, acc["id"], "reset", auth.LINK_TTL)
            _send(
                rt,
                request,
                "reset",
                acc["email"],
                url=f"{_base_url(rt, request)}/login#reset={token}",
            )
            audit("auth.reset.sent", request)
        return {"sent": True}

    @router.post("/auth/reset/use")
    def use_reset(body: Reset, request: Request) -> JSONResponse:
        acc = auth.account(engine)
        if acc is None:
            return JSONResponse(
                {"error": "This link has expired or was used. Ask for a new one."}, 401
            )
        auth.check_password(body.password, acc["email"])  # before spending the link
        account_id = auth.use_token(engine, body.token, "reset")
        if account_id is None:
            return JSONResponse(
                {"error": "This link has expired or was used. Ask for a new one."}, 401
            )
        auth.set_password(engine, account_id, body.password)  # ends every session
        audit("auth.reset.used", request)
        return finish(request, account_id)

    # --- logged in (the gate lets /auth through, so each route checks the session) ---------

    def me(request: Request) -> tuple[str, auth.Session]:
        token = cookie_value(request.scope)
        current = auth.session(engine, token, touch=False)
        if current is None or not current.mfa_ok or token is None:
            raise HTTPException(status_code=401, detail="Log in first.")
        return token, current

    @router.post("/auth/password")
    def change_password(body: Password, request: Request) -> dict[str, Any]:
        _, current = me(request)
        acc = auth.account(engine)
        assert acc is not None
        if acc["password_hash"] and not auth.verify_password(
            body.current or "", acc["password_hash"]
        ):
            raise auth.AuthError("Your current password is not right.")
        auth.set_password(engine, current.account_id, body.password, keep=current.id)
        audit("auth.password", request)
        return {"ok": True}

    @router.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        auth.end_session(engine, cookie_value(request.scope))
        return _cleared({"ok": True})

    @router.post("/auth/logout-all")
    def logout_all(request: Request) -> JSONResponse:
        _, current = me(request)
        auth.end_all_sessions(engine, current.account_id)
        audit("auth.logout_all", request)
        return _cleared({"ok": True})

    @router.get("/auth/account")
    def account_page(request: Request) -> dict[str, Any]:
        token, current = me(request)
        acc = auth.account(engine)
        assert acc is not None
        from vibesynergy import mailer

        return {
            "email": acc["email"],
            "mfa": acc["mfa"],
            "password_set": bool(acc["password_hash"]),
            "backup_codes_left": auth.backup_codes_left(rt.settings)
            if acc["mfa"] == "totp"
            else None,
            "mail_configured": mailer.configured(rt.settings),
            "sessions": auth.sessions(engine, current.account_id, token),
        }

    @router.post("/auth/totp/start")
    def totp_start(request: Request) -> dict[str, Any]:
        me(request)
        acc = auth.account(engine)
        assert acc is not None
        secret = auth.start_totp(rt.settings)
        return {"secret": secret, "uri": auth.otpauth_uri(secret, acc["email"])}

    @router.post("/auth/totp/confirm")
    def totp_confirm(body: Code, request: Request) -> dict[str, Any]:
        _, current = me(request)
        codes = auth.confirm_totp(engine, rt.settings, current.account_id, body.code)
        audit("auth.mfa.totp", request)
        return {"backup_codes": codes}

    @router.post("/auth/mfa/mail")
    def mfa_mail(body: MailOn, request: Request) -> dict[str, Any]:
        _, current = me(request)
        auth.set_mail_mfa(engine, rt.settings, current.account_id, body.on)
        audit("auth.mfa.mail" if body.on else "auth.mfa.off", request)
        return {"ok": True}

    @router.post("/auth/sessions/end")
    def end_one(body: SessionId, request: Request) -> dict[str, Any]:
        _, current = me(request)
        return {"ended": auth.end_session_by_prefix(engine, current.account_id, body.id)}

    return router
