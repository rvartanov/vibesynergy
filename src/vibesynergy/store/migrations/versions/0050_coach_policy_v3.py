"""Coach policy version 3: the Garmin Coach boundary is gone.

Claude is the only coach, so the boundary "Preserve Garmin Coach, personal events and
human-written activity text." now reads "Preserve personal events and human-written
activity text.". The active policy stores its effective document, so the coach would keep
reading the old boundary until the next save. For an active policy whose effective document
is not version 3, a new revision with the same payload and the version 3 effective document
is written and activated (actor "migration"); history and the audit trail stay intact.

The effective document and fingerprint repeat ``vibesynergy.query.coach.preview`` for policy
version 3 at the time of writing; a test checks they agree.

Revision ID: 0050
Revises: 0049
"""

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

POLICY_VERSION = "3"
BOUNDARIES = [
    "Use measured evidence and confirmed user context; distinguish uncertainty and inference.",
    "Do not diagnose, invent measurements or infer exercise load from calendar commitments.",
    "Preserve personal events and human-written activity text.",
    "Settings never grant external-write permission or bypass workout approval and ownership.",
    (
        "Private context stays out of public annotations; apply the "
        "shared safety engine before actions."
    ),
]
POLICY_DEFAULTS = {"instructions": "", "language": "auto", "detail": "concise", "tone": "direct"}
REASON = "Removed the Garmin Coach boundary: Claude is the only coach."


def _fingerprint(data) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def effective(payload: dict) -> tuple[dict, dict]:
    """The policy payload and its version 3 effective document."""
    values = {key: payload.get(key, default) for key, default in POLICY_DEFAULTS.items()}
    return values, {**values, "policy_version": POLICY_VERSION, "boundaries": BOUNDARIES}


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def upgrade() -> None:
    conn = op.get_bind()
    heads = conn.execute(
        sa.text(
            "SELECT subject_id, version, active_version FROM coach_head "
            "WHERE kind = 'policy' AND active_version IS NOT NULL"
        )
    ).all()
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    for subject_id, version, active_version in heads:
        row = conn.execute(
            sa.text(
                "SELECT schema_version, payload, effective FROM coach_revision "
                "WHERE subject_id = :s AND kind = 'policy' AND version = :v"
            ),
            {"s": subject_id, "v": active_version},
        ).first()
        if row is None:
            continue
        values, fresh = effective(_load(row.payload) or {})
        if _load(row.effective) == fresh:
            continue
        new_version = version + 1
        conn.execute(
            sa.text(
                "INSERT INTO coach_revision (subject_id, kind, version, schema_version, "
                "payload, effective, fingerprint, created_at, actor, reason) VALUES "
                "(:s, 'policy', :v, :schema, :payload, :effective, :fp, :at, 'migration', "
                ":reason)"
            ),
            {
                "s": subject_id,
                "v": new_version,
                "schema": row.schema_version,
                "payload": json.dumps(values),
                "effective": json.dumps(fresh),
                "fp": _fingerprint(fresh),
                "at": now,
                "reason": REASON,
            },
        )
        conn.execute(
            sa.text(
                "UPDATE coach_head SET version = :v, active_version = :v "
                "WHERE subject_id = :s AND kind = 'policy'"
            ),
            {"v": new_version, "s": subject_id},
        )
        conn.execute(
            sa.text(
                "INSERT INTO coach_event (subject_id, kind, version, action, created_at, "
                "actor, reason) VALUES (:s, 'policy', :v, 'activate', :at, 'migration', "
                ":reason)"
            ),
            {"s": subject_id, "v": new_version, "at": now, "reason": REASON},
        )


def downgrade() -> None:
    # The new revision stays; the previous one is still in the history to restore.
    pass
