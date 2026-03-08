"""initial v2 schema: 8 tables

Revision ID: f957dc9e6455
Revises:
Create Date: 2026-03-08 16:58:54.028464

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f957dc9e6455"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- investigations --
    op.create_table(
        "investigations",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), server_default="active", nullable=False),
        sa.Column("current_stage", sa.String(20), server_default="research", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # -- stages --
    op.create_table(
        "stages",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("revision_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("investigation_id", "stage", name="uq_stages_investigation_stage"),
    )
    op.create_index("idx_stages_investigation", "stages", ["investigation_id"])

    # -- stage_outputs --
    op.create_table(
        "stage_outputs",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("output_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "investigation_id", "stage", "version", name="uq_stage_outputs_inv_stage_version"
        ),
    )
    op.create_index(
        "idx_stage_outputs_lookup", "stage_outputs", ["investigation_id", "stage", "version"]
    )

    # -- state_snapshots (must precede gate_log due to FK) --
    op.create_table(
        "state_snapshots",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("snapshot_data", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_snapshots_investigation", "state_snapshots", ["investigation_id", "created_at"]
    )

    # -- gate_log --
    op.create_table(
        "gate_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("feedback", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("actor", sa.String(100), server_default="journalist", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["snapshot_id"], ["state_snapshots.id"]),
    )
    op.create_index(
        "idx_gate_log_investigation", "gate_log", ["investigation_id", "created_at"]
    )

    # -- agent_log --
    op.create_table(
        "agent_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("model", sa.String(50), nullable=False),
        sa.Column(
            "called_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cache_create_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("thinking_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), server_default="0", nullable=False),
        sa.Column("tool_calls_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("iteration_count", sa.Integer(), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("idx_agent_log_investigation", "agent_log", ["investigation_id", "stage"])

    # -- messages --
    op.create_table(
        "messages",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_messages_investigation", "messages", ["investigation_id", "created_at"]
    )

    # -- query_log --
    op.create_table(
        "query_log",
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(20), nullable=False),
        sa.Column("tool_name", sa.String(50), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("sql_text", sa.Text(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["investigation_id"], ["investigations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_query_log_investigation", "query_log", ["investigation_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_table("query_log")
    op.drop_table("messages")
    op.drop_table("agent_log")
    op.drop_table("gate_log")
    op.drop_table("state_snapshots")
    op.drop_table("stage_outputs")
    op.drop_table("stages")
    op.drop_table("investigations")
