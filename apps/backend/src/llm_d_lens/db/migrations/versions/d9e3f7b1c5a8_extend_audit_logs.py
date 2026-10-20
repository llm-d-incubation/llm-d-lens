"""extend audit logs for the unified audit ledger

Revision ID: d9e3f7b1c5a8
Revises: b8d4e2f6a1c3
Create Date: 2026-10-19 00:00:00.000000

Adds request correlation, source/action, status/duration and target display
fields used by the unified audit pipeline. Existing rows keep ``legacy`` as
their action because their original semantics cannot be reconstructed.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e3f7b1c5a8"
down_revision: str | Sequence[str] | None = "b8d4e2f6a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("audit_logs", "event_type", existing_type=sa.String(32), type_=sa.String(96))
    op.add_column(
        "audit_logs",
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'system'")),
    )
    op.add_column(
        "audit_logs",
        sa.Column("action", sa.String(16), nullable=False, server_default=sa.text("'legacy'")),
    )
    op.add_column("audit_logs", sa.Column("target_name", sa.String(256), nullable=True))
    op.add_column("audit_logs", sa.Column("status_code", sa.Integer(), nullable=True))
    op.add_column("audit_logs", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column("audit_logs", sa.Column("request_id", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("trace_id", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("parent_request_id", sa.String(64), nullable=True))
    op.add_column("audit_logs", sa.Column("user_agent", sa.String(512), nullable=True))
    op.create_index("ix_audit_logs_target_created", "audit_logs", ["target_type", "target_id", "created_at"])
    op.create_index("ix_audit_logs_cluster_created", "audit_logs", ["cluster_id", "created_at"])
    op.create_index("ix_audit_logs_request", "audit_logs", ["request_id"])
    op.create_index("ix_audit_logs_result_created", "audit_logs", ["result", "created_at"])
    op.create_index("ix_audit_logs_source_created", "audit_logs", ["source", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_source_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_result_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_request", table_name="audit_logs")
    op.drop_index("ix_audit_logs_cluster_created", table_name="audit_logs")
    op.drop_index("ix_audit_logs_target_created", table_name="audit_logs")
    op.drop_column("audit_logs", "user_agent")
    op.drop_column("audit_logs", "parent_request_id")
    op.drop_column("audit_logs", "trace_id")
    op.drop_column("audit_logs", "request_id")
    op.drop_column("audit_logs", "duration_ms")
    op.drop_column("audit_logs", "status_code")
    op.drop_column("audit_logs", "target_name")
    op.drop_column("audit_logs", "action")
    op.drop_column("audit_logs", "source")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("audit_logs", "event_type", existing_type=sa.String(96), type_=sa.String(32))
