from alembic import context

from vibesynergy.store.schema import metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through vibesynergy.store.db (vibesynergy db upgrade)")

context.configure(
    connection=connection,
    target_metadata=metadata,
    render_as_batch=True,  # SQLite cannot ALTER most things in place
    compare_type=True,
)
with context.begin_transaction():
    context.run_migrations()
