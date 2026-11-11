"""Errors shared by sources and the job worker."""

from __future__ import annotations

from datetime import datetime


class RateLimited(Exception):
    """A source refused a call because of its rate limit.

    ``retry_at`` is when the limit resets (None: unknown). The worker re-queues a job that
    failed only on rate limits for that time, without using up one of its attempts.
    """

    retry_at: datetime | None = None
