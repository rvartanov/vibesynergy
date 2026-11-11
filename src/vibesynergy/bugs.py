"""Bug reports: the bug button opens a new issue on GitHub, in the reporter's own account.

The portal fills in the title and what the page knew (version, page, the error it showed, the
API calls that failed lately, the browser); the reporter reads and edits it on GitHub before
creating the issue. Nothing is sent from this server and no GitHub token is kept here.
"""

from __future__ import annotations

import re
from typing import Any

REPO = "rvartanov/vibesynergy"  # the default; ``[bugs] repo`` points elsewhere (a fork)


def repo(settings: Any) -> str:
    value = str((getattr(settings, "bugs", None) or {}).get("repo") or REPO)
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_-][A-Za-z0-9_.-]*", value):
        raise ValueError("[bugs] repo must look like owner/name")
    return value


def new_issue_url(settings: Any) -> str:
    """GitHub's page for a new issue; the portal adds title, body and the bug label."""
    return f"https://github.com/{repo(settings)}/issues/new"
