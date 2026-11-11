"""CalDAV client: read-only collection snapshots, and the PROPFIND calls used to discover
an account's calendars. There is no PUT/DELETE/MKCALENDAR here: reading never grants write
authority. Credentials are Basic (app-specific password) or a Bearer token (Google OAuth)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from defusedxml import ElementTree as ET

from vibesynergy.sources.http_timeout import DEFAULT_HTTP_TIMEOUT_SECONDS, requests_timeout_pair

DAV = "{DAV:}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
REPORT = b"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
<d:prop><d:getetag/><c:calendar-data/></d:prop>
<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT"/></c:comp-filter></c:filter>
</c:calendar-query>"""
log = logging.getLogger(__name__)
TRUSTED_DOMAINS = ("icloud.com", "fastmail.com", "googleusercontent.com")


class IncrementalSnapshotInvalid(Exception):
    """Multiget did not return the requested objects; caller should do a full snapshot."""


ALLOW_LOCAL_HTTP = False  # Tests monkeypatch this in a throwaway, in-process portal only.


def calendar_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        (
            parts.scheme != "https"
            and not (
                ALLOW_LOCAL_HTTP
                and parts.scheme == "http"
                and parts.hostname in ("localhost", "127.0.0.1")
            )
        )
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not parts.path.endswith("/")
    ):
        raise ValueError(
            "Calendar URL must be an HTTPS collection URL ending in /, without credentials"
        )
    if any(p in (".", "..") for p in unquote(parts.path).split("/")):
        raise ValueError("Calendar URL contains a relative path")
    return url


def object_url(base: str, href: str, *, response_url: str | None = None) -> str:
    parent = urlsplit(calendar_url(base))
    raw = urlsplit(href)
    result = urljoin(response_url or base, href)
    child = urlsplit(result)
    host = (child.hostname or "").lower()
    base_host = (parent.hostname or "").lower()
    domain_ok = host == base_host or any(
        (host == domain or host.endswith("." + domain))
        and (base_host == domain or base_host.endswith("." + domain))
        for domain in TRUSTED_DOMAINS
    )
    path, prefix = unquote(child.path), unquote(parent.path)
    suffix = path[len(prefix) :] if path.startswith(prefix) else ""
    if (
        raw.username
        or raw.password
        or child.scheme != parent.scheme
        or not domain_ok
        or raw.query
        or raw.fragment
        or child.query
        or child.fragment
        or not path.startswith(prefix)
        or not suffix
        or "/" in suffix
        or suffix in (".", "..")
        or any(segment in (".", "..") for segment in path.split("/"))
    ):
        raise ValueError("CalDAV response contains an object outside the selected collection")
    return urlunsplit((parent.scheme, parent.netloc, quote(path, safe="/"), "", ""))


class CalDAVClient:
    """``bearer`` is a zero-argument callable returning a fresh access token; it replaces
    the username/password pair."""

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        *,
        bearer: Callable[[], str] | None = None,
        http=None,
        request_timeout: float | tuple[float, float] | None = None,
    ) -> None:
        self._auth = (username, password) if bearer is None else None
        self._bearer = bearer
        self._http = http or requests.Session()
        self._request_timeout = (
            request_timeout
            if request_timeout is not None
            else requests_timeout_pair(DEFAULT_HTTP_TIMEOUT_SECONDS)
        )

    def request_args(self) -> dict:
        """The authentication part of a request; never log or echo this."""
        if self._bearer is not None:
            return {"headers": {"Authorization": f"Bearer {self._bearer()}"}}
        return {"auth": self._auth}

    def snapshot(self, url: str) -> list[dict[str, str]]:
        return self._report(url, REPORT)

    def incremental_snapshot(self, url: str, previous: list[dict]) -> list[dict[str, str]]:
        """Reconcile a complete ETag inventory, downloading only new/changed objects."""
        inventory_body = REPORT.replace(b"<c:calendar-data/>", b"")
        inventory = self._report(url, inventory_body, require_data=False)
        old = {obj["url"]: obj for obj in previous}
        expected = {obj["url"]: obj["etag"] for obj in inventory}
        changed = [href for href, etag in expected.items() if old.get(href, {}).get("etag") != etag]
        result = {href: old[href] for href in expected if href not in changed}
        for offset in range(0, len(changed), 100):
            batch = changed[offset : offset + 100]
            body = (
                '<c:calendar-multiget xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                "<d:prop><d:getetag/><c:calendar-data/></d:prop>"
                + "".join(f"<d:href>{escape(href)}</d:href>" for href in batch)
                + "</c:calendar-multiget>"
            ).encode()
            fetched = self._report(url, body, tolerate_non_2xx=True)
            if {obj["url"] for obj in fetched} != set(batch) or any(
                obj["etag"] != expected[obj["url"]] for obj in fetched
            ):
                raise IncrementalSnapshotInvalid()
            result.update((obj["url"], obj) for obj in fetched)
        if changed:
            after = self._report(url, inventory_body, require_data=False)
            if {obj["url"]: obj["etag"] for obj in after} != expected:
                raise IncrementalSnapshotInvalid()
        return [result[href] for href in sorted(result)]

    def _report(
        self,
        url: str,
        body: bytes,
        *,
        require_data: bool = True,
        tolerate_non_2xx: bool = False,
    ) -> list[dict[str, str]]:
        base = calendar_url(url)
        auth = self.request_args()
        try:
            response = self._http.request(
                "REPORT",
                base,
                data=body,
                auth=auth.get("auth"),
                headers={
                    "Depth": "1",
                    "Content-Type": "application/xml; charset=utf-8",
                    **auth.get("headers", {}),
                },
                timeout=self._request_timeout,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RuntimeError("CalDAV calendar read failed; previous snapshot retained") from None
        if response.status_code != 207:
            raise RuntimeError(f"CalDAV calendar read returned HTTP {response.status_code}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            raise ValueError("Invalid CalDAV XML response") from None
        if root.tag != DAV + "multistatus" or root.find(DAV + "error") is not None:
            raise ValueError("Incomplete CalDAV multistatus response")
        effective_url = getattr(response, "url", None) or base
        collection = urlsplit(base)
        objects = {}
        skipped = 0
        for row in root.findall(DAV + "response"):
            href = row.findtext(DAV + "href")
            if not href:
                raise ValueError("CalDAV object has no href")
            resolved = urlsplit(urljoin(effective_url, href))
            if (
                resolved.scheme == collection.scheme
                and resolved.netloc == collection.netloc
                and not resolved.query
                and not resolved.fragment
                and unquote(resolved.path).rstrip("/") == unquote(collection.path).rstrip("/")
            ):
                continue  # REPORT may include the collection itself without calendar data.
            if row.find(".//" + DAV + "error") is not None:
                raise ValueError("Incomplete CalDAV multistatus response")
            try:
                absolute = object_url(base, href, response_url=effective_url)
            except ValueError:
                skipped += 1
                continue
            status_el = row.find(DAV + "status")
            if status_el is not None:
                parts = (status_el.text or "").split()
                code = parts[1] if len(parts) > 1 else ""
                if code != "200":
                    if tolerate_non_2xx:
                        log.info(
                            "Skipped CalDAV object response with status %s", status_el.text or ""
                        )
                        continue
                    raise ValueError("Incomplete CalDAV object properties")
            props = {}
            for propstat in row.findall(DAV + "propstat"):
                status = (propstat.findtext(DAV + "status") or "").split()
                if len(status) < 2 or status[1] != "200":
                    raise ValueError("Incomplete CalDAV object properties")
                prop = propstat.find(DAV + "prop")
                if prop is None:
                    raise ValueError("Missing CalDAV properties")
                for value in prop:
                    if value.tag in props:
                        raise ValueError("Duplicate CalDAV property")
                    props[value.tag] = value.text
            etag, data = props.get(DAV + "getetag"), props.get(CAL + "calendar-data")
            if not etag or (require_data and not data):
                raise ValueError("CalDAV object is missing ETag or calendar data")
            if absolute in objects:
                if objects[absolute]["etag"] == etag:
                    continue
                log.warning("Duplicate CalDAV href with differing ETag; keeping the last response")
            objects[absolute] = {"url": absolute, "etag": etag, "data": data or ""}
        if skipped:
            log.warning("Skipped %d CalDAV objects outside the selected collection", skipped)
        return [objects[key] for key in sorted(objects)]
