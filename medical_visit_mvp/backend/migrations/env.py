"""Alembic environment. Migrations are run programmatically by backend.db.migrate(), which passes
an open connection (foreign-key enforcement off, one transaction per run) via config.attributes."""
from alembic import context
from backend.models import Base

target_metadata = Base.metadata
connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Run migrations with: python -m backend.manage migrate")
context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()
