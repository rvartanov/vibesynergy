"""OAuth 2.1 for the MCP endpoint: VibeSynergy is its own authorization server, for one user.

How an AI app (Claude, ChatGPT, …) gets access:

1. It calls ``<public_url>/vibesynergy/mcp`` without a token and receives 401 with a
   ``WWW-Authenticate`` header pointing at the protected-resource metadata.
2. It registers itself (dynamic client registration) and sends the user's browser to
   ``/vibesynergy/authorize``. VibeSynergy shows a page naming the app and each permission in
   plain words. The owner types the passphrase (set in Settings → Coach apps) and approves.
3. The app exchanges the one-time code (with PKCE) for an access token (1 hour) and a
   refresh token (90 days, replaced on every use). Each app's access can be revoked with
   ``vibesynergy mcp revoke <client id>``.

All public paths live under ``/vibesynergy`` because the rest of the host belongs to other
services. Discovery therefore relies on the ``resource_metadata`` URL in the 401 header and
on RFC 8414 authorization-server metadata. The RFC 8414 path-inserted location is served
for a proxy that forwards it. The RFC 9728 path-inserted location for this resource
(``/.well-known/oauth-protected-resource/vibesynergy/mcp`` at the host root) is not reachable
through the Funnel; clients use the ``resource_metadata`` URL of the 401 header instead, and
the document's ``resource`` always equals the MCP URL the client called.

MCP clients whose issuer has a path try ``{issuer}/.well-known/openid-configuration`` after
the path-inserted well-known URLs. That URL is under ``/vibesynergy``, so the Funnel delivers
it, and it advertises the same absolute endpoints as the RFC 8414 document (including
``registration_endpoint``). The MCP TypeScript SDK parses that URL with its OpenID discovery
schema, so the alias also carries the fields that schema requires. VibeSynergy does not issue
ID tokens and ``openid`` is not a scope. Clients that probe under the MCP address
(``/vibesynergy/mcp/.well-known/`` and the same paths with the prefix stripped) receive those
same documents; the routes are registered before the MCP endpoint. Clients that ignore
metadata and join ``/register`` onto the issuer drop the path
(``new URL("/register", "https://host/vibesynergy")`` is ``https://host/register``); that
host-root URL is not this service.

Codes and tokens are stored as SHA-256 hashes only. The passphrase is stored as an scrypt
hash. Failed passphrase attempts are limited (5 per 15 minutes), and every approval and
failure is written to the audit log.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse, urlsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl, TypeAdapter, ValidationError
from sqlalchemy import Engine, select, update
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from vibesynergy.core.secrets import SecretStore
from vibesynergy.core.timeutil import utc_now
from vibesynergy.core.user_datetime import format_user_datetime
from vibesynergy.i18n import Translator, request_language, translator
from vibesynergy.mcp.scopes import (
    ADVERTISED_SCOPES,
    COACH_SCOPES,
    LEGACY_EXPANSION,
    LEGACY_SCOPE,
    LEGACY_SCOPE_ERROR,
    PAT_DAYS,
    PAT_DEFAULT_DAYS,
    SCOPE_LABELS,
    check_scope_names,
    expand_scopes,
)
from vibesynergy.store.schema import audit_log, mcp_pat, oauth_client, oauth_grant

_OAUTH_ALREADY_APPROVED_BODY = (
    "This request was already approved. If your app did not continue, "
    "start the connection again from the app."
)
_OAUTH_APPROVED_COPY_HINT = (
    "Select the link above to go back to your app. If that does not work, for example "
    "because you approved on another device, copy this URL and open it on the machine "
    "running the app, or paste the code into the app."
)
_OAUTH_DENY_COPY_HINT = (
    "If the link above does not work, copy this URL and open it on the machine running the app."
)
_OAUTH_INTRO_DENIED = "{title} — {client} was not allowed to use your account."
_OAUTH_ACCESS_INTRO = "wants access to your VibeSynergy account. Choose what it may do:"
_OAUTH_SCOPE_LEGEND = "What it may do"
_OAUTH_INTRO_RETURN = "{title} — return to {client}."
UI_STRINGS = (
    "Access denied",
    "Allow",
    "Allow access to VibeSynergy?",
    "Already approved",
    "An app",
    "Approved",
    "Callback URL",
    "Client registered: {created}.",
    "Code",
    "Continue to {client}",
    "Deny",
    "First connection for this app.",
    _OAUTH_DENY_COPY_HINT,
    _OAUTH_APPROVED_COPY_HINT,
    "It will return to {redirect}.",
    "Only paste this into an app you started yourself.",
    "Passphrase",
    "Request expired.",
    "Return to {client}",
    "This approval request has expired. Start again from the app.",
    _OAUTH_ALREADY_APPROVED_BODY,
    "Too many wrong attempts. Try again in 15 minutes.",
    "VibeSynergy: Access denied",
    "VibeSynergy: Approved",
    "VibeSynergy: Already approved",
    "VibeSynergy: allow access?",
    "Wrong passphrase ({n} attempts left).",
    _OAUTH_ACCESS_INTRO,
    _OAUTH_SCOPE_LEGEND,
    "Choose at least one permission.",
    *SCOPE_LABELS.values(),
    _OAUTH_INTRO_RETURN,
    _OAUTH_INTRO_DENIED,
)

# A request or stored grant without any scope keeps meaning what it always did: every
# scope except publish and plan.write, now spelled out.
DEFAULT_SCOPES = list(LEGACY_EXPANSION)
log = logging.getLogger(__name__)
PAT_UNUSED_AFTER = timedelta(days=180)
ACCESS_TTL = timedelta(hours=1)
REFRESH_TTL = timedelta(days=90)
CODE_TTL = timedelta(minutes=10)
REQUEST_TTL = 600.0  # seconds to type the passphrase
PAT_PREFIX = "vit_pat_"
PAT_LAST_USED_INTERVAL = timedelta(minutes=1)
MAX_FAILURES = 5  # wrong passphrases per app (or portal) per window
GLOBAL_MAX_FAILURES = 50  # all apps together per window: a ceiling on guessing, not a lock-out
FAILURE_WINDOW = 900.0  # seconds
MIN_PASSPHRASE = 12

_SCRYPT = {"n": 2**15, "r": 8, "p": 1}
_ANY_URL = TypeAdapter(AnyUrl)
# Native apps register private-use callback schemes (RFC 8252 section 7.1), with or without a
# dot (cursor://, zed://, com.example.app:/). Any scheme is accepted except these, which
# execute script, read local files or are plain web/network schemes with their own rules.
_BLOCKED_REDIRECT_SCHEMES = frozenset(
    {
        "javascript",
        "data",
        "file",
        "vbscript",
        "blob",
        "about",
        "filesystem",
        "view-source",
        "jar",
        "intent",
        "ftp",
        "ftps",
        "ws",
        "wss",
        "mailto",
        "tel",
        "sms",
        "ssh",
        "telnet",
        "gopher",
        "ldap",
        "chrome",
        "chrome-extension",
        "resource",
    }
)


def _resource_matches(value: str | None, resource: str) -> bool:
    """The only accepted resource indicator is this MCP endpoint, with one optional slash."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    return (value[:-1] if value.endswith("/") else value) == resource


def _is_loopback_redirect(uri: str) -> bool:
    parsed = urlsplit(uri)
    if parsed.scheme != "http" or parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _is_private_use_redirect(uri: str) -> bool:
    """RFC 8252 section 7.1 private-use scheme for native apps, e.g. ``cursor://...``."""
    scheme = urlsplit(uri).scheme.lower()
    return scheme not in ("", "http", "https") and scheme not in _BLOCKED_REDIRECT_SCHEMES


def _valid_redirect_uri(uri: object) -> bool:
    if not isinstance(uri, str) or not uri or uri != uri.strip():
        return False
    try:
        _ANY_URL.validate_python(uri)
        parsed = urlsplit(uri)
        if parsed.fragment or parsed.username or parsed.password:
            return False
        if _is_private_use_redirect(uri):
            return True
        if not parsed.hostname:
            return False
        if parsed.port is not None and parsed.port == 0:
            return False
        return parsed.scheme == "https" or _is_loopback_redirect(uri)
    except (ValueError, ValidationError):
        return False


class _RegisteredClient(OAuthClientInformationFull):
    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        try:
            return super().validate_redirect_uri(redirect_uri)
        except InvalidRedirectUriError:
            if redirect_uri is not None and _is_loopback_redirect(str(redirect_uri)):
                requested = urlsplit(str(redirect_uri))
                for registered in self.redirect_uris or []:
                    known = urlsplit(str(registered))
                    if (
                        _is_loopback_redirect(str(registered))
                        and not requested.fragment
                        and requested.scheme == known.scheme
                        and requested.hostname == known.hostname
                        and requested.path == known.path
                        and requested.query == known.query
                    ):
                        return redirect_uri
            raise


# Passphrase -----------------------------------------------------------------------------------


def set_passphrase(store: SecretStore, passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise ValueError("Use at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(passphrase.encode(), salt=salt, maxmem=2**26, **_SCRYPT)
    store.set("passphrase", {"salt": salt.hex(), "hash": digest.hex(), **_SCRYPT})


def check_passphrase(store: SecretStore, passphrase: str) -> bool:
    record = store.get("passphrase")
    if not record:
        return False
    digest = hashlib.scrypt(
        passphrase.encode(),
        salt=bytes.fromhex(record["salt"]),
        n=record["n"],
        r=record["r"],
        p=record["p"],
        maxmem=2**26,
    )
    return hmac.compare_digest(digest.hex(), record["hash"])


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class _ApprovedRequest:
    approved_at: float  # time.monotonic()
    client_id: str
    code: str


@dataclass(frozen=True)
class ApproveOutcome:
    kind: Literal["success", "already", "expired", "locked", "wrong_passphrase", "no_scope"]
    client_name: str = ""
    callback_url: str = ""
    code: str = ""
    message: str = ""
    attempts_left: int = -1


@dataclass(frozen=True)
class DenyOutcome:
    client_name: str
    callback_url: str
    redirect_uri: str


PORTAL_KEY = "portal"
# Registration is open (RFC 7591). Apps nobody approved are kept this long and this many.
UNAPPROVED_CLIENT_TTL = timedelta(hours=24)
MAX_UNAPPROVED_CLIENTS = 50
MAX_PENDING_REQUESTS = 200


class PassphraseLimiter:
    """Wrong-passphrase counter per key (an app's client id, or ``portal``), with a ceiling.

    Registration is open and the approve page is reachable from the internet, so a
    stranger can register an app and burn its attempts. Counting per app keeps the athlete's
    own apps (and the portal) usable. The global ceiling still caps how fast anyone can
    guess across many registrations; at 50 per 15 minutes against a 12+ character scrypt
    passphrase that is no practical attack, and it is far above what one person mistypes.
    """

    def __init__(
        self,
        *,
        per_key: int = MAX_FAILURES,
        ceiling: int = GLOBAL_MAX_FAILURES,
        window: float = FAILURE_WINDOW,
        clock=time.monotonic,
    ) -> None:
        self.per_key = per_key
        self.ceiling = ceiling
        self.window = window
        self._clock = clock
        self._by_key: dict[str, deque[float]] = {}
        self._all: deque[float] = deque()

    def _prune(self) -> None:
        cutoff = self._clock() - self.window
        while self._all and self._all[0] <= cutoff:
            self._all.popleft()
        for key in list(self._by_key):
            entries = self._by_key[key]
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if not entries:
                del self._by_key[key]

    def locked(self, key: str, *, trusted: bool = False) -> bool:
        """``trusted`` (the portal, an app the athlete approved before) is held to its own
        count only: strangers registering apps and burning attempts reach the ceiling, and
        that must not keep the athlete from approving their own apps."""
        self._prune()
        if not (trusted or key == PORTAL_KEY) and len(self._all) >= self.ceiling:
            return True
        return len(self._by_key.get(key, ())) >= self.per_key

    def fail(self, key: str, *, trusted: bool = False) -> int:
        """Record a wrong passphrase; returns the attempts this key has left."""
        now = self._clock()
        self._by_key.setdefault(key, deque()).append(now)
        self._all.append(now)
        self._prune()
        left = self.per_key - len(self._by_key.get(key, ()))
        if trusted or key == PORTAL_KEY:
            return max(0, left)
        return max(0, min(left, self.ceiling - len(self._all)))


# Provider -------------------------------------------------------------------------------------


class VibeSynergyOAuthProvider:
    """``OAuthAuthorizationServerProvider`` backed by SQLite, with a passphrase approval page."""

    def __init__(self, engine: Engine, store: SecretStore, issuer: str, resource: str) -> None:
        self.engine = engine
        self.store = store
        self.issuer = issuer.rstrip("/")
        self.resource = resource
        self._pending: dict[str, tuple[float, str, AuthorizationParams]] = {}
        self._approved: dict[str, _ApprovedRequest] = {}
        self.limiter = PassphraseLimiter()
        try:
            expand_legacy_scope_grants(engine)
        except Exception:  # never block startup on the one-time rewrite
            log.exception("could not rewrite grants that still hold the 'vibesynergy' scope")

    # Clients --------------------------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(oauth_client.c.info).where(
                    oauth_client.c.client_id == client_id, oauth_client.c.revoked_at.is_(None)
                )
            ).scalar()
        return _RegisteredClient.model_validate(row) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with self.engine.begin() as conn:
            # Anyone can register; what was never approved does not pile up.
            never = oauth_client.c.approved_at.is_(None)
            conn.execute(
                oauth_client.delete().where(
                    never, oauth_client.c.created_at < utc_now() - UNAPPROVED_CLIENT_TTL
                )
            )
            newest = (
                select(oauth_client.c.client_id)
                .where(never)
                .order_by(oauth_client.c.created_at.desc())
                .limit(MAX_UNAPPROVED_CLIENTS - 1)
            )
            conn.execute(
                oauth_client.delete().where(never, oauth_client.c.client_id.not_in(newest))
            )
            conn.execute(
                oauth_client.insert().values(
                    client_id=client_info.client_id,
                    info=client_info.model_dump(mode="json", exclude_none=True),
                    created_at=utc_now(),
                )
            )
        self._audit(
            "oauth.register",
            {"client_id": client_info.client_id, "client_name": client_info.client_name},
        )

    # Authorization --------------------------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Park the request and send the browser to the passphrase page."""
        if not _resource_matches(params.resource, self.resource):
            raise AuthorizeError("invalid_target", "unsupported resource")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "invalid S256 code_challenge")
        if LEGACY_SCOPE in (params.scopes or []):
            self._audit("oauth.legacy_scope", {"client_id": client.client_id})
            raise AuthorizeError("invalid_scope", LEGACY_SCOPE_ERROR)
        if not self.store.get("passphrase"):
            raise AuthorizeError(
                "temporarily_unavailable",
                "no passphrase set on the server yet; run `vibesynergy mcp set-passphrase`",
            )
        self._expire_pending()
        while len(self._pending) >= MAX_PENDING_REQUESTS:
            self._pending.pop(next(iter(self._pending)))  # the oldest; dicts keep order
        request_id = secrets.token_urlsafe(24)
        self._pending[request_id] = (time.monotonic(), client.client_id or "", params)
        return f"{self.issuer}/approve?request={request_id}"

    def pending(self, request_id: str) -> tuple[str, AuthorizationParams] | None:
        self._expire_pending()
        entry = self._pending.get(request_id)
        return (entry[1], entry[2]) if entry else None

    def locked(self, client_id: str) -> bool:
        """Too many wrong passphrases for this app, or for all apps together."""
        return self.limiter.locked(client_id, trusted=self._approved_before(client_id))

    def _approved_before(self, client_id: str) -> bool:
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    select(oauth_client.c.approved_at).where(oauth_client.c.client_id == client_id)
                ).scalar()
                is not None
            )

    def approved_outcome(self, request_id: str) -> ApproveOutcome | None:
        """Return a still-valid approval for ``request_id``, or None if unknown/expired."""
        self._expire_approved()
        record = self._approved.get(request_id)
        if record is None or not self._code_still_valid(record.code, record.client_id):
            return None
        return ApproveOutcome("already", self._client_name(record.client_id))

    def approve(
        self, request_id: str, passphrase: str, granted: list[str] | None = None
    ) -> ApproveOutcome:
        """Check the passphrase and issue a one-time code (or return an existing approval).

        The authorization code and callback URL are returned only on the first successful
        passphrase check; later GET/POST responses are credential-free.
        """
        cached = self.approved_outcome(request_id)
        if cached is not None:
            return cached
        entry = self.pending(request_id)
        if entry is None:
            return ApproveOutcome("expired")
        client_id, params = entry
        if self.locked(client_id):
            self._audit("oauth.locked", {"request": "approve", "client_id": client_id})
            return ApproveOutcome("locked")
        if not check_passphrase(self.store, passphrase):
            left = self.limiter.fail(client_id, trusted=self._approved_before(client_id))
            self._audit("oauth.denied", {"client_id": client_id, "reason": "wrong passphrase"})
            return ApproveOutcome("wrong_passphrase", attempts_left=left)
        requested = expand_scopes(params.scopes or DEFAULT_SCOPES)
        if granted is None:
            chosen = requested
        else:
            chosen = [scope for scope in expand_scopes(granted) if scope in requested]
        if not chosen:
            return ApproveOutcome("no_scope")
        del self._pending[request_id]
        code = secrets.token_urlsafe(32)
        callback_url = construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state, iss=self.issuer
        )
        self._store_grant(
            code,
            "code",
            client_id,
            {
                "scopes": chosen,
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": self.resource,
            },
            CODE_TTL,
        )
        with self.engine.begin() as conn:
            conn.execute(
                update(oauth_client)
                .where(oauth_client.c.client_id == client_id, oauth_client.c.approved_at.is_(None))
                .values(approved_at=utc_now())
            )
        self._audit("oauth.approved", {"client_id": client_id, "scopes": chosen})
        self._approved[request_id] = _ApprovedRequest(time.monotonic(), client_id, code)
        return ApproveOutcome(
            "success",
            self._client_name(client_id),
            callback_url,
            code,
        )

    def deny(self, request_id: str) -> DenyOutcome | None:
        entry = self.pending(request_id)
        if entry is None:
            return None
        del self._pending[request_id]
        client_id, params = entry
        self._audit("oauth.denied", {"client_id": client_id, "reason": "declined"})
        return DenyOutcome(
            self._client_name(client_id),
            construct_redirect_uri(
                str(params.redirect_uri),
                error="access_denied",
                state=params.state,
                iss=self.issuer,
            ),
            str(params.redirect_uri),
        )

    # Codes and tokens -----------------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        grant = self._load_grant(authorization_code, "code", client.client_id)
        if grant is None:
            return None
        data = grant["data"]
        return AuthorizationCode(
            code=authorization_code,
            scopes=data["scopes"],
            expires_at=grant["expires_at"].timestamp(),
            client_id=grant["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource") or self.resource,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not self._use_once(authorization_code.code, "code"):
            raise TokenError("invalid_grant", "authorization code already used")
        return self._issue(
            client.client_id or "", authorization_code.scopes, authorization_code.resource
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        grant = self._load_grant(refresh_token, "refresh", client.client_id)
        if grant is None:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=grant["client_id"],
            scopes=grant["data"]["scopes"],
            expires_at=int(grant["expires_at"].timestamp()),
            resource=grant["data"].get("resource") or self.resource,
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if not self._use_once(refresh_token.token, "refresh"):
            raise TokenError("invalid_grant", "refresh token already used")
        # The request's own `resource` was validated in token_route. A legacy grant may carry a
        # foreign stored resource; new tokens are always bound to this MCP resource.
        return self._issue(client.client_id or "", scopes or refresh_token.scopes, self.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        grant = self._load_grant(token, "access", None)
        if grant is None:
            return None
        resource = grant["data"].get("resource") or self.resource
        if not _resource_matches(resource, self.resource):
            return None
        return AccessToken(
            token=token,
            client_id=grant["client_id"],
            scopes=grant["data"]["scopes"],
            expires_at=int(grant["expires_at"].timestamp()),
            resource=self.resource,
        )

    async def load_personal_access_token(self, token: str) -> AccessToken | None:
        if not token.startswith(PAT_PREFIX):
            return None
        now = utc_now()
        token_hash = _hash(token)
        with self.engine.connect() as conn:
            # SHA-256 hex lookup in SQL; no secret compared in Python, so compare_digest N/A.
            row = conn.execute(select(mcp_pat).where(mcp_pat.c.token_hash == token_hash)).first()
        if row is None or row.revoked_at is not None:
            return None
        if row.expires_at is None or row.expires_at <= now:
            return None
        scopes = expand_scopes(row.scopes or DEFAULT_SCOPES)
        if not scopes:
            return None
        first_use = row.last_used_at is None
        if first_use or now - row.last_used_at >= PAT_LAST_USED_INTERVAL:
            with self.engine.begin() as conn:
                conn.execute(update(mcp_pat).where(mcp_pat.c.id == row.id).values(last_used_at=now))
        if first_use:
            self._audit("oauth.pat.first_use", {"name": row.name, "id": row.id})
        return AccessToken(
            token=token,
            client_id=f"pat:{row.id}",
            scopes=scopes,
            expires_at=int(row.expires_at.timestamp()),
            resource=self.resource,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                update(oauth_grant)
                .where(oauth_grant.c.token_hash == _hash(token.token))
                .values(used_at=utc_now())
            )

    # Helpers --------------------------------------------------------------------------------

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if not _resource_matches(resource, self.resource):
            raise TokenError("invalid_target", "unsupported resource")
        data = {"scopes": scopes, "resource": self.resource}
        self._store_grant(access, "access", client_id, data, ACCESS_TTL)
        self._store_grant(refresh, "refresh", client_id, data, REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            expires_in=int(ACCESS_TTL.total_seconds()),
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    def _store_grant(
        self, token: str, kind: str, client_id: str, data: dict, ttl: timedelta
    ) -> None:
        now = utc_now()
        with self.engine.begin() as conn:
            conn.execute(
                oauth_grant.insert().values(
                    token_hash=_hash(token),
                    kind=kind,
                    client_id=client_id,
                    data=data,
                    expires_at=now + ttl,
                    created_at=now,
                )
            )

    def _load_grant(self, token: str, kind: str, client_id: str | None) -> dict | None:
        query = (
            select(oauth_grant, oauth_client.c.revoked_at)
            .join(oauth_client, oauth_client.c.client_id == oauth_grant.c.client_id)
            .where(oauth_grant.c.token_hash == _hash(token), oauth_grant.c.kind == kind)
        )
        with self.engine.connect() as conn:
            row = conn.execute(query).first()
        if row is None or row.revoked_at is not None or row.used_at is not None:
            return None
        if row.expires_at <= utc_now():
            return None
        if client_id is not None and row.client_id != client_id:
            return None
        return dict(row._mapping)

    def _use_once(self, token: str, kind: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                update(oauth_grant)
                .where(
                    oauth_grant.c.token_hash == _hash(token),
                    oauth_grant.c.kind == kind,
                    oauth_grant.c.used_at.is_(None),
                )
                .values(used_at=utc_now())
            )
        return result.rowcount == 1

    def _expire_pending(self) -> None:
        now = time.monotonic()
        for key in [k for k, (t, _, _) in self._pending.items() if now - t > REQUEST_TTL]:
            del self._pending[key]

    def _expire_approved(self) -> None:
        now = time.monotonic()
        ttl = CODE_TTL.total_seconds()
        for key, record in list(self._approved.items()):
            if now - record.approved_at > ttl or not self._code_still_valid(
                record.code, record.client_id
            ):
                del self._approved[key]

    def _code_still_valid(self, code: str, client_id: str) -> bool:
        return self._load_grant(code, "code", client_id) is not None

    def _client_name(self, client_id: str) -> str:
        with self.engine.connect() as conn:
            info = (
                conn.execute(
                    select(oauth_client.c.info).where(oauth_client.c.client_id == client_id)
                ).scalar()
                or {}
            )
        return info.get("client_name") or "the app"

    def label_for(self, client_id: str) -> str:
        """Display name for diary notes and the coach feed. Empty when unknown."""
        if client_id.startswith("pat:"):
            pat_id = client_id.removeprefix("pat:")
            with self.engine.connect() as conn:
                row = conn.execute(select(mcp_pat).where(mcp_pat.c.id == pat_id)).first()
            if row is None:
                return ""
            return (row.display_name or row.name or "").strip()
        name = self._client_name(client_id)
        return "" if name == "the app" else name

    def _audit(self, action: str, detail: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                audit_log.insert().values(at=utc_now(), actor="oauth", action=action, detail=detail)
            )


def expand_legacy_scope_grants(engine: Engine) -> dict[str, int]:
    """Rewrite stored grants that still hold the retired ``vibesynergy`` scope, once.

    Personal access tokens, app registrations and live tokens get the concrete names the
    old scope stood for, so nothing loses or gains access. Idempotent: a second call finds
    nothing. Returns how many rows of each kind changed; an audit entry records them.
    """
    counts = {"tokens": 0, "apps": 0, "grants": 0}
    with engine.begin() as conn:
        for row in conn.execute(select(mcp_pat.c.id, mcp_pat.c.scopes)).all():
            if row.scopes and LEGACY_SCOPE in row.scopes:
                conn.execute(
                    update(mcp_pat)
                    .where(mcp_pat.c.id == row.id)
                    .values(scopes=expand_scopes(row.scopes))
                )
                counts["tokens"] += 1
        for row in conn.execute(select(oauth_client.c.client_id, oauth_client.c.info)).all():
            names = str((row.info or {}).get("scope") or "").split()
            if LEGACY_SCOPE in names:
                # Keep any names the registration listed after the expansion as well.
                kept = expand_scopes(names)
                info = {**row.info, "scope": " ".join(kept)}
                conn.execute(
                    update(oauth_client)
                    .where(oauth_client.c.client_id == row.client_id)
                    .values(info=info)
                )
                counts["apps"] += 1
        for row in conn.execute(select(oauth_grant.c.token_hash, oauth_grant.c.data)).all():
            data = row.data if isinstance(row.data, dict) else {}
            scopes = data.get("scopes") or []
            if LEGACY_SCOPE in scopes:
                conn.execute(
                    update(oauth_grant)
                    .where(oauth_grant.c.token_hash == row.token_hash)
                    .values(data={**data, "scopes": expand_scopes(scopes)})
                )
                counts["grants"] += 1
        if any(counts.values()):
            conn.execute(
                audit_log.insert().values(
                    at=utc_now(), actor="oauth", action="oauth.legacy_scope.expanded", detail=counts
                )
            )
            log.warning("expanded the retired 'vibesynergy' scope in stored grants: %s", counts)
    return counts


# Clients (CLI) --------------------------------------------------------------------------------


def list_clients(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(oauth_client).order_by(oauth_client.c.created_at)).all()
    return [
        {
            "client_id": r.client_id,
            "name": (r.info or {}).get("client_name"),
            "display_name": (r.info or {}).get("display_name") or (r.info or {}).get("client_name"),
            "redirect": ", ".join(
                urlparse(u).netloc for u in (r.info or {}).get("redirect_uris", [])
            ),
            "created": r.created_at,
            "approved": r.approved_at,
            "revoked": r.revoked_at,
        }
        for r in rows
    ]


def revoke_client(engine: Engine, client_id: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            update(oauth_client)
            .where(oauth_client.c.client_id == client_id, oauth_client.c.revoked_at.is_(None))
            .values(revoked_at=utc_now())
        )
    return result.rowcount == 1


def create_personal_access_token(
    engine: Engine,
    name: str,
    *,
    scopes: list[str] | None = None,
    days: int = PAT_DEFAULT_DAYS,
    display_name: str | None = None,
) -> str:
    """Create a personal access token. The secret is returned once and only a hash is stored.

    ``days`` is 30, 90 or 365. There is no never-expiring token. Omitted scopes become the
    coach set (read, messages, diary, plan suggestions).
    """
    name = name.strip()
    if not name:
        raise ValueError("A name is required.")
    if days not in PAT_DAYS:
        raise ValueError("Choose 30, 90 or 365 days.")
    if scopes is None:
        chosen = expand_scopes(COACH_SCOPES)
    else:
        chosen = expand_scopes(check_scope_names(scopes))
    if not chosen:
        raise ValueError("Choose at least one permission.")
    label = (display_name or name).strip() or name
    token = PAT_PREFIX + secrets.token_urlsafe(32)
    now = utc_now()
    row = {
        "id": secrets.token_hex(8),
        "name": name,
        "token_hash": _hash(token),
        "token_prefix": token[: len(PAT_PREFIX) + 8],
        "created_at": now,
        "last_used_at": None,
        "revoked_at": None,
        "expires_at": now + timedelta(days=days),
        "scopes": chosen,
        "display_name": label,
    }
    with engine.begin() as conn:
        conn.execute(mcp_pat.insert().values(**row))
        conn.execute(
            audit_log.insert().values(
                at=now,
                actor="oauth",
                action="oauth.pat.create",
                detail={"name": name, "id": row["id"], "scopes": chosen, "days": days},
            )
        )
    return token


def _pat_row(row: Any, now: Any | None = None) -> dict[str, Any]:
    moment = now or utc_now()
    anchor = row.last_used_at or row.created_at
    stale = bool(
        row.revoked_at is None and anchor is not None and moment - anchor >= PAT_UNUSED_AFTER
    )
    return {
        "id": row.id,
        "name": row.name,
        "display_name": row.display_name or row.name,
        "prefix": row.token_prefix,
        "scopes": expand_scopes(row.scopes or DEFAULT_SCOPES),
        "created": row.created_at,
        "last_used": row.last_used_at,
        "expires_at": row.expires_at,
        "revoked": row.revoked_at,
        "stale": stale,
    }


def list_personal_access_tokens(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(select(mcp_pat).order_by(mcp_pat.c.created_at)).all()
    return [_pat_row(r) for r in rows]


def rename_personal_access_token(engine: Engine, token_id: str, display_name: str) -> bool:
    label = display_name.strip()
    if not label:
        raise ValueError("name is required")
    with engine.begin() as conn:
        result = conn.execute(
            update(mcp_pat).where(mcp_pat.c.id == token_id).values(display_name=label)
        )
    return result.rowcount == 1


def rename_oauth_client(engine: Engine, client_id: str, display_name: str) -> bool:
    label = display_name.strip()
    if not label:
        raise ValueError("name is required")
    with engine.begin() as conn:
        info = conn.execute(
            select(oauth_client.c.info).where(oauth_client.c.client_id == client_id)
        ).scalar()
        if info is None:
            return False
        updated = dict(info)
        updated["display_name"] = label
        result = conn.execute(
            update(oauth_client).where(oauth_client.c.client_id == client_id).values(info=updated)
        )
    return result.rowcount == 1


def update_personal_access_token_scopes(
    engine: Engine,
    name: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    """Change the scopes of an active token. The secret hash is not touched.

    A scope named in both lists is removed. The result must still include one permission.
    """
    granted = check_scope_names(add or [])
    dropped = set(check_scope_names(remove or []))
    if not granted and not dropped:
        raise ValueError("Name a scope to add or remove.")
    label = name.strip()
    if not label:
        raise ValueError("A name is required.")
    now = utc_now()
    with engine.begin() as conn:
        row = conn.execute(
            select(mcp_pat).where(mcp_pat.c.revoked_at.is_(None), mcp_pat.c.name == label)
        ).first()
        if row is None:
            raise ValueError(f"No active token named {label}.")
        current = expand_scopes(row.scopes or DEFAULT_SCOPES)
        updated = [scope for scope in current if scope not in dropped]
        for scope in granted:
            if scope not in dropped and scope not in updated:
                updated.append(scope)
        updated = expand_scopes(updated)
        if not updated:
            raise ValueError("Choose at least one permission.")
        conn.execute(update(mcp_pat).where(mcp_pat.c.id == row.id).values(scopes=updated))
        conn.execute(
            audit_log.insert().values(
                at=now,
                actor="oauth",
                action="oauth.pat.scopes",
                detail={"name": row.name, "id": row.id, "before": current, "after": updated},
            )
        )
    return updated


def update_oauth_client_scopes(
    engine: Engine,
    client_id: str,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
) -> list[str]:
    """Change what an approved app may do, without a new consent round.

    Updates the registration (what a later consent page offers) and the app's live access
    and refresh tokens. A scope named in both lists is removed.
    """
    granted = check_scope_names(add or [])
    dropped = set(check_scope_names(remove or []))
    if not granted and not dropped:
        raise ValueError("Name a scope to add or remove.")
    now = utc_now()
    with engine.begin() as conn:
        row = conn.execute(
            select(oauth_client).where(
                oauth_client.c.client_id == client_id.strip(), oauth_client.c.revoked_at.is_(None)
            )
        ).first()
        if row is None or row.approved_at is None:
            raise ValueError(f"No approved app with client id {client_id.strip()}.")
        current = expand_scopes((row.info.get("scope") or " ".join(DEFAULT_SCOPES)).split())
        updated = [scope for scope in current if scope not in dropped]
        for scope in granted:
            if scope not in dropped and scope not in updated:
                updated.append(scope)
        updated = expand_scopes(updated)
        if not updated:
            raise ValueError("Choose at least one permission.")
        info = dict(row.info)
        info["scope"] = " ".join(updated)
        conn.execute(
            update(oauth_client).where(oauth_client.c.client_id == row.client_id).values(info=info)
        )
        live = conn.execute(
            select(oauth_grant.c.token_hash, oauth_grant.c.data).where(
                oauth_grant.c.client_id == row.client_id,
                oauth_grant.c.kind.in_(("access", "refresh")),
                oauth_grant.c.used_at.is_(None),
                oauth_grant.c.expires_at > now,
            )
        ).all()
        for grant in live:
            data = dict(grant.data)
            data["scopes"] = updated
            conn.execute(
                update(oauth_grant)
                .where(oauth_grant.c.token_hash == grant.token_hash)
                .values(data=data)
            )
        conn.execute(
            audit_log.insert().values(
                at=now,
                actor="oauth",
                action="oauth.client.scopes",
                client_id=row.client_id,
                detail={
                    "client_id": row.client_id,
                    "before": current,
                    "after": updated,
                    "tokens": len(live),
                },
            )
        )
    return updated


def revoke_personal_access_token(engine: Engine, name_or_id: str) -> bool:
    now = utc_now()
    with engine.begin() as conn:
        row = conn.execute(
            select(mcp_pat).where(mcp_pat.c.revoked_at.is_(None), mcp_pat.c.id == name_or_id)
        ).first()
        if row is None:
            row = conn.execute(
                select(mcp_pat).where(mcp_pat.c.revoked_at.is_(None), mcp_pat.c.name == name_or_id)
            ).first()
        if row is None:
            return False
        conn.execute(update(mcp_pat).where(mcp_pat.c.id == row.id).values(revoked_at=now))
        conn.execute(
            audit_log.insert().values(
                at=now,
                actor="oauth",
                action="oauth.pat.revoke",
                detail={"name": row.name, "id": row.id},
            )
        )
    return True


# HTTP -----------------------------------------------------------------------------------------


def metadata_documents(issuer: str, resource: str) -> tuple[dict, dict]:
    """(authorization server metadata, protected resource metadata)."""
    from mcp.server.auth.routes import build_metadata

    server = build_metadata(
        AnyHttpUrl(issuer),
        None,
        ClientRegistrationOptions(
            enabled=True,
            valid_scopes=list(ADVERTISED_SCOPES),
            default_scopes=list(ADVERTISED_SCOPES),
        ),
        RevocationOptions(enabled=True),
    ).model_dump(mode="json", exclude_none=True)
    # The SDK's default omits public clients even though its token and revocation
    # authenticators accept DCR registrations with method "none".
    methods = ["none", "client_secret_post", "client_secret_basic"]
    server["token_endpoint_auth_methods_supported"] = methods
    server["revocation_endpoint_auth_methods_supported"] = methods
    protected = {
        "resource": resource,
        "authorization_servers": [issuer],
        "scopes_supported": list(ADVERTISED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "VibeSynergy",
    }
    return server, protected


def path_prefix_discovery_document(server: dict, issuer: str) -> dict:
    """Authorization-server metadata for the path-appended discovery URL.

    MCP SDK ``buildDiscoveryUrls`` tries, for an issuer ``https://host/vibesynergy``:

    1. ``https://host/.well-known/oauth-authorization-server/vibesynergy`` (path-inserted;
       the Funnel does not forward this)
    2. ``https://host/.well-known/openid-configuration/vibesynergy`` (same)
    3. ``https://host/vibesynergy/.well-known/openid-configuration`` (under ``/vibesynergy``;
       this is the probe that reaches VibeSynergy)

    Step 3 is parsed with the OpenID discovery schema, which requires ``jwks_uri``,
    ``subject_types_supported`` and ``id_token_signing_alg_values_supported``. The
    endpoints themselves are the RFC 8414 values, absolute and under ``/vibesynergy``.
    The JWKS is empty: no ID token is signed.
    """
    document = dict(server)
    document["jwks_uri"] = f"{issuer}/.well-known/jwks.json"
    document["subject_types_supported"] = ["public"]
    document["id_token_signing_alg_values_supported"] = ["RS256"]
    return document


def routes(provider: VibeSynergyOAuthProvider) -> list[Route]:
    """All OAuth routes, each served under /vibesynergy and (for the proxy) without it."""
    server_meta, protected_meta = metadata_documents(provider.issuer, provider.resource)
    sdk = create_auth_routes(
        provider,  # type: ignore[arg-type]
        AnyHttpUrl(provider.issuer),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=list(ADVERTISED_SCOPES),
            default_scopes=list(ADVERTISED_SCOPES),
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    endpoints = {r.path: r for r in sdk if not r.path.startswith("/.well-known")}

    class ASGIEndpoint:
        def __init__(self, handler):
            self.handler = handler

        async def __call__(self, scope, receive, send):
            await self.handler(scope, receive, send)

    def replay_body(body: bytes):
        sent = False

        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive

    async def authorization_error(params, error: str, description: str) -> Response:
        client_id = params.get("client_id")
        client = await provider.get_client(client_id) if isinstance(client_id, str) else None
        if client:
            try:
                raw = params.get("redirect_uri")
                redirect = client.validate_redirect_uri(
                    _ANY_URL.validate_python(raw) if raw is not None else None
                )
            except (ValidationError, InvalidRedirectUriError):
                pass
            else:
                state = params.get("state")
                return RedirectResponse(
                    construct_redirect_uri(
                        str(redirect),
                        error=error,
                        state=state if isinstance(state, str) else None,
                        iss=provider.issuer,
                    ),
                    status_code=302,
                    headers={"Cache-Control": "no-store"},
                )
        return JSONResponse(
            {"error": error, "error_description": description},
            status_code=400,
            headers={"Cache-Control": "no-store"},
        )

    async def authorize_route(scope, receive, send) -> None:
        request = Request(scope, receive)
        body = b""
        if request.method == "POST":
            body = await request.body()
            try:
                params = parse_qs(body.decode(), keep_blank_values=True)
            except UnicodeDecodeError:
                response = JSONResponse({"error": "invalid_request"}, status_code=400)
                await response(scope, receive, send)
                return
            method = params.get("code_challenge_method", [])
            resources = params.get("resource", [])
            params_for_error = {key: values[-1] for key, values in params.items()}
        else:
            params = request.query_params
            method = params.getlist("code_challenge_method")
            resources = params.getlist("resource")
            params_for_error = params
        error = None
        if method != ["S256"]:
            error = ("invalid_request", "code_challenge_method must be S256")
        elif len(resources) > 1 or any(
            not _resource_matches(value, provider.resource) for value in resources
        ):
            error = ("invalid_target", "unsupported resource")
        if error:
            response = await authorization_error(params_for_error, *error)
            await response(scope, receive, send)
            return
        await endpoints["/authorize"].endpoint(scope, replay_body(body) if body else receive, send)

    async def token_route(scope, receive, send) -> None:
        if scope["method"] == "OPTIONS":
            await endpoints["/token"].endpoint(scope, receive, send)
            return
        body = await Request(scope, receive).body()
        try:
            values = parse_qs(body.decode(), keep_blank_values=True).get("resource", [])
        except UnicodeDecodeError:
            response = JSONResponse(
                {"error": "invalid_request"},
                status_code=400,
                headers={"Access-Control-Allow-Origin": "*"},
            )
            await response(scope, receive, send)
            return
        if len(values) > 1 or any(
            not _resource_matches(value, provider.resource) for value in values
        ):
            response = JSONResponse(
                {"error": "invalid_target", "error_description": "unsupported resource"},
                status_code=400,
                headers={
                    "Cache-Control": "no-store",
                    "Pragma": "no-cache",
                    "Access-Control-Allow-Origin": "*",
                },
            )
            await response(scope, receive, send)
            return
        await endpoints["/token"].endpoint(scope, replay_body(body), send)

    async def register_route(scope, receive, send) -> None:
        if scope["method"] == "OPTIONS":
            await endpoints["/register"].endpoint(scope, receive, send)
            return
        body = await Request(scope, receive).body()
        try:
            data = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        error = None
        if not isinstance(data, dict):
            error = ("invalid_client_metadata", "registration body must be a JSON object")
        elif not isinstance(data.get("redirect_uris"), list) or not data["redirect_uris"]:
            error = ("invalid_redirect_uri", "redirect_uris must be a nonempty array")
        elif not all(_valid_redirect_uri(uri) for uri in data["redirect_uris"]):
            error = (
                "invalid_redirect_uri",
                "redirect_uris must use HTTPS, HTTP loopback or a private-use app scheme",
            )
        elif "grant_types" in data and (
            not isinstance(data["grant_types"], list)
            or any(
                grant not in ("authorization_code", "refresh_token")
                for grant in data["grant_types"]
            )
        ):
            error = ("invalid_client_metadata", "unsupported grant_types")
        elif data.get("response_types", ["code"]) != ["code"]:
            error = ("invalid_client_metadata", "response_types must be ['code']")
        if error:
            response = JSONResponse(
                {"error": error[0], "error_description": error[1]},
                status_code=400,
                headers={"Access-Control-Allow-Origin": "*"},
            )
            await response(scope, receive, send)
            return
        await endpoints["/register"].endpoint(scope, replay_body(body), send)

    async def server_metadata(request: Request) -> Response:
        return JSONResponse(server_meta, headers={"Access-Control-Allow-Origin": "*"})

    discovery_meta = path_prefix_discovery_document(server_meta, provider.issuer)

    async def path_prefix_discovery(request: Request) -> Response:
        return JSONResponse(discovery_meta, headers={"Access-Control-Allow-Origin": "*"})

    async def empty_jwks(request: Request) -> Response:
        return JSONResponse({"keys": []}, headers={"Access-Control-Allow-Origin": "*"})

    async def resource_metadata(request: Request) -> Response:
        return JSONResponse(protected_meta, headers={"Access-Control-Allow-Origin": "*"})

    async def approve_page(request: Request) -> Response:
        tr = translator(request_language(request))
        request_id = request.query_params.get("request", "")
        if request.method == "POST":
            form = parse_qs((await request.body()).decode())
            request_id = form.get("request", [""])[0]
            if form.get("action", [""])[0] == "deny":
                outcome = provider.deny(request_id)
                return (
                    _deny_result_page(outcome, tr)
                    if outcome is not None
                    else _page(tr, "Request expired.", "")
                )
            granted = form.get("scope") if form.get("scopes_shown") else None
            outcome = provider.approve(request_id, form.get("passphrase", [""])[0], granted=granted)
            return _respond_approve(provider, request_id, outcome, tr)
        cached = provider.approved_outcome(request_id)
        if cached is not None:
            return _already_approved_page(cached, tr)
        if provider.pending(request_id) is None:
            return _page(tr, "This approval request has expired. Start again from the app.", "")
        return _page(tr, "", request_id, provider)

    route_list: list[Route] = []
    for prefix in ("/vibesynergy", ""):
        for path, route in endpoints.items():
            endpoint = {
                "/authorize": ASGIEndpoint(authorize_route),
                "/token": ASGIEndpoint(token_route),
                "/register": ASGIEndpoint(register_route),
            }.get(path, route.endpoint)
            route_list.append(Route(prefix + path, endpoint=endpoint, methods=route.methods))
        route_list += [
            Route(prefix + "/approve", approve_page, methods=["GET", "POST"]),
            Route(prefix + "/.well-known/oauth-authorization-server", server_metadata),
            Route(prefix + "/.well-known/oauth-protected-resource", resource_metadata),
            # Path-appended discovery. With the prefix this is the URL the Funnel
            # forwards; without it, the same request after the prefix is stripped.
            Route(prefix + "/.well-known/openid-configuration", path_prefix_discovery),
            Route(prefix + "/.well-known/jwks.json", empty_jwks),
            # Under the MCP URL. create_app registers this list before the /mcp
            # endpoint, so that endpoint does not take these requests.
            Route(prefix + "/mcp/.well-known/oauth-protected-resource", resource_metadata),
            Route(prefix + "/mcp/.well-known/oauth-authorization-server", server_metadata),
            Route(prefix + "/mcp/.well-known/openid-configuration", path_prefix_discovery),
        ]
    route_list += [  # RFC 8414 / 9728 path-inserted locations, if the proxy forwards them
        Route("/.well-known/oauth-authorization-server/vibesynergy", server_metadata),
        Route("/.well-known/oauth-protected-resource/vibesynergy/mcp", resource_metadata),
        Route("/.well-known/openid-configuration/vibesynergy", path_prefix_discovery),
    ]
    return route_list


def _respond_approve(
    provider: VibeSynergyOAuthProvider,
    request_id: str,
    outcome: ApproveOutcome,
    tr: Translator,
) -> Response:
    if outcome.kind == "success":
        return _approval_result_page(outcome, tr)
    if outcome.kind == "already":
        return _already_approved_page(outcome, tr)
    if outcome.kind == "wrong_passphrase":
        message = tr(
            "Wrong passphrase ({n} attempts left).",
            n=outcome.attempts_left,
        )
        return _page(tr, message, request_id, provider, message_is_translated=True)
    if outcome.kind == "locked":
        return _page(tr, "Too many wrong attempts. Try again in 15 minutes.", "")
    if outcome.kind == "expired":
        return _page(tr, "This approval request has expired. Start again from the app.", "")
    if outcome.kind == "no_scope":
        return _page(tr, "Choose at least one permission.", request_id, provider)
    return _page(tr, outcome.message, "")


def _tx(tr: Translator, text: str, **values: Any) -> str:
    return tr(text, **{key: html.escape(str(val)) for key, val in values.items()})


def _html_shell(tr: Translator, title_key: str, inner: str, *, width_em: int = 36) -> str:
    title = _tx(tr, title_key)
    return (
        f'<!doctype html><html lang="{tr.lang}">'
        '<meta charset=utf-8><meta name=viewport content="width=device-width">'
        f"<title>{title}</title>"
        f'<body style="font-family:system-ui;max-width:{width_em}em;margin:3em auto;padding:0 1em">'
        f"<main>{inner}</main></body></html>"
    )


def _already_approved_page(outcome: ApproveOutcome, tr: Translator) -> Response:
    title_key = "Already approved"
    heading = _tx(tr, title_key)
    intro = (
        f"<p>{_tx(tr, _OAUTH_INTRO_RETURN, title=tr(title_key), client=outcome.client_name)}</p>"
    )
    body = _html_shell(
        tr,
        "VibeSynergy: Already approved",
        f"<h1>{heading}</h1>{intro}<p>{_tx(tr, _OAUTH_ALREADY_APPROVED_BODY)}</p>",
    )
    return HTMLResponse(body, headers=_html_headers(form=False))


def _approval_result_page(outcome: ApproveOutcome, tr: Translator) -> Response:
    # No timed redirect or meta-refresh for any callback scheme (WCAG 2.2.1): the user follows
    # the link, or copies the callback URL or code, at their own pace.
    title_key = "Approved"
    heading = _tx(tr, title_key)
    intro = (
        f"<p>{_tx(tr, _OAUTH_INTRO_RETURN, title=tr(title_key), client=outcome.client_name)}</p>"
    )
    callback = html.escape(outcome.callback_url, quote=True)
    code = html.escape(outcome.code)
    continue_link = _tx(tr, "Continue to {client}", client=outcome.client_name)
    callback_label = _tx(tr, "Callback URL")
    code_label = _tx(tr, "Code")
    inner = (
        f"<h1>{heading}</h1>{intro}"
        f'<p><a href="{callback}">{continue_link}</a></p>'
        f"<p>{_tx(tr, _OAUTH_APPROVED_COPY_HINT)}</p>"
        f"<p>{_tx(tr, 'Only paste this into an app you started yourself.')}</p>"
        f'<p><label>{callback_label}<br><input readonly style="width:100%" '
        f'value="{callback}"></label></p>'
        f'<p><label>{code_label}<br><input readonly style="width:100%" '
        f'value="{code}"></label></p>'
    )
    return HTMLResponse(
        _html_shell(tr, "VibeSynergy: Approved", inner),
        headers=_html_headers(form=False),
    )


def _deny_result_page(outcome: DenyOutcome, tr: Translator) -> Response:
    title_key = "Access denied"
    heading = _tx(tr, title_key)
    intro = (
        f"<p>{_tx(tr, _OAUTH_INTRO_DENIED, title=tr(title_key), client=outcome.client_name)}</p>"
    )
    callback = html.escape(outcome.callback_url, quote=True)
    return_link = _tx(tr, "Return to {client}", client=outcome.client_name)
    callback_label = _tx(tr, "Callback URL")
    inner = (
        f"<h1>{heading}</h1>{intro}"
        f'<p><a href="{callback}">{return_link}</a></p>'
        f"<p>{_tx(tr, _OAUTH_DENY_COPY_HINT)}</p>"
        f'<p><label>{callback_label}<br><input readonly style="width:100%" '
        f'value="{callback}"></label></p>'
    )
    return HTMLResponse(
        _html_shell(tr, "VibeSynergy: Access denied", inner),
        headers=_html_headers(form=False),
    )


def _html_headers(*, form: bool) -> dict[str, str]:
    csp = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    if form:
        csp += "; form-action 'self'"
    return {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
        "Content-Security-Policy": csp,
    }


def _page(
    tr: Translator,
    message: str,
    request_id: str,
    provider: VibeSynergyOAuthProvider | None = None,
    *,
    message_is_translated: bool = False,
) -> Response:
    app, redirect, created, first_connection = tr("An app"), "", "", False
    params = None
    if provider and request_id and (entry := provider.pending(request_id)):
        client_id, params = entry
        with provider.engine.connect() as conn:
            row = conn.execute(
                select(
                    oauth_client.c.info,
                    oauth_client.c.created_at,
                    oauth_client.c.approved_at,
                ).where(oauth_client.c.client_id == client_id)
            ).first()
        info = row.info if row is not None and row.info else {}
        app = info.get("client_name") or tr("An app")
        redirect = str(params.redirect_uri)
        if row is not None and row.created_at is not None:
            created = format_user_datetime(row.created_at)
            first_connection = row.approved_at is None
    form = ""
    field_error = " aria-invalid=true aria-describedby=form-error" if message else ""
    if request_id:
        created_note = (
            f"<p>{_tx(tr, 'Client registered: {created}.', created=created)}</p>" if created else ""
        )
        first_connection_note = (
            f"<p><strong>{_tx(tr, 'First connection for this app.')}</strong></p>"
            if first_connection
            else ""
        )
        app_name = html.escape(app)
        requested = (
            expand_scopes(list(params.scopes or DEFAULT_SCOPES)) if params is not None else []
        )
        boxes = "".join(
            "<label style='display:block;margin:.35em 0'>"
            f'<input type=checkbox name=scope value="{html.escape(scope, quote=True)}" checked> '
            f"{_tx(tr, SCOPE_LABELS[scope])}</label>"
            for scope in requested
            if scope in SCOPE_LABELS
        )
        form = f"""
        <p><b>{app_name}</b> {_tx(tr, _OAUTH_ACCESS_INTRO)}</p>
        <p>{_tx(tr, "It will return to {redirect}.", redirect=redirect)}</p>
        {created_note}
        {first_connection_note}
        <form method=post>
          <input type=hidden name=request value="{html.escape(request_id)}">
          <input type=hidden name=scopes_shown value=1>
          <fieldset>
            <legend>{_tx(tr, _OAUTH_SCOPE_LEGEND)}</legend>
            {boxes}
          </fieldset>
          <p><label>{_tx(tr, "Passphrase")} <input type=password name=passphrase autofocus
            autocomplete=current-password required{field_error}></label></p>
          <p><button name=action value=approve>{_tx(tr, "Allow")}</button>
             <button name=action value=deny formnovalidate>{_tx(tr, "Deny")}</button></p>
        </form>"""
    if message:
        text = html.escape(message) if message_is_translated else _tx(tr, message)
        note = f"<p id=form-error role=alert style='color:#b00'>{text}</p>"
    else:
        note = ""
    body = _html_shell(
        tr,
        "VibeSynergy: allow access?",
        f"<h1>{_tx(tr, 'Allow access to VibeSynergy?')}</h1>{note}{form}",
        width_em=32,
    )
    return HTMLResponse(body, headers=_html_headers(form=bool(request_id)))


class BearerGuard:
    """ASGI wrapper for the MCP endpoint: requires a valid access token, else 401 with the
    discovery header. Rewrites the path to the inner app's route."""

    def __init__(self, app, provider: VibeSynergyOAuthProvider, inner_path: str) -> None:
        self.app = app
        self.provider = provider
        self.inner_path = inner_path
        self.metadata_url = provider.issuer + "/.well-known/oauth-protected-resource"

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode()
        bearer_attempt = auth.lower().startswith("bearer ")
        token = auth[7:].strip() if bearer_attempt else ""
        access = await self.provider.load_access_token(token) if token else None
        if access is None and token:
            access = await self.provider.load_personal_access_token(token)
        from vibesynergy.mcp.scopes import SCOPES

        advertised = " ".join(SCOPES)
        if access is None:
            error = ', error="invalid_token"' if bearer_attempt else ""
            response = JSONResponse(
                {
                    "error": "invalid_token" if bearer_attempt else "unauthorized",
                    "error_description": "authentication required",
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{self.metadata_url}"'
                    f'{error}, scope="{advertised}"'
                },
            )
            await response(scope, receive, send)
            return
        effective = expand_scopes(access.scopes)
        if not effective:
            response = JSONResponse(
                {"error": "insufficient_scope"},
                status_code=403,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{self.metadata_url}", '
                    f'error="insufficient_scope", scope="{advertised}"'
                },
            )
            await response(scope, receive, send)
            return
        from vibesynergy.mcp.identity import author_for_client, reset_connection, set_connection

        client = access.client_id
        tokens = set_connection(
            client,
            author_for_client(client),
            frozenset(effective),
            self.provider.label_for(client),
        )
        try:
            await self.app(
                {**scope, "path": self.inner_path, "raw_path": self.inner_path.encode()},
                receive,
                send,
            )
        finally:
            reset_connection(tokens)
