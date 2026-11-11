"""Find an account's calendars: principal -> calendar home -> calendar collections.

Only PROPFIND is used. Credentials are sent to the server the user named and, through at
most five HTTPS redirects, to hosts on the same host or inside the provider's trusted domain
(iCloud answers from ``pNN-caldav.icloud.com``). A redirect anywhere else is refused, so a
server can't bounce the password to a third party. Errors carry a stable ``code`` that the
portal turns into a localized message; they never contain the credentials.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import requests
from defusedxml import ElementTree as ET

from vibesynergy.sources.caldav import client as caldav_client
from vibesynergy.sources.caldav.client import CAL, DAV, CalDAVClient, calendar_url

APPLE = "{http://apple.com/ns/ical/}"
MAX_REDIRECTS = 5
MAX_CALENDARS = 100

# Sign-in presets. ``trusted`` is a domain whose subdomains may receive the credentials.
PRESETS: dict[str, dict] = {
    "icloud": {
        "name": "iCloud",
        "url": "https://caldav.icloud.com/",
        "trusted": "icloud.com",
        "kind": "password",
    },
    "fastmail": {
        "name": "Fastmail",
        "url": "https://caldav.fastmail.com/",
        "trusted": "fastmail.com",
        "kind": "password",
    },
    "other": {"name": "Nextcloud / other", "url": None, "trusted": None, "kind": "password"},
    "google": {
        "name": "Google",
        "url": "https://apidata.googleusercontent.com/caldav/v2/",
        "trusted": "googleusercontent.com",
        "kind": "oauth",
    },
}


class CalDAVError(ValueError):
    """``code``: auth_failed, not_caldav, network, insecure_url, bad_url, redirect,
    reauth, no_google_app, google_denied, google_failed, state_invalid, scope."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def host_trusted(url: str, origin_host: str, trusted: str | None) -> bool:
    parts = urlsplit(url)
    local_http = (
        caldav_client.ALLOW_LOCAL_HTTP
        and parts.scheme == "http"
        and parts.hostname in ("localhost", "127.0.0.1")
    )
    if (
        (parts.scheme != "https" and not local_http)
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        return False
    host = (parts.hostname or "").lower()
    if host == origin_host.lower():
        return True
    return bool(trusted) and (host == trusted or host.endswith("." + trusted))


@dataclass
class Discovery:
    principal: str | None
    home: str
    calendars: list[dict]


PRINCIPAL_BODY = (
    b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/>'
    b"</d:prop></d:propfind>"
)
HOME_BODY = (
    b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
    b"<d:prop><c:calendar-home-set/></d:prop></d:propfind>"
)
LIST_BODY = (
    b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
    b'xmlns:a="http://apple.com/ns/ical/"><d:prop><d:resourcetype/><d:displayname/>'
    b"<a:calendar-color/><c:supported-calendar-component-set/></d:prop></d:propfind>"
)


class _Session:
    def __init__(self, client: CalDAVClient, origin_host: str, trusted: str | None) -> None:
        self.client, self.origin, self.trusted, self.http = (
            client,
            origin_host,
            trusted,
            client._http,
        )

    def propfind(self, url: str, body: bytes, depth: str):
        """(final url, status, xml root or None). Follows redirects under the host policy."""
        for _ in range(MAX_REDIRECTS + 1):
            if not host_trusted(url, self.origin, self.trusted):
                raise CalDAVError("redirect" if self._moved else "insecure_url")
            auth = self.client.request_args()
            try:
                response = self.http.request(
                    "PROPFIND",
                    url,
                    data=body,
                    auth=auth.get("auth"),
                    headers={
                        "Depth": depth,
                        "Content-Type": "application/xml; charset=utf-8",
                        **auth.get("headers", {}),
                    },
                    timeout=self.client._request_timeout,
                    allow_redirects=False,
                )
            except requests.RequestException:
                raise CalDAVError("network") from None
            status = response.status_code
            if status in (301, 302, 303, 307, 308):
                location = (getattr(response, "headers", None) or {}).get("Location")
                if not location:
                    raise CalDAVError("not_caldav")
                url, self._moved = urljoin(url, location), True
                continue
            if status in (401, 403):
                raise CalDAVError("auth_failed")
            root = None
            if status == 207:
                try:
                    root = ET.fromstring(response.content)
                except ET.ParseError:
                    root = None
                if root is not None and root.tag != DAV + "multistatus":
                    root = None
            return url, status, root
        raise CalDAVError("redirect")

    _moved = False


def _href(root, *path: str) -> str | None:
    """Text of the first ``<d:href>`` below the named elements, anywhere in the response."""
    for node in root.iter(path[0]):
        for child in path[1:]:
            node = node.find(child) if node is not None else None
        if node is not None:
            href = node.findtext(DAV + "href")
            if href and href.strip():
                return href.strip()
    return None


def _color(value: str | None) -> str | None:
    match = re.fullmatch(r"#([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", (value or "").strip())
    return "#" + match.group(1).lower() if match else None


def _calendars(root, base: str, origin_host: str, trusted: str | None) -> list[dict]:
    found, seen = [], set()
    for row in root.findall(DAV + "response"):
        href = row.findtext(DAV + "href")
        if not href:
            continue
        for propstat in row.findall(DAV + "propstat"):
            status = (propstat.findtext(DAV + "status") or "").split()
            prop = propstat.find(DAV + "prop")
            if prop is None or (len(status) > 1 and status[1] != "200"):
                continue
            kind = prop.find(DAV + "resourcetype")
            if kind is None or kind.find(CAL + "calendar") is None:
                continue
            components = prop.find(CAL + "supported-calendar-component-set")
            if components is not None and not any(
                c.get("name", "").upper() == "VEVENT" for c in components.findall(CAL + "comp")
            ):
                continue  # a task or journal list
            url = urljoin(base, href)
            if not host_trusted(url, origin_host, trusted):
                continue
            if not url.endswith("/") and not urlsplit(url).query:
                url += "/"
            try:
                url = calendar_url(url)
            except ValueError:
                continue
            if url in seen:
                continue
            seen.add(url)
            name = (prop.findtext(DAV + "displayname") or "").strip()
            found.append(
                {
                    "url": url,
                    "name": name[:120] or url.rstrip("/").rsplit("/", 1)[-1],
                    "color": _color(prop.findtext(APPLE + "calendar-color")),
                }
            )
    return found[:MAX_CALENDARS]


def discover(start_url: str, client: CalDAVClient, trusted: str | None = None) -> Discovery:
    parts = urlsplit(start_url)
    if not host_trusted(start_url, parts.hostname or "", trusted):
        raise CalDAVError("insecure_url" if parts.scheme == "http" else "bad_url")
    session = _Session(client, parts.hostname, trusted)
    origin = f"{parts.scheme}://{parts.netloc}"
    candidates = [start_url]
    well_known = origin + "/.well-known/caldav"
    if parts.path.rstrip("/") != "/.well-known/caldav":
        candidates.append(well_known)

    principal = None
    for candidate in candidates:
        session._moved = False
        url, _status, root = session.propfind(candidate, PRINCIPAL_BODY, "0")
        if root is None:
            continue
        href = _href(root, DAV + "current-user-principal")
        if href:
            principal = urljoin(url, href)
            if not host_trusted(principal, parts.hostname, trusted):
                raise CalDAVError("redirect")
            break
    home = None
    if principal:
        url, _status, root = session.propfind(principal, HOME_BODY, "0")
        href = _href(root, CAL + "calendar-home-set") if root is not None else None
        if href:
            home = urljoin(url, href)
            if not host_trusted(home, parts.hostname, trusted):
                raise CalDAVError("redirect")
    if home is None:
        # Some servers (Google) have no principal discovery: the address itself lists them.
        home = start_url
    session._moved = False
    url, _status, root = session.propfind(home, LIST_BODY, "1")
    if root is None:
        raise CalDAVError("not_caldav")
    return Discovery(principal, url, _calendars(root, url, parts.hostname, trusted))
