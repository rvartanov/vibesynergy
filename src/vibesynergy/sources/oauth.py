"""OAuth2 helpers shared by the Withings and Strava sources.

Login works on a headless server: ``vibesynergy auth <source>`` prints the provider's consent
link, the user opens it on any device and approves, and pastes the address the browser was
sent to (``<public_url>/vibesynergy/<source>/callback?code=…``) back into the terminal. The
page itself does not need to load for this to work.

Stored per source in the secret store: ``app`` (client id and secret, entered once) and
``tokens`` (access and refresh token). Refresh tokens rotate on every refresh at both
providers, so new tokens are saved immediately.
"""

from __future__ import annotations

import getpass
import hashlib
import queue
import re
import secrets as pysecrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from vibesynergy.core.config import Settings
from vibesynergy.core.secrets import SecretStore

APP = "app"
TOKENS = "tokens"


def scope_set(grant: str | None) -> set[str]:
    """Strava returns comma-separated callback scopes and sometimes space-separated tokens."""
    return {part for part in re.split(r"[\s,]+", grant or "") if part}


class AuthError(Exception):
    pass


@dataclass(frozen=True)
class OAuthApp:
    client_id: str
    client_secret: str


def redirect_uri(settings: Settings, source_id: str) -> str:
    if not settings.public_url:
        raise AuthError(
            f"set [server] public_url in {settings.config_path} first "
            "(the https address of this machine, e.g. https://server.example.ts.net)"
        )
    return f"{settings.public_url}/vibesynergy/{source_id}/callback"


def load_app(store: SecretStore, source_id: str) -> OAuthApp:
    app = store.get(APP)
    if not app:
        hint = (
            "connect Strava in Settings → Sources"
            if source_id == "strava"
            else f"run `vibesynergy auth {source_id}`"
        )
        raise AuthError(f"no {source_id} app configured; {hint}")
    return OAuthApp(app["client_id"], app["client_secret"])


def ask_app(
    store: SecretStore,
    label: str,
    *,
    ask: Callable[[str], str] | None = None,
    secret: Callable[[str], str] | None = None,
    numeric_id: bool = False,
) -> OAuthApp:
    """Use the stored app credentials, or ask for them once.

    ``numeric_id``: the provider's client ids are digits only (Strava). Strava reads just
    the leading digits of a client id, so a secret pasted into the id field would silently
    select someone else's app; such input is refused here instead of saved.
    """
    ask = ask or input
    secret = secret or getpass.getpass
    stored = store.get(APP)
    if stored and (not numeric_id or str(stored["client_id"]).isdigit()):
        answer = ask(f"Use the stored {label} app ({stored['client_id']})? [Y/n] ")
        if answer.strip().lower() in ("", "y", "yes"):
            return OAuthApp(stored["client_id"], stored["client_secret"])
    client_id = ask(f"{label} Client ID: ").strip()
    if numeric_id and not client_id.isdigit():
        raise AuthError(
            f"a {label} Client ID is a number (e.g. 123456); this looks like something else, "
            "perhaps the Client Secret. Nothing was saved."
        )
    client_secret = secret(f"{label} Client Secret (input hidden): ").strip()
    if not client_id or not client_secret:
        raise AuthError("client id and secret are required")
    if client_secret == client_id:
        raise AuthError("the Client Secret is the same as the Client ID; nothing was saved")
    store.set(APP, {"client_id": client_id, "client_secret": client_secret})
    return OAuthApp(client_id, client_secret)


HANDOFF = "login"  # secret-store folder where the service hands a login's code to the CLI
HANDOFF_TTL_S = 15 * 60

PUBLICATION_OAUTH = "publication_oauth"
PUBLICATION_OAUTH_TTL_S = 15 * 60
STRAVA_PORTAL_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{40,60}$")


def publication_oauth_name(state: str) -> str:
    return "pending-" + hashlib.sha256(state.encode()).hexdigest()[:32]


def store_publication_oauth_pending(
    store: SecretStore, state: str, *, source: str, portal: str, created: float | None = None
) -> None:
    store.set(
        publication_oauth_name(state),
        {
            "source": source,
            "created": created if created is not None else time.time(),
            "portal": portal,
        },
    )


def take_publication_oauth_pending(store: SecretStore, state: str) -> dict[str, Any] | None:
    name = publication_oauth_name(state)
    pending = store.get(name)
    if not pending:
        return None
    store.delete(name)
    return pending


def strava_portal_oauth_result(
    pending: dict[str, Any],
    *,
    code: str | None,
    error: str | None,
    scope: str | None,
    now: float,
) -> str:
    if error:
        return "denied"
    if now - pending.get("created", 0) > PUBLICATION_OAUTH_TTL_S:
        return "expired"
    if not code:
        return "failed"
    if "activity:write" not in scope_set(scope):
        return "write_missing"
    return "connected"


def strava_portal_return_url(portal: str, result: str) -> str:
    return f"{portal}#/settings/sources?strava={result}"


def _handoff_name(state: str) -> str:
    return "pending-" + hashlib.sha256(state.encode()).hexdigest()[:32]


def begin_handoff(store: SecretStore, state: str) -> None:
    """Mark a login as waiting, so the service's callback page may hand its code over."""
    store.set(_handoff_name(state), {"created": time.time()})


def complete_handoff(
    store: SecretStore, state: str, *, code: str | None, error: str | None = None
) -> bool:
    """Called by the callback page: store the code for a waiting login. False when no login
    with this state is waiting (then the page asks for paste-back instead)."""
    name = _handoff_name(state)
    pending = store.get(name)
    if not pending or time.time() - pending.get("created", 0) > HANDOFF_TTL_S:
        return False
    store.set(name, {**pending, "code": code, "error": error})
    return True


def take_handoff(store: SecretStore, state: str) -> dict[str, Any] | None:
    """The handed-over result, once (it is removed when taken)."""
    name = _handoff_name(state)
    pending = store.get(name)
    if pending and ("code" in pending or "error" in pending):
        store.delete(name)
        return pending
    return None


def paste_back_code(
    authorize_url: str,
    params: dict[str, str],
    *,
    ask: Callable[[str], str] | None = None,
    handoff: SecretStore | None = None,
    poll_s: float = 1.0,
    timeout_s: float = HANDOFF_TTL_S,
) -> str:
    """Show the consent link; return the ``code``. With ``handoff`` the service's callback
    page passes it on by itself once the browser comes back; pasting the address the browser
    went to still works (e.g. when that page can't be reached)."""
    ask = ask or input
    state = pysecrets.token_urlsafe(16)
    link = f"{authorize_url}?{urlencode({**params, 'state': state})}"
    print("\n1. Open this link in a browser and approve access:\n")
    print(f"   {link}\n")
    if handoff is None:
        print("2. The browser then goes to a …/callback?code=… address (it may show an error page;")
        print("   that is fine). Copy that whole address from the address bar.\n")
        return _code_from(ask("3. Paste it here: ").strip(), authorize_url, state)

    begin_handoff(handoff, state)
    print("2. After you approve, VibeSynergy finishes the login by itself (the page says so).")
    print("   If that page doesn't open, copy the …/callback?code=… address from the browser's")
    print("   address bar and paste it here.\n")
    answers: queue.Queue[str] = queue.Queue()
    threading.Thread(
        target=lambda: answers.put(ask("Waiting… (or paste the address): ")), daemon=True
    ).start()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            result = take_handoff(handoff, state)
            if result is not None:
                if result.get("error"):
                    raise AuthError(f"access was not granted: {result['error']}")
                if not result.get("code"):
                    raise AuthError("the browser came back without a code; try again")
                print("\nApproved in the browser.")
                return str(result["code"])
            try:
                return _code_from(answers.get(timeout=poll_s).strip(), authorize_url, state)
            except queue.Empty:
                continue
        raise AuthError("no approval within 15 minutes; run the login again")
    finally:
        handoff.delete(_handoff_name(state))


def _code_from(pasted: str, authorize_url: str, state: str) -> str:
    if urlparse(pasted).netloc and urlparse(pasted).netloc == urlparse(authorize_url).netloc:
        raise AuthError(
            "that is the consent link itself. Open it, click Allow/Authorize, and paste the "
            "address the browser goes to afterwards (it contains /callback?code=)."
        )
    query = parse_qs(urlparse(pasted).query)
    if query.get("error"):
        raise AuthError(f"access was not granted: {query['error'][0]}")
    if query.get("state", [None])[0] != state:
        raise AuthError("the pasted address does not belong to this login attempt; try again")
    code = query.get("code", [None])[0]
    if not code:
        raise AuthError("no code found in the pasted address")
    return code


def save_tokens(store: SecretStore, tokens: dict[str, Any]) -> dict[str, Any]:
    """Store tokens with an absolute expiry time."""
    record = dict(tokens)
    if "expires_at" not in record and "expires_in" in record:
        record["expires_at"] = int(time.time()) + int(record["expires_in"])
    store.set(TOKENS, record)
    return record


def needs_refresh(tokens: dict[str, Any], margin: int = 120) -> bool:
    return int(tokens.get("expires_at", 0)) - margin <= time.time()
