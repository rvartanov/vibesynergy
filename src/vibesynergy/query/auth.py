"""Portal login: one account per installation, safe to put on the internet.

- The account is made at install time with an email address only (``vibesynergy init
  --email``). Its first login is a link; on that first visit the athlete sets a password.
- Passwords are hashed with scrypt (``hashlib.scrypt``, random salt, constant-time compare).
- Login links, password-reset links and mailed 2FA codes are one-time tokens: random,
  stored only as a SHA-256 hash, short-lived, and spent on first use.
- Second factor, optional: a code by mail, or an authenticator app (TOTP, RFC 6238) with
  one-time backup codes. The TOTP secret is kept in the encrypted secret store.
- Sessions: a random cookie value, stored only hashed, with an idle and an absolute
  lifetime. A new session at every login; "log out everywhere" ends them all.
- Failed attempts are counted per email and per client address; past the limit, logins are
  refused for a while. Answers never say whether an address exists.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine

from vibesynergy.core.timeutil import utc_now
from vibesynergy.store.schema import auth_account, auth_attempt, auth_session, auth_token

# Tokens and their lifetimes.
LINK_TTL = timedelta(minutes=15)  # login link and password-reset link
CODE_TTL = timedelta(minutes=10)  # 2FA code by mail
SETUP_TTL = timedelta(hours=24)  # the first login link from the installer
SESSION_IDLE = timedelta(days=14)
SESSION_MAX = timedelta(days=60)
PENDING_TTL = timedelta(minutes=10)  # a session waiting for its second factor

# Brute-force brake: failures in WINDOW before LOCK applies.
WINDOW = timedelta(minutes=15)
MAX_FAILURES_EMAIL = 5
MAX_FAILURES_CLIENT = 20
LOCK = timedelta(minutes=15)

MIN_PASSWORD = 12
SCRYPT = {"n": 2**15, "r": 8, "p": 1, "dklen": 32}
BACKUP_CODES = 10
SECRET_DOC = "auth_totp"  # in the encrypted secret store
TOTP_STEP = 30
TOTP_DIGITS = 6

# The most common passwords and keyboard runs; a long, unusual one is what counts.
COMMON = frozenset(
    {
        "123456789012",
        "password1234",
        "passwordpassword",
        "qwertyuiopas",
        "wachtwoord12",
        "welkom123456",
        "iloveyou1234",
        "111111111111",
        "abcdefghijkl",
        "letmein12345",
        "administrator",
        "000000000000",
    }
)


class AuthError(ValueError):
    """Refused; the message is plain English for the page to translate."""


@dataclass(frozen=True)
class Session:
    id: str  # the stored hash
    account_id: int
    mfa_ok: bool


# --- helpers ---------------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def _token() -> str:
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, **SCRYPT)
    params = f"{SCRYPT['n']}:{SCRYPT['r']}:{SCRYPT['p']}"
    return f"scrypt${params}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        # Burn the same time as a real check, so timing says nothing.
        hashlib.scrypt(b"x", salt=b"0" * 16, maxmem=64 * 1024 * 1024, **SCRYPT)
        return False
    _, params, salt, digest = stored.split("$")
    n, r, p = (int(x) for x in params.split(":"))
    want = base64.b64decode(digest)
    got = hashlib.scrypt(
        password.encode(),
        salt=base64.b64decode(salt),
        n=n,
        r=r,
        p=p,
        dklen=len(want),
        maxmem=64 * 1024 * 1024,
    )
    return hmac.compare_digest(got, want)


def check_password(password: str, email: str) -> None:
    """A new password: long enough, not a common one, not the email address."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError("Use at least 12 characters.")
    if len(password) > 256:
        raise AuthError("Use at most 256 characters.")
    low = password.lower()
    if (
        low in COMMON
        or len(set(low)) < 4
        or low == _norm(email)
        or _norm(email).split("@")[0] == low
    ):
        raise AuthError("This password is too easy to guess. Choose a longer, less common one.")


# --- TOTP (RFC 6238) ------------------------------------------------------------------------


def _totp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_check(
    secret: str, code: str, *, last: int | None = None, now: float | None = None
) -> int | None:
    """The counter the code belongs to (now ±1 step), or None. A code is never used twice."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != TOTP_DIGITS:
        return None
    current = int((now if now is not None else time.time()) // TOTP_STEP)
    for counter in (current - 1, current, current + 1):
        if last is not None and counter <= last:
            continue
        if hmac.compare_digest(_totp(secret, counter), code):
            return counter
    return None


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def otpauth_uri(secret: str, email: str) -> str:
    from urllib.parse import quote

    label = quote(f"VibeSynergy:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer=VibeSynergy&digits=6&period=30"


# --- the account -----------------------------------------------------------------------------


def account(engine: Engine) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(select(auth_account).limit(1)).mappings().first()
    return dict(row) if row else None


def create_account(engine: Engine, email: str) -> dict[str, Any]:
    """The installation's one account, by email only; the password follows at first login."""
    email = _norm(email)
    if "@" not in email or "." not in email.split("@")[-1] or len(email) > 254:
        raise AuthError("Give a valid email address.")
    if account(engine) is not None:
        raise AuthError("This installation already has an account.")
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            insert(auth_account).values(
                email=email, password_hash=None, mfa="none", created_at=now, updated_at=now
            )
        )
    found = account(engine)
    assert found is not None
    return found


def set_email(engine: Engine, account_id: int, email: str) -> None:
    email = _norm(email)
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("Give a valid email address.")
    with engine.begin() as conn:
        conn.execute(
            update(auth_account)
            .where(auth_account.c.id == account_id)
            .values(email=email, updated_at=utc_now())
        )


def set_password(
    engine: Engine, account_id: int, password: str, *, keep: str | None = None
) -> None:
    """A new password ends every other session (``keep`` is the session that set it)."""
    acc = account(engine)
    if acc is None or acc["id"] != account_id:
        raise AuthError("No account.")
    check_password(password, acc["email"])
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            update(auth_account)
            .where(auth_account.c.id == account_id)
            .values(password_hash=hash_password(password), password_set_at=now, updated_at=now)
        )
        stmt = delete(auth_session).where(auth_session.c.account_id == account_id)
        if keep:
            stmt = stmt.where(auth_session.c.id != keep)
        conn.execute(stmt)


# --- one-time tokens -------------------------------------------------------------------------


def issue_token(engine: Engine, account_id: int, purpose: str, ttl: timedelta) -> str:
    """A new link or code. An earlier unused code or setup link stops working; earlier login
    and reset links stay good until they expire, so a stranger asking for a link cannot
    cancel the one the owner is about to click (how many are sent is bounded, see
    ``mail_allowed``)."""
    token = (
        "".join(secrets.choice("0123456789") for _ in range(6))
        if purpose == "mfa_mail"
        else _token()
    )
    now = utc_now()
    with engine.begin() as conn:
        stale = [auth_token.c.account_id == account_id, auth_token.c.purpose == purpose]
        if purpose in ("login", "reset"):
            stale.append(auth_token.c.expires_at <= now)
        else:
            stale.append(auth_token.c.used_at.is_(None))
        conn.execute(delete(auth_token).where(*stale))
        conn.execute(
            insert(auth_token).values(
                hash=_hash(token),
                account_id=account_id,
                purpose=purpose,
                created_at=now,
                expires_at=now + ttl,
            )
        )
    return token


def use_token(engine: Engine, token: str, purpose: str) -> int | None:
    """The account the token belongs to, spending it; None when unknown, used or expired."""
    now = utc_now()
    with engine.begin() as conn:
        row = (
            conn.execute(
                select(auth_token).where(
                    auth_token.c.hash == _hash(token or ""), auth_token.c.purpose == purpose
                )
            )
            .mappings()
            .first()
        )
        if row is None or row["used_at"] is not None or row["expires_at"] <= now:
            return None
        spent = conn.execute(
            update(auth_token)
            .where(auth_token.c.hash == row["hash"], auth_token.c.used_at.is_(None))
            .values(used_at=now)
        ).rowcount
        return row["account_id"] if spent else None


def peek_token(engine: Engine, token: str, purpose: str) -> bool:
    """Whether a link is still good, without spending it (to show the set-password form)."""
    with engine.connect() as conn:
        row = (
            conn.execute(
                select(auth_token.c.expires_at, auth_token.c.used_at).where(
                    auth_token.c.hash == _hash(token or ""), auth_token.c.purpose == purpose
                )
            )
            .mappings()
            .first()
        )
    return bool(row and row["used_at"] is None and row["expires_at"] > utc_now())


# --- brute-force brake ----------------------------------------------------------------------


def _record(engine: Engine, key: str, ok: bool) -> None:
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(insert(auth_attempt).values(key=key, at=now, ok=ok))
        conn.execute(delete(auth_attempt).where(auth_attempt.c.at < now - timedelta(days=2)))


def locked(engine: Engine, email: str, client: str) -> bool:
    """Too many recent failures for this address or this client."""
    since = utc_now() - max(WINDOW, LOCK)
    with engine.connect() as conn:

        def failures(key: str) -> int:
            return conn.execute(
                select(func.count())
                .select_from(auth_attempt)
                .where(
                    auth_attempt.c.key == key,
                    auth_attempt.c.ok.is_(False),
                    auth_attempt.c.at >= since,
                )
            ).scalar_one()

        return (
            failures(f"email:{_norm(email)}") >= MAX_FAILURES_EMAIL
            or failures(f"client:{client}") >= MAX_FAILURES_CLIENT
        )


MAX_MAILS_CLIENT = 3
MAX_MAILS_ALL = 6


def mail_allowed(engine: Engine, purpose: str, client: str) -> bool:
    """May a login or reset link be mailed now? Counts the mail when it may.

    A few per quarter of an hour, per visitor and in total, so nobody can fill the mailbox.
    Deliberately apart from the wrong-password brake: someone guessing passwords must not
    be able to close the one way back in.
    """
    since = utc_now() - WINDOW
    keys = {f"mail:{purpose}:{client}": MAX_MAILS_CLIENT, f"mail:{purpose}": MAX_MAILS_ALL}
    with engine.connect() as conn:
        for key, most in keys.items():
            sent = conn.execute(
                select(func.count())
                .select_from(auth_attempt)
                .where(auth_attempt.c.key == key, auth_attempt.c.at >= since)
            ).scalar_one()
            if sent >= most:
                return False
    for key in keys:
        _record(engine, key, True)
    return True


def note_attempt(engine: Engine, email: str, client: str, ok: bool) -> None:
    _record(engine, f"email:{_norm(email)}", ok)
    _record(engine, f"client:{client}", ok)


# --- sessions -------------------------------------------------------------------------------


def new_session(
    engine: Engine, account_id: int, *, mfa_ok: bool, client: str = "", agent: str = ""
) -> str:
    """A fresh session; the cookie gets the returned value, the database only its hash."""
    token = _token()
    now = utc_now()
    with engine.begin() as conn:
        conn.execute(
            insert(auth_session).values(
                id=_hash(token),
                account_id=account_id,
                created_at=now,
                last_seen=now,
                expires_at=now + (SESSION_MAX if mfa_ok else PENDING_TTL),
                mfa_ok=mfa_ok,
                client=client[:64],
                agent=agent[:200],
            )
        )
    return token


def session(engine: Engine, token: str | None, *, touch: bool = True) -> Session | None:
    """The live session for a cookie value, or None. Each use moves the idle limit."""
    if not token:
        return None
    now = utc_now()
    with engine.connect() as conn:
        row = (
            conn.execute(select(auth_session).where(auth_session.c.id == _hash(token)))
            .mappings()
            .first()
        )
    if row is None or row["expires_at"] <= now or row["last_seen"] + SESSION_IDLE <= now:
        return None
    if touch and now - row["last_seen"] > timedelta(minutes=5):
        with engine.begin() as conn:
            conn.execute(
                update(auth_session).where(auth_session.c.id == row["id"]).values(last_seen=now)
            )
    return Session(row["id"], row["account_id"], bool(row["mfa_ok"]))


def complete_mfa(engine: Engine, token: str, *, client: str = "", agent: str = "") -> str:
    """The second factor passed: the waiting session is replaced by a full one."""
    current = session(engine, token, touch=False)
    if current is None:
        raise AuthError("The login has expired. Start again.")
    end_session(engine, token)
    return new_session(engine, current.account_id, mfa_ok=True, client=client, agent=agent)


def end_session(engine: Engine, token: str | None) -> None:
    if token:
        with engine.begin() as conn:
            conn.execute(delete(auth_session).where(auth_session.c.id == _hash(token)))


def end_all_sessions(engine: Engine, account_id: int, *, keep: str | None = None) -> int:
    with engine.begin() as conn:
        stmt = delete(auth_session).where(auth_session.c.account_id == account_id)
        if keep:
            stmt = stmt.where(auth_session.c.id != _hash(keep))
        return conn.execute(stmt).rowcount


def sessions(engine: Engine, account_id: int, current: str | None = None) -> list[dict[str, Any]]:
    now = utc_now()
    with engine.connect() as conn:
        rows = conn.execute(
            select(auth_session)
            .where(auth_session.c.account_id == account_id, auth_session.c.mfa_ok.is_(True))
            .order_by(auth_session.c.last_seen.desc())
        ).mappings()
        return [
            {
                "id": r["id"][:12],
                "created_at": r["created_at"].isoformat(),
                "last_seen": r["last_seen"].isoformat(),
                "client": r["client"],
                "agent": r["agent"],
                "current": bool(current) and r["id"] == _hash(current or ""),
            }
            for r in rows
            if r["expires_at"] > now
        ]


def end_session_by_prefix(engine: Engine, account_id: int, prefix: str) -> bool:
    if len(prefix) < 12:
        return False
    with engine.begin() as conn:
        return bool(
            conn.execute(
                delete(auth_session).where(
                    auth_session.c.account_id == account_id, auth_session.c.id.like(f"{prefix}%")
                )
            ).rowcount
        )


# --- second factor --------------------------------------------------------------------------


def _store(settings: Any):
    from vibesynergy.core.secrets import SecretStore

    return SecretStore(settings.secrets_dir, "portal")


def totp_secret(settings: Any) -> dict[str, Any]:
    return dict(_store(settings).get(SECRET_DOC) or {})


def start_totp(settings: Any) -> str:
    """A new secret, kept as pending until the first code from the app confirms it."""
    doc = totp_secret(settings)
    doc["pending"] = new_totp_secret()
    _store(settings).set(SECRET_DOC, doc)
    return doc["pending"]


def confirm_totp(engine: Engine, settings: Any, account_id: int, code: str) -> list[str]:
    """The app works: TOTP becomes the second factor. Returns fresh backup codes (shown once)."""
    doc = totp_secret(settings)
    pending = doc.get("pending")
    if not pending:
        raise AuthError("Start setting up the app first.")
    counter = totp_check(pending, code)
    if counter is None:
        raise AuthError("That code is not right. Check the time on your phone and try again.")
    codes = [secrets.token_hex(4) + "-" + secrets.token_hex(4) for _ in range(BACKUP_CODES)]
    _store(settings).set(
        SECRET_DOC,
        {"secret": pending, "last": counter, "backup": [_hash(c) for c in codes]},
    )
    _set_mfa(engine, account_id, "totp")
    return codes


def set_mail_mfa(engine: Engine, settings: Any, account_id: int, on: bool) -> None:
    if on:
        from vibesynergy import mailer

        if not mailer.configured(settings):
            raise AuthError("Set up mail first (Settings → Alerts), so the code can reach you.")
        _store(settings).set(SECRET_DOC, {})
        _set_mfa(engine, account_id, "mail")
    else:
        _store(settings).set(SECRET_DOC, {})
        _set_mfa(engine, account_id, "none")


def _set_mfa(engine: Engine, account_id: int, method: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(auth_account)
            .where(auth_account.c.id == account_id)
            .values(mfa=method, updated_at=utc_now())
        )


def check_second_factor(engine: Engine, settings: Any, account_id: int, code: str) -> bool:
    """A TOTP code, a backup code (spent), or the code that was mailed."""
    acc = account(engine)
    if acc is None or acc["id"] != account_id:
        return False
    if acc["mfa"] == "mail":
        return use_token(engine, (code or "").strip(), "mfa_mail") == account_id
    if acc["mfa"] != "totp":
        return True
    doc = totp_secret(settings)
    counter = totp_check(doc.get("secret", ""), code, last=doc.get("last"))
    if counter is not None:
        _store(settings).set(SECRET_DOC, {**doc, "last": counter})
        return True
    backup = _hash((code or "").strip().lower())
    if backup in (doc.get("backup") or []):
        _store(settings).set(
            SECRET_DOC, {**doc, "backup": [b for b in doc["backup"] if b != backup]}
        )
        return True
    return False


def backup_codes_left(settings: Any) -> int:
    return len(totp_secret(settings).get("backup") or [])


def clean(engine: Engine, now: datetime | None = None) -> None:
    """Spent and expired tokens and sessions out (called now and then)."""
    now = now or utc_now()
    with engine.begin() as conn:
        conn.execute(delete(auth_token).where(auth_token.c.expires_at < now - timedelta(days=1)))
        conn.execute(delete(auth_session).where(auth_session.c.expires_at < now))


def audit(engine: Engine, action: str, client: str, outcome: str = "ok") -> None:
    """Every login, refusal, link and change in the audit log (no secrets, no email)."""
    from vibesynergy.store.schema import audit_log

    with engine.begin() as conn:
        conn.execute(
            audit_log.insert().values(
                at=utc_now(),
                actor="portal",
                action=action,
                detail={"client": client},
                outcome=outcome,
            )
        )
