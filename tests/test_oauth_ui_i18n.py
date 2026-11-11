"""OAuth approval UI: Dutch/English via Accept-Language."""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qs, urlparse

import pytest
from test_oauth import (
    PASSPHRASE,
    REDIRECT_CUSTOM,
    _assert_no_timed_handoff,
    _authorize,
    _pkce,
    _register,
    _request_id,
)

pytest_plugins = ["test_oauth"]

NL = {"Accept-Language": "nl-NL,nl;q=0.9,en;q=0.8"}
EN = {"Accept-Language": "en-US,en;q=0.9"}
_CODE_INPUT = re.compile(r'<label>(?:Code|Autorisatiecode)<br><input readonly[^>]*value="([^"]+)"')
_CALLBACK_INPUT = re.compile(
    r'<label>(?:Callback URL|Callback-URL)<br><input readonly[^>]*value="([^"]+)"'
)
_MAIN_COUNT = re.compile(r"<main>", re.IGNORECASE)


def _assert_single_main(html: str) -> None:
    assert len(_MAIN_COUNT.findall(html)) == 1


def test_consent_page_dutch_has_lang_and_translations(setup) -> None:
    _, client = setup
    client_id = _register(client, name="Mijn app")
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.get(f"/vibesynergy/approve?request={request_id}", headers=NL)
    assert reply.status_code == 200
    _assert_single_main(reply.text)
    assert '<html lang="nl">' in reply.text
    assert "VibeSynergy-toegang toestaan?" in reply.text
    assert "Wachtwoordzin" in reply.text
    assert "Toestaan" in reply.text and "Weigeren" in reply.text
    assert "wil toegang tot je VibeSynergy-account" in reply.text
    assert "Mijn training en gezondheid bekijken" in reply.text


def test_consent_page_follows_appearance_cookie_over_accept_language(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    client.cookies.set("vibesynergy-language", "nl")
    reply = client.get(f"/vibesynergy/approve?request={request_id}", headers=EN)
    assert reply.status_code == 200
    assert '<html lang="nl">' in reply.text
    assert "VibeSynergy-toegang toestaan?" in reply.text


def test_consent_page_english_has_lang(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.get(f"/vibesynergy/approve?request={request_id}", headers=EN)
    assert '<html lang="en">' in reply.text
    assert "Allow access to VibeSynergy?" in reply.text


def test_approve_success_dutch(setup) -> None:
    _, client = setup
    client_id = _register(client, name="Claude")
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=NL,
    )
    assert reply.status_code == 200
    _assert_single_main(reply.text)
    assert '<html lang="nl">' in reply.text
    assert "Goedgekeurd" in reply.text
    assert "Doorgaan naar" in reply.text
    assert _CODE_INPUT.search(reply.text) is not None


def test_deny_dutch(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "action": "deny"},
        headers=NL,
    )
    assert reply.status_code == 200
    _assert_single_main(reply.text)
    assert '<html lang="nl">' in reply.text
    assert "Toegang geweigerd" in reply.text
    assert "Terug naar" in reply.text
    callback = html_lib.unescape(_CALLBACK_INPUT.search(reply.text).group(1))
    assert parse_qs(urlparse(callback).query)["error"] == ["access_denied"]


def test_wrong_passphrase_dutch(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": "fout"},
        headers=NL,
    )
    assert reply.status_code == 200
    assert "Verkeerde wachtwoordzin" in reply.text
    assert "pogingen over" in reply.text


def test_client_name_escaped_in_dutch_consent(setup) -> None:
    _, client = setup
    client_id = _register(client, name="<img src=x onerror=alert(1)>")
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.get(f"/vibesynergy/approve?request={request_id}", headers=NL)
    assert "&lt;img" in reply.text and "<img" not in reply.text


def test_client_name_escaped_in_approval_and_denial(setup) -> None:
    _, client = setup
    evil = "<script>alert(1)</script>"
    client_id = _register(client, name=evil)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    approved = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=EN,
    )
    assert approved.status_code == 200
    assert "&lt;script" in approved.text and "<script" not in approved.text
    _, challenge2 = _pkce()
    request_id2 = _request_id(_authorize(client, client_id, challenge2))
    denied = client.post(
        "/vibesynergy/approve",
        data={"request": request_id2, "action": "deny"},
        headers=EN,
    )
    assert denied.status_code == 200
    assert "&lt;script" in denied.text and "<script" not in denied.text


def test_expired_request_page_dutch_and_english(setup) -> None:
    _, client = setup
    nl_reply = client.get("/vibesynergy/approve?request=not-a-real-request-id", headers=NL)
    assert nl_reply.status_code == 200
    assert '<html lang="nl">' in nl_reply.text
    assert "Dit goedkeuringsverzoek is verlopen" in nl_reply.text
    en_reply = client.get("/vibesynergy/approve?request=not-a-real-request-id", headers=EN)
    assert en_reply.status_code == 200
    assert '<html lang="en">' in en_reply.text
    assert "This approval request has expired" in en_reply.text


def test_wrong_passphrase_english(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": "wrong"},
        headers=EN,
    )
    assert reply.status_code == 200
    assert '<html lang="en">' in reply.text
    assert "Wrong passphrase" in reply.text
    assert "attempts left" in reply.text


def test_deny_expired_request_dutch(setup) -> None:
    _, client = setup
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": "not-a-real-request-id", "action": "deny"},
        headers=NL,
    )
    assert reply.status_code == 200
    assert '<html lang="nl">' in reply.text
    assert "Verzoek verlopen" in reply.text


def test_already_approved_dutch_hides_credentials(setup) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    first = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=NL,
    )
    code = _CODE_INPUT.search(first.text).group(1)
    callback = html_lib.unescape(_CALLBACK_INPUT.search(first.text).group(1))
    again = client.get(f"/vibesynergy/approve?request={request_id}", headers=NL)
    assert again.status_code == 200
    _assert_single_main(again.text)
    assert '<html lang="nl">' in again.text
    assert "Al toegestaan" in again.text
    assert code not in again.text
    assert callback not in again.text
    assert _CODE_INPUT.search(again.text) is None
    assert "Doorgaan naar" not in again.text


# The app schemes MCP clients (editors, desktop apps) register; none may start a timed handoff.
APP_SCHEME_REDIRECTS = [
    f"{scheme}://vibesynergy/oauth/callback"
    for scheme in ("cursor", "vscode", "vscode-insiders", "claude", "zed", "windsurf")
]
APP_SCHEME_COPY = {
    "en": (
        "Select the link above to go back to your app.",
        "copy this URL",
        "paste the code into the app",
        "Continue to ",
        "Callback URL",
    ),
    "nl": (
        "Kies de link hierboven om terug te gaan naar je app.",
        "kopieer dan deze URL",
        "plak de code in de app",
        "Doorgaan naar ",
        "Callback-URL",
    ),
}


@pytest.mark.parametrize("redirect_uri", APP_SCHEME_REDIRECTS)
@pytest.mark.parametrize(("lang", "headers"), [("en", EN), ("nl", NL)])
def test_app_scheme_success_has_manual_handoff_and_no_delayed_refresh(
    setup, redirect_uri: str, lang: str, headers: dict[str, str]
) -> None:
    _, client = setup
    client_id = _register(client, name="Synthetic app", redirect_uri=redirect_uri)
    verifier, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, redirect_uri))
    reply = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=headers,
    )
    assert reply.status_code == 200
    _assert_single_main(reply.text)
    assert f'<html lang="{lang}">' in reply.text
    _assert_no_timed_handoff(reply)
    for phrase in APP_SCHEME_COPY[lang]:
        assert phrase in reply.text, phrase
    assert "automatic" not in reply.text.lower() and "automatisch" not in reply.text.lower()
    callback = html_lib.unescape(_CALLBACK_INPUT.search(reply.text).group(1))
    code = _CODE_INPUT.search(reply.text).group(1)
    assert callback.startswith(redirect_uri) and code in callback
    # The visible link, the copyable URL and the code all stay available for copy/recovery.
    assert f'<a href="{html_lib.escape(callback, quote=True)}">' in reply.text
    assert "default-src 'none'" in reply.headers["content-security-policy"]
    assert reply.headers["cache-control"] == "no-store"
    token = client.post(
        "/vibesynergy/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text


@pytest.mark.parametrize(("lang", "headers"), [("en", EN), ("nl", NL)])
def test_app_scheme_repeat_and_deny_stay_credential_free_without_refresh(
    setup, lang: str, headers: dict[str, str]
) -> None:
    _, client = setup
    client_id = _register(client, redirect_uri=REDIRECT_CUSTOM)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge, REDIRECT_CUSTOM))
    first = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=headers,
    )
    code = _CODE_INPUT.search(first.text).group(1)
    again = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": PASSPHRASE, "action": "approve"},
        headers=headers,
    )
    _assert_no_timed_handoff(again)
    assert code not in again.text and "<a href" not in again.text
    _, challenge2 = _pkce()
    request_id2 = _request_id(_authorize(client, client_id, challenge2, REDIRECT_CUSTOM))
    denied = client.post(
        "/vibesynergy/approve", data={"request": request_id2, "action": "deny"}, headers=headers
    )
    _assert_no_timed_handoff(denied)
    assert "automatic" not in denied.text.lower() and "automatisch" not in denied.text.lower()


@pytest.mark.parametrize(
    ("headers", "message"), [(EN, "Wrong passphrase"), (NL, "Verkeerde wachtwoordzin")]
)
def test_wrong_passphrase_is_announced_and_linked_to_the_field(
    setup, headers: dict[str, str], message: str
) -> None:
    _, client = setup
    client_id = _register(client)
    _, challenge = _pkce()
    request_id = _request_id(_authorize(client, client_id, challenge))
    retry = client.post(
        "/vibesynergy/approve",
        data={"request": request_id, "passphrase": "synthetic wrong"},
        headers=headers,
    )
    assert re.search(rf"<p id=form-error role=alert[^>]*>{message}", retry.text)
    field = re.search(r"<input type=password[^>]*>", retry.text).group(0)
    assert "aria-invalid=true" in field and "aria-describedby=form-error" in field
    # A fresh consent page has no error, so nothing to announce or mark invalid.
    fresh = client.get(f"/vibesynergy/approve?request={request_id}", headers=headers)
    assert "role=alert" not in fresh.text and "aria-invalid" not in fresh.text
