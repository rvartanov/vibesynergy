"""Conditional CalDAV writes for owned mirrors.

The read client stays free of PUT and DELETE. This writer is constructed only for a
reconcile that is allowed to change the target collection. Every mutating request carries
an ETag precondition. Response bodies are never returned to logs.
"""

from __future__ import annotations

import requests

from vibesynergy.sources.caldav.client import CalDAVClient


class MirrorWriteError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PreconditionFailed(MirrorWriteError):
    def __init__(self) -> None:
        super().__init__("precondition_failed")


class MirrorTimeout(MirrorWriteError):
    def __init__(self) -> None:
        super().__init__("timeout")


class CalendarWriter:
    def __init__(self, client: CalDAVClient) -> None:
        self._client = client

    def snapshot(self, url: str) -> list[dict[str, str]]:
        try:
            return self._client.snapshot(url)
        except (RuntimeError, ValueError) as exc:
            raise MirrorWriteError("calendar_read_failed") from exc

    def get(self, href: str) -> dict[str, str] | None:
        response = self._request("GET", href, headers={"Accept": "text/calendar"})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MirrorWriteError(f"http_{response.status_code}")
        etag = response.headers.get("ETag")
        if not etag or not response.content:
            raise MirrorWriteError("missing_etag")
        text = response.text
        return {"url": href, "etag": etag, "data": text}

    def put(self, href: str, body: bytes, *, match: str | None) -> str:
        """``match`` None is create (If-None-Match: *). Otherwise If-Match the observed tag."""
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if match is None:
            headers["If-None-Match"] = "*"
        else:
            headers["If-Match"] = match
        response = self._request("PUT", href, data=body, headers=headers)
        if response.status_code == 412:
            raise PreconditionFailed()
        if response.status_code not in (200, 201, 204):
            raise MirrorWriteError(f"http_{response.status_code}")
        etag = response.headers.get("ETag")
        if etag:
            return etag
        fetched = self.get(href)
        if fetched is None:
            raise MirrorWriteError("readback_missing")
        return fetched["etag"]

    def delete(self, href: str, match: str) -> None:
        response = self._request("DELETE", href, headers={"If-Match": match})
        if response.status_code == 412:
            raise PreconditionFailed()
        if response.status_code not in (200, 204, 404):
            raise MirrorWriteError(f"http_{response.status_code}")

    def _request(self, method: str, url: str, **kwargs):
        auth = self._client.request_args()
        headers = {**kwargs.pop("headers", {}), **auth.get("headers", {})}
        try:
            return self._client._http.request(
                method,
                url,
                auth=auth.get("auth"),
                headers=headers,
                timeout=self._client._request_timeout,
                allow_redirects=False,
                **kwargs,
            )
        except requests.RequestException:
            raise MirrorTimeout() from None
