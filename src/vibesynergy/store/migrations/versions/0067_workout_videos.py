"""Videos of a workout (#114): ``media_item`` can hold a video.

- ``kind``: ``photo`` (every row that is there, so nothing changes for them) or ``video``.
- ``duration_s``: a video's length in seconds.
- ``stream_sha256``: the copy a browser plays (without the file's metadata, MP4 with
  faststart), kept uncompressed in the blob store so it can be served in parts. A video's
  ``display_sha256`` and ``thumb_sha256`` are its poster frame in the two sizes.

Three columns only: no row changes.

Revision ID: 0067
Revises: 0066
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("media_item") as batch:
        batch.add_column(sa.Column("kind", sa.String(), nullable=False, server_default="photo"))
        batch.add_column(sa.Column("duration_s", sa.Float(), nullable=True))
        batch.add_column(sa.Column("stream_sha256", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("media_item") as batch:
        batch.drop_column("stream_sha256")
        batch.drop_column("duration_s")
        batch.drop_column("kind")
