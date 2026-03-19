"""add user_id to investigations

Revision ID: b3d4e5f6a7b8
Revises: a1b2c3d4e5f6
Create Date: 2026-03-19

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d4e5f6a7b8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("investigations", sa.Column("user_id", sa.String(), nullable=True))
    op.create_index("ix_investigations_user_id", "investigations", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_investigations_user_id", "investigations")
    op.drop_column("investigations", "user_id")
