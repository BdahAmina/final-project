"""Baseline: the schema as it existed before migrations were introduced (created by create_all).

Existing databases with these tables but no alembic_version table are stamped at this revision
instead of running it.

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("display", sa.String(80), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean, nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.CheckConstraint("role IN ('doctor','patient')"),
    )
    op.create_table(
        "login_sessions",
        sa.Column("token_sha256", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("expires_at", sa.Float, nullable=False),
    )
    op.create_index("ix_login_sessions_user_id", "login_sessions", ["user_id"])
    op.create_table(
        "visits",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("doctor_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("patient_id", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("language", sa.String(20)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("error_category", sa.String(40)),
        sa.Column("consent", sa.Boolean, nullable=False),
        sa.Column("input_origin", sa.String(20)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("state IN ('received','transcribing','source_review','extracting','draft','approved','failed')"),
    )
    op.create_table(
        "transcript_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("transcript_versions.id")),
        sa.Column("language", sa.String(20)),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("roles_confirmed", sa.Boolean, nullable=False),
        sa.Column("redaction_reviewed", sa.Boolean, nullable=False),
        sa.Column("notices", sa.JSON, nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("visit_id", "version"),
        sa.CheckConstraint("origin IN ('auto','edited','manual','redacted')"),
    )
    op.create_index("ix_transcript_versions_visit_id", "transcript_versions", ["visit_id"])
    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transcript_version_id", sa.Integer, sa.ForeignKey("transcript_versions.id"), nullable=False),
        sa.Column("segment_key", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("start", sa.Float, nullable=False),
        sa.Column("end", sa.Float, nullable=False),
        sa.Column("speaker_label", sa.String(40), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("words", sa.JSON, nullable=False),
        sa.Column("flags", sa.JSON, nullable=False),
        sa.UniqueConstraint("transcript_version_id", "segment_key"),
    )
    op.create_index("ix_transcript_segments_transcript_version_id", "transcript_segments", ["transcript_version_id"])
    op.create_table(
        "speaker_assignments",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("transcript_version_id", sa.Integer, sa.ForeignKey("transcript_versions.id"), nullable=False),
        sa.Column("speaker_label", sa.String(40), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("assigned_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("assigned_at", sa.Float, nullable=False),
        sa.UniqueConstraint("transcript_version_id", "speaker_label"),
        sa.CheckConstraint("role IN ('doctor','patient','other','unknown')"),
    )
    op.create_index("ix_speaker_assignments_transcript_version_id", "speaker_assignments", ["transcript_version_id"])
    op.create_table(
        "summary_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("transcript_version_id", sa.Integer, sa.ForeignKey("transcript_versions.id"), nullable=False),
        sa.Column("parent_id", sa.Integer, sa.ForeignKey("summary_versions.id")),
        sa.Column("origin", sa.String(10), nullable=False),
        sa.Column("content", sa.JSON, nullable=False),
        sa.Column("blocking", sa.Boolean, nullable=False),
        sa.Column("created_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.UniqueConstraint("visit_id", "kind", "version"),
        sa.CheckConstraint("kind IN ('clinician','patient')"),
    )
    op.create_index("ix_summary_versions_visit_id", "summary_versions", ["visit_id"])
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("clinician_version_id", sa.Integer, sa.ForeignKey("summary_versions.id"), nullable=False),
        sa.Column("patient_version_id", sa.Integer, sa.ForeignKey("summary_versions.id"), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("approved_by", sa.String(40), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_at", sa.Float, nullable=False),
        sa.Column("revoked_at", sa.Float),
        sa.Column("revoked_reason", sa.String(40)),
    )
    op.create_index("ix_approvals_visit_id", "approvals", ["visit_id"])
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("visit_id", sa.String(32), sa.ForeignKey("visits.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("error_category", sa.String(40)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
    )
    op.create_index("ix_jobs_visit_id", "jobs", ["visit_id"])


def downgrade():
    raise NotImplementedError("Downgrades are not supported; restore a backup from data/database/backups instead.")
