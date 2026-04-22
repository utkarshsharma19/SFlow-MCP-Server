"""ecmp_groups — operator-defined ECMP member sets per device

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-18

Stores curated ECMP (equal-cost multipath) groups: one row per (device,
group_name), with the member interface list as a JSONB array. The
imbalance detector consults this table first; if no group is configured
for a device it falls back to a speed-based heuristic.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ecmp_groups",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("group_name", sa.String(length=128), nullable=False),
        sa.Column(
            "members",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "device", "group_name", name="uq_ecmp_group_per_device"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_ecmp_tenant_device", "ecmp_groups", ["tenant_id", "device"]
    )


def downgrade() -> None:
    op.drop_index("ix_ecmp_tenant_device", table_name="ecmp_groups")
    op.drop_table("ecmp_groups")
