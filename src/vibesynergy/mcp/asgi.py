"""ASGI helpers for MCP HTTP mounts."""

from __future__ import annotations

from vibesynergy.mcp.identity import author_for_client, reset_connection, set_connection
from vibesynergy.mcp.scopes import SCOPES


class McpIdentityMiddleware:
    """Bind the resolved MCP client and coach author for one HTTP request."""

    def __init__(
        self,
        app,
        client: str,
        author: str | None = None,
        *,
        scopes: frozenset[str] | None = None,
        label: str = "",
    ) -> None:
        self.app = app
        self.client = client
        self.author = author
        self.scopes = scopes
        self.label = label

    async def __call__(self, scope, receive, send) -> None:
        # OAuth BearerGuard sets client/author before this wrapper; do not clobber it.
        if self.client == "oauth":
            await self.app(scope, receive, send)
            return
        # A secret-link mount is not an OAuth token. It keeps every scope, including
        # publish, until the athlete turns the link off. The old ``vibesynergy`` scope on
        # a real token is narrower (everything except publish).
        scopes = self.scopes if self.scopes is not None else frozenset(SCOPES)
        label = self.label or ("" if self.client in {"shared", "oauth"} else self.client)
        tokens = set_connection(
            self.client,
            self.author or author_for_client(self.client),
            scopes,
            label,
        )
        try:
            await self.app(scope, receive, send)
        finally:
            reset_connection(tokens)
