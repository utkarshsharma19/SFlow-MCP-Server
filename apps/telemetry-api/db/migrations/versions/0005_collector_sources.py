"""collector_sources — per-source → tenant mapping

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-18

Replaces the DEFAULT_TENANT_ID hardcoding in the sFlow and gNMI ingest
loops. Each row maps a (source_kind, source_identifier) pair to a tenant
so multi-tenant installs can route telemetry from a given device to the
right customer without code changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collector_sources",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_identifier", sa.String(length=255), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "source_kind",
            "source_identifier",
            name="uq_collector_source_kind_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_collector_sources_tenant", "collector_sources", ["tenant_id"]
    )
    op.create_index(
        "ix_collector_sources_lookup",
        "collector_sources",
        ["source_kind", "source_identifier"],
    )


def downgrade() -> None:
    op.drop_index("ix_collector_sources_lookup", table_name="collector_sources")
    op.drop_index("ix_collector_sources_tenant", table_name="collector_sources")
    op.drop_table("collector_sources")
