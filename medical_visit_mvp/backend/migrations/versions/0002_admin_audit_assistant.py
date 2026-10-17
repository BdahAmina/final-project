"""Admin role, lockout and session fields, care assignments, e-signature fields on approvals,
patient context, doctor plan, clinical assistant tables and the append-only audit trail.

Existing rows are kept: users are copied into the rebuilt users table, care assignments are
created for every existing doctor/patient pair that already has a visit, and existing approvals
get their transcript version back-filled. Legacy approvals keep empty signature fields.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16
"""
import time
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    # users: the role CHECK constraint changes, which SQLite can only do by rebuilding the table.
    # Foreign-key enforcement is off for the migration connection (see backend/db.py).
    op.execute("""
        CREATE TABLE users_new (
            id VARCHAR(40) NOT NULL PRIMARY KEY,
            role VARCHAR(10) NOT NULL,
            display VARCHAR(80) NOT NULL,
            password_hash VARCHAR(200) NOT NULL,
            active BOOLEAN NOT NULL,
            created_by VARCHAR(40) REFERENCES users (id),
            created_at FLOAT NOT NULL,
            failed_logins INTEGER NOT NULL DEFAULT 0,
            locked_until FLOAT,
            password_changed_at FLOAT,
            must_change_password BOOLEAN NOT NULL DEFAULT 0,
            CONSTRAINT ck_users_role CHECK (role IN ('admin','doctor','patient'))
        )""")
    op.execute("""
        INSERT INTO users_new (id, role, display, password_hash, active, created_by, created_at,
                               failed_logins, locked_until, password_changed_at, must_change_password)
        SELECT id, role, display, password_hash, active, created_by, created_at, 0, NULL, created_at, 0 FROM users""")
    op.execute("DROP TABLE users")
    op.execute("ALTER TABLE users_new RENAME TO users")

    with op.batch_alter_table("login_sessions") as b:
        b.add_column(sa.Column("created_at", sa.Float))
        b.add_column(sa.Column("last_seen_at", sa.Float))

    op.create_table(
        "care_assignments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("doctor_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("reason", sa.String(200), nullable=False),
        sa.Column("assigned_by", sa.String(40), sa.ForeignKey("users.id")),
        sa.Column("assigned_at", sa.Float, nullable=False),
        sa.Column("revoked_at", sa.Float),
        sa.Column("revoked_by", sa.String(40), sa.ForeignKey("users.id")),
    )
    op.create_index("ix_care_assignments_doctor_id", "care_assignments", ["doctor_id"])
    op.create_index("ix_care_assignments_patient_id", "care_assignments", ["patient_id"])
    op.get_bind().execute(sa.text(
        "INSERT INTO care_assignments (doctor_id, patient_id, reason, assigned_by, assigned_at) "
        "SELECT DISTINCT doctor_id, patient_id, 'migrated from existing visits', NULL, :now FROM visits"), {"now": time.time()})

    op.create_table(
        "patient_context_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("visit_id", "version"),
    )
    op.create_index("ix_patient_context_versions_visit_id", "patient_context_versions", ["visit_id"])
    op.create_table(
        "plan_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("plan_versions.id")),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("visit_id", "version"),
    )
    op.create_index("ix_plan_versions_visit_id", "plan_versions", ["visit_id"])

    with op.batch_alter_table("approvals") as b:
        b.add_column(sa.Column("plan_version_id", sa.Integer, sa.ForeignKey("plan_versions.id", name="fk_approvals_plan")))
        b.add_column(sa.Column("transcript_version_id", sa.Integer,
                               sa.ForeignKey("transcript_versions.id", name="fk_approvals_transcript")))
        b.add_column(sa.Column("signer_name", sa.String(80)))
        b.add_column(sa.Column("meaning", sa.String(200)))
        b.add_column(sa.Column("signature_method", sa.String(40)))
    op.execute("UPDATE approvals SET transcript_version_id = (SELECT transcript_version_id FROM summary_versions "
               "WHERE summary_versions.id = approvals.clinician_version_id)")

    op.create_table(
        "assistant_conversations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("doctor_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
    )
    op.create_index("ix_assistant_conversations_visit_id", "assistant_conversations", ["visit_id"])
    op.create_table(
        "assistant_messages",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("conversation_id", sa.Integer, sa.ForeignKey("assistant_conversations.id"), nullable=False),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("author", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("context_version", sa.String(64)),
        sa.Column("model", sa.String(80)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.CheckConstraint("author IN ('doctor','assistant')"),
    )
    op.create_index("ix_assistant_messages_conversation_id", "assistant_messages", ["conversation_id"])
    op.create_index("ix_assistant_messages_visit_id", "assistant_messages", ["visit_id"])
    op.create_table(
        "assistant_suggestions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("message_id", sa.Integer, sa.ForeignKey("assistant_messages.id"), nullable=False),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("citations", sa.JSON, nullable=False),
        sa.Column("medication", sa.JSON),
        sa.Column("safety", sa.JSON),
        sa.Column("context_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("final_text", sa.Text),
        sa.Column("decided_by", sa.String(40), sa.ForeignKey("users.id")),
        sa.Column("decided_at", sa.Float),
        sa.CheckConstraint("status IN ('pending','accepted','edited','rejected')"),
    )
    op.create_index("ix_assistant_suggestions_message_id", "assistant_suggestions", ["message_id"])
    op.create_index("ix_assistant_suggestions_visit_id", "assistant_suggestions", ["visit_id"])

    op.create_table(
        "audit_events",
        sa.Column("seq", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("ts_utc", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(40)),
        sa.Column("actor_role", sa.String(10)),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("record_type", sa.String(40)),
        sa.Column("record_id", sa.String(64)),
        sa.Column("record_version", sa.String(32)),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("session_ref", sa.String(16)),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.CheckConstraint("outcome IN ('success','denied','failure')"),
    )
    op.create_index("ix_audit_events_ts_utc", "audit_events", ["ts_utc"])
    op.create_index("ix_audit_events_actor_id", "audit_events", ["actor_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_record_id", "audit_events", ["record_id"])
    # Append-only at the database level. Not tamper-proof: a user with write access to the file can drop these.
    op.execute("CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
               "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END")
    op.execute("CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events "
               "BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END")


def downgrade():
    raise NotImplementedError("Downgrades are not supported; restore a backup from data/database/backups instead.")
