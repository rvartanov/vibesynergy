"""Drop retired coach settings from the active coach documents.

The policy keeps language, detail, tone and the athlete's notes; the context keeps only the
notes to remember. For each active document that still holds retired keys, a new revision
with the cleaned payload is written and activated (actor "migration"), so history and the
audit trail stay intact. Older revisions are left as they were; the code ignores the retired
keys when one of them is restored.

The effective document and fingerprint repeat ``vibesynergy.query.coach.preview`` for policy
version 2 at the time of writing; a test checks they agree.

Revision ID: 0035
Revises: 0034
"""

import hashlib
import json
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

POLICY_VERSION = "2"
BOUNDARIES = [
    "Use measured evidence and confirmed user context; distinguish uncertainty and inference.",
    "Do not diagnose, invent measurements or infer exercise load from calendar commitments.",
    "Preserve Garmin Coach, personal events and human-written activity text.",
    "Settings never grant external-write permission or bypass workout approval and ownership.",
    (
        "Private context stays out of public annotations; apply the "
        "shared safety engine before actions."
    ),
]
POLICY_DEFAULTS = {"instructions": "", "language": "auto", "detail": "concise", "tone": "direct"}
REASON = "Removed coach settings nothing reads any more."


def _fingerprint(data) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def clean(kind: str, payload: dict) -> tuple[dict, dict]:
    """The cleaned payload and its effective document."""
    if kind == "policy":
        values = {key: payload.get(key, default) for key, default in POLICY_DEFAULTS.items()}
        return values, {**values, "policy_version": POLICY_VERSION, "boundaries": BOUNDARIES}
    constraints = list(payload.get("constraints") or [])
    values = {"constraints": constraints}
    return values, {"constraints": [row for row in constraints if row.get("active", True)]}


def _load(value):
    return json.loads(value) if isinstance(value, str) else value


def upgrade() -> None:
    conn = op.get_bind()
    heads = conn.execute(
        sa.text(
            "SELECT subject_id, kind, version, active_version FROM coach_head "
            "WHERE active_version IS NOT NULL"
        )
    ).all()
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    for subject_id, kind, version, active_version in heads:
        if kind not in ("policy", "context"):
            continue
        row = conn.execute(
            sa.text(
                "SELECT schema_version, payload FROM coach_revision "
                "WHERE subject_id = :s AND kind = :k AND version = :v"
            ),
            {"s": subject_id, "k": kind, "v": active_version},
        ).first()
        if row is None:
            continue
        payload = _load(row.payload) or {}
        values, effective = clean(kind, payload)
        if values == payload:
            continue
        new_version = version + 1
        conn.execute(
            sa.text(
                "INSERT INTO coach_revision (subject_id, kind, version, schema_version, "
                "payload, effective, fingerprint, created_at, actor, reason) VALUES "
                "(:s, :k, :v, :schema, :payload, :effective, :fp, :at, 'migration', :reason)"
            ),
            {
                "s": subject_id,
                "k": kind,
                "v": new_version,
                "schema": row.schema_version,
                "payload": json.dumps(values),
                "effective": json.dumps(effective),
                "fp": _fingerprint(effective),
                "at": now,
                "reason": REASON,
            },
        )
        conn.execute(
            sa.text(
                "UPDATE coach_head SET version = :v, active_version = :v "
                "WHERE subject_id = :s AND kind = :k"
            ),
            {"v": new_version, "s": subject_id, "k": kind},
        )
        conn.execute(
            sa.text(
                "INSERT INTO coach_event (subject_id, kind, version, action, created_at, "
                "actor, reason) VALUES (:s, :k, :v, 'activate', :at, 'migration', :reason)"
            ),
            {"s": subject_id, "k": kind, "v": new_version, "at": now, "reason": REASON},
        )


def downgrade() -> None:
    # The cleaned revision stays; the previous one is still in the history to restore.
    pass
