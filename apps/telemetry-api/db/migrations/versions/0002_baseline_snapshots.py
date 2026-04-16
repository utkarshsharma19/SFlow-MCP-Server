"""baseline_snapshots table for rolling mean/stddev per device/interface/hour

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-16
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "baseline_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("hour_of_day", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("mean_value", sa.Float(), nullable=False),
        sa.Column("stddev_value", sa.Float(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_baseline_device_if_hour",
        "baseline_snapshots",
        ["device", "interface", "hour_of_day"],
    )


def downgrade() -> None:
    op.drop_index("ix_baseline_device_if_hour", table_name="baseline_snapshots")
    op.drop_table("baseline_snapshots")
