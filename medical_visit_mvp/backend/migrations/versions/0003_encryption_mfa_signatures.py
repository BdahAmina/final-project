"""Encryption at rest, multi-factor authentication and cryptographic (Ed25519) signatures.

- users: MFA secret (encrypted), MFA flag and last used time step; wrapped signing key.
- signing_keys: every public signing key a user has had (old signatures stay verifiable).
- approvals: signed manifest, signature, key fingerprint, MFA flag, time source and clock offset.
- Data: every clinical column and personal name written before this revision is encrypted in place with the
  data key (backend/crypto.py). backend/db.py writes a backup copy of the database before this runs.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18
"""
from alembic import op
import sqlalchemy as sa
from backend import crypto

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# table -> (primary key, columns holding clinical content or personal names)
ENCRYPTED = {
    "users": ("id", ["display"]),
    "transcript_versions": ("id", ["notices"]),
    "transcript_segments": ("id", ["text", "words"]),
    "summary_versions": ("id", ["content"]),
    "patient_context_versions": ("id", ["content"]),
    "plan_versions": ("id", ["content"]),
    "assistant_messages": ("id", ["content"]),
    "assistant_suggestions": ("id", ["text", "rationale", "citations", "medication", "safety", "final_text"]),
}


def encrypt_existing(conn):
    for table, (pk, columns) in ENCRYPTED.items():
        rows = conn.execute(sa.text(f'SELECT "{pk}", {", ".join(columns)} FROM "{table}"')).all()
        for row in rows:
            values = {c: row[i + 1] for i, c in enumerate(columns)}
            changed = {c: crypto.encrypt(v if isinstance(v, str) else str(v)) for c, v in values.items()
                       if v is not None and not crypto.is_encrypted(v)}
            if changed:
                sets = ", ".join(f"{c} = :{c}" for c in changed)
                conn.execute(sa.text(f'UPDATE "{table}" SET {sets} WHERE "{pk}" = :pk'), changed | {"pk": row[0]})


def upgrade():
    with op.batch_alter_table("users") as b:
        b.add_column(sa.Column("mfa_secret", sa.Text))
        b.add_column(sa.Column("mfa_enabled", sa.Boolean, nullable=False, server_default=sa.false()))
        b.add_column(sa.Column("mfa_last_step", sa.Integer))
        b.add_column(sa.Column("sign_key_wrapped", sa.Text))
    op.create_table(
        "signing_keys",
        sa.Column("fingerprint", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("public_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("retired_at", sa.Float),
        sa.Column("retired_reason", sa.String(40)),
    )
    with op.batch_alter_table("approvals") as b:
        b.add_column(sa.Column("manifest", sa.Text))
        b.add_column(sa.Column("signature", sa.String(128)))
        b.add_column(sa.Column("key_fingerprint", sa.String(32), sa.ForeignKey("signing_keys.fingerprint",
                                                                               name="fk_approvals_key_fingerprint")))
        b.add_column(sa.Column("mfa_verified", sa.Boolean))
        b.add_column(sa.Column("time_source", sa.String(80)))
        b.add_column(sa.Column("clock_offset", sa.Float))
    encrypt_existing(op.get_bind())


def downgrade():
    raise NotImplementedError("Encrypted data is not decrypted back; restore the pre-migration backup instead.")
