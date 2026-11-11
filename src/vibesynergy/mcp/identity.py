"""Resolved MCP client identity for server-side coach author attribution."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from vibesynergy.mcp.scopes import SCOPES

# Historical authors stay valid on old rows. New app writes use ``client:<id>``.
LOCAL_CLIENTS = frozenset({"athlete", "portal", "user", "stdio"})

# The AI coach's author on rows written before per-app identities. New rows never get it.
AI_COACH_AUTHOR = "ai_coach"

_CLIENT: ContextVar[str] = ContextVar("mcp_client", default="shared")
_AUTHOR: ContextVar[str] = ContextVar("mcp_author", default="user")
# None means a local caller (stdio, tests): every scope. A token sets an explicit set.
_SCOPES: ContextVar[frozenset[str] | None] = ContextVar("mcp_scopes", default=None)
_LABEL: ContextVar[str] = ContextVar("mcp_label", default="")


@dataclass(frozen=True)
class McpConnection:
    client: str
    author: str
    label: str = ""


def author_for_client(client: str) -> str:
    """Author stored on new coach rows.

    The athlete's own portal, CLI and local stdio stay ``user``. Every app, personal
    access token (``pat:<id>``) and path client gets ``client:<id>``. Old ``legacy`` and
    AI-coach rows are left as they are; this function does not return them.
    """
    if client in LOCAL_CLIENTS:
        return "user"
    return f"client:{client}"


def read_author(author: str | None) -> str | None:
    """The author as readers see it."""
    return author


def set_connection(
    client: str,
    author: str | None = None,
    scopes: frozenset[str] | None = None,
    label: str = "",
) -> tuple[object, object, object, object]:
    resolved = author if author is not None else author_for_client(client)
    return (
        _CLIENT.set(client),
        _AUTHOR.set(resolved),
        _SCOPES.set(scopes),
        _LABEL.set(label or ""),
    )


def reset_connection(tokens: tuple[object, ...]) -> None:
    _CLIENT.reset(tokens[0])
    _AUTHOR.reset(tokens[1])
    if len(tokens) > 2:
        _SCOPES.reset(tokens[2])
    if len(tokens) > 3:
        _LABEL.reset(tokens[3])


def current_connection() -> McpConnection:
    return McpConnection(client=_CLIENT.get(), author=_AUTHOR.get(), label=_LABEL.get())


def current_author() -> str:
    return _AUTHOR.get()


def current_label() -> str:
    return _LABEL.get()


def current_scopes() -> frozenset[str]:
    value = _SCOPES.get()
    if value is None:
        return frozenset(SCOPES)
    return value


def note_source() -> str:
    """Diary ``source`` for a note this connection writes.

    The old hardcoded ``claude`` is only used when the app's name is actually Claude.
    """
    label = _LABEL.get().strip()
    if label.casefold() == "claude":
        return "claude"
    return _AUTHOR.get()
