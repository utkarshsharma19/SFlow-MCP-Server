"""LLDP neighbors table for topology + path ordering

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-12

LLDP gives us the only ground-truth L2 adjacency the fabric exposes.
Without it, ``find_path`` can only return an unordered set; with it
we walk the graph and emit ``ordered=true``. We persist one row per
(device, interface, neighbor_chassis_id) and refresh it on every gNMI
poll — neighbors change rarely (cable moves) but a fresh sample lets
us detect when a neighbor *disappeared*, which is itself an event.

A composite unique key on (tenant, device, interface, neighbor_chassis_id)
makes the upsert path the natural ingest pattern. We don't bucket by
time because LLDP isn't time-series — there's only ever "the current
neighbor on this port".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "lldp_neighbors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("neighbor_chassis_id", sa.String(length=128), nullable=False),
        sa.Column("neighbor_system_name", sa.String(length=255), nullable=True),
        sa.Column("neighbor_port_id", sa.String(length=255), nullable=True),
        sa.Column(
            "neighbor_port_description", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "neighbor_management_address", sa.String(length=64), nullable=True
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "device",
            "interface",
            "neighbor_chassis_id",
            name="uq_lldp_neighbor",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_lldp_tenant_device", "lldp_neighbors", ["tenant_id", "device"]
    )
    op.create_index(
        "ix_lldp_tenant_neighbor_name",
        "lldp_neighbors",
        ["tenant_id", "neighbor_system_name"],
    )

    op.execute("ALTER TABLE lldp_neighbors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE lldp_neighbors FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON lldp_neighbors
        USING (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        WITH CHECK (
            tenant_id::text = current_setting('app.tenant_id', true)
            OR current_setting('app.rls_bypass', true) = 'on'
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON lldp_neighbors")
    op.drop_index("ix_lldp_tenant_neighbor_name", table_name="lldp_neighbors")
    op.drop_index("ix_lldp_tenant_device", table_name="lldp_neighbors")
    op.drop_table("lldp_neighbors")
