"""add research_assets table

Revision ID: a1b2c3d4e5f6
Revises: f957dc9e6455
Create Date: 2026-03-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f957dc9e6455"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "research_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("investigation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("initiatives", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("votes", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["investigation_id"], ["investigations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("investigation_id", name="uq_research_assets_investigation"),
    )


def downgrade() -> None:
    op.drop_table("research_assets")
