"""Request-body size limit for routes anyone on the internet can call.

The public app (webhooks, OAuth registration, token and approval) and the portal's
``/auth/*`` routes read request bodies into memory before anyone is known. ``BodyLimit``
answers 413 when ``Content-Length`` is above the cap, and counts the bytes of a body sent
without one (chunked), so a stream cannot grow past the cap either. Routes that
legitimately take bigger bodies get their own, explicit cap by path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULT_LIMIT = 64 * 1024


class _TooLarge(Exception):
    pass


class BodyLimit:
    """ASGI wrapper: ``limit`` bytes per request body, or the cap of the first matching
    pattern in ``larger``. With ``prefixes`` only paths starting with one of them are
    limited; the rest pass untouched."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int = DEFAULT_LIMIT,
        larger: Sequence[tuple[str, int]] = (),
        prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self.app = app
        self.limit = limit
        self.larger = [(re.compile(pattern), cap) for pattern, cap in larger]
        self.prefixes = prefixes

    def __getattr__(self, name: str):
        return getattr(self.app, name)

    def cap(self, path: str) -> int | None:
        if self.prefixes is not None and not path.startswith(self.prefixes):
            return None
        for pattern, cap in self.larger:
            if pattern.fullmatch(path):
                return cap
        return self.limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cap = self.cap(scope.get("path", ""))
        if cap is None:
            await self.app(scope, receive, send)
            return
        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                declared = value
        if declared is not None:
            if not declared.isdigit():
                await _reply(send, 400, b"invalid content-length")
                return
            if int(declared) > cap:
                await _reply(send, 413, b"request body too large")
                return

        received = 0
        exceeded = False
        started = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > cap:
                    exceeded = True
                    raise _TooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if exceeded and not started:
                return  # the 413 below answers instead
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            await _reply(send, 413, b"request body too large")


async def _reply(send: Send, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"connection", b"close"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
