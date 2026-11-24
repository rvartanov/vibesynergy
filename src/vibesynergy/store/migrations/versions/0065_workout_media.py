"""Photos of a workout, kept in VibeSynergy (#114, phase 6).

- ``media_item``: one photo of a workout. The original and the two sizes the portal shows
  (``display``, ``thumb``: re-encoded, without the camera's location data) are blobs in the
  content-addressed blob store; a row is never deleted, removing it in VibeSynergy only sets
  ``removed_at`` (restore clears it).
- ``media_origin``: a photo taken in from Strava or Garmin, by the platform's own photo id,
  so it is fetched once and never comes back after you removed it.
- ``media_change``: every change of a workout's photos (added, also from another place,
  removed, restored): only ever added, like ``gear_change``.
- ``media_check``: which Strava and Garmin copies were looked at for photos, and with which
  stored version of the activity, so an unchanged copy is not read again.

New tables only: no row changes.

Revision ID: 0065
Revises: 0064
"""

import sqlalchemy as sa
from alembic import op

from vibesynergy.store.types import UTCDateTime

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_item",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("content_type", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("phash", sa.String(), nullable=False),
        sa.Column("display_sha256", sa.String(), nullable=False),
        sa.Column("thumb_sha256", sa.String(), nullable=False),
        sa.Column("caption", sa.String()),
        sa.Column("taken_at", UTCDateTime()),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("author", sa.String()),
        sa.Column("added_at", UTCDateTime(), nullable=False),
        sa.Column("removed_at", UTCDateTime()),
        sa.PrimaryKeyConstraint("id", name="pk_media_item"),
    )
    op.create_index("ix_media_item_group_id", "media_item", ["group_id"])
    op.create_table(
        "media_origin",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("photo_id", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("media_id", sa.String(), nullable=False),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("source", "photo_id", name="pk_media_origin"),
    )
    op.create_index("ix_media_origin_media_id", "media_origin", ["media_id"])
    op.create_table(
        "media_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.String(), nullable=False),
        sa.Column("media_id", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("author", sa.String()),
        sa.Column("at", UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_media_change"),
    )
    op.create_index("ix_media_change_group_id", "media_change", ["group_id"])
    op.create_table(
        "media_check",
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("raw_id", sa.String()),
        sa.Column("photos", sa.Integer(), nullable=False),
        sa.Column("checked_at", UTCDateTime(), nullable=False),
        sa.Column("next_at", UTCDateTime()),
        sa.Column("error", sa.String()),
        sa.PrimaryKeyConstraint("source", "source_id", name="pk_media_check"),
    )


def downgrade() -> None:
    op.drop_table("media_check")
    op.drop_index("ix_media_change_group_id", table_name="media_change")
    op.drop_table("media_change")
    op.drop_index("ix_media_origin_media_id", table_name="media_origin")
    op.drop_table("media_origin")
    op.drop_index("ix_media_item_group_id", table_name="media_item")
    op.drop_table("media_item")
