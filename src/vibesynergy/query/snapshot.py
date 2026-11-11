"""Read helpers for one open SQLite snapshot, shared by the coach readers.

Daily coaching (``daily_coach``), training rhythm (``rhythm``) and the activity input
digest (``assessments``) read many query functions inside one transaction so every value
comes from the same moment. These helpers used to be private to ``assessments``.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from vibesynergy.query.evidence import json_default

# Receipt and bookkeeping keys, and generated descriptions: they change without the
# athlete's data changing, so they never count as a changed input.
_BOOKKEEPING = frozenset(
    {
        "raw_item_id",
        "updated_at",
        "received_at",
        "description",
        "etag",
        "last_seen_at",
        "last_success_at",
        "last_received_at",
        "calendar_sync",
        "schedule_sync",
        "calendar_mirror",
        "mirror_state",
        "mirror_revision",
        "done_by",
        "completion_match",
    }
)


class SnapshotReader:
    """Adapt query functions that take an engine to one already-open connection."""

    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connect(self):
        yield self.conn


def json_safe(value):
    """``value`` as plain JSON types (dates as ISO text); NaN is an error."""
    return json.loads(json.dumps(value, default=json_default, allow_nan=False))


def semantic(value):
    """``value`` without bookkeeping keys, at any depth."""
    if isinstance(value, list):
        return [semantic(v) for v in value]
    if isinstance(value, dict):
        return {k: semantic(v) for k, v in value.items() if k not in _BOOKKEEPING}
    return value
