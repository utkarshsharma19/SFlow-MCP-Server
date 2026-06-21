"""device intent snapshots for intent-vs-state diff

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-10

The fabric has *intent* (what the operator declared) and *state* (what
gNMI reports). When an orchestrator like Verity is in the loop, intent
lives in the orchestrator's DB. We don't want the MCP server to talk to
the orchestrator directly on every tool call — the diff is the only
place that needs intent, and intent changes on human timescales (hours)
while state changes per-minute.

So we cache the intent here. A Verity-side connector (future PR) syncs
into ``device_intent``; the diff service reads from this table and
compares against ``device_state_minute`` / ``bgp_session_minute``. Today
operators load intent via ``scripts/seed.py set-intent``.

Two tables:

* ``device_intent`` — per (tenant, device, interface) expected state.
  NULL fields = "no opinion, accept anything". This is important for
  Verity import: Verity may only declare admin_status on one port and
  mtu on another. Diffs treat NULL as match.

* ``bgp_intent`` — per (tenant, device, peer_address) expected session
  state. Same NULL semantics. Mainly used to flag "this peer was
  declared but never came up" and "this peer is up but wasn't declared
  by intent".
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


INTENT_TABLES = ("device_intent", "bgp_intent")


def upgrade() -> None:
    op.create_table(
        "device_intent",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("expected_admin_status", sa.String(length=16), nullable=True),
        sa.Column("expected_oper_status", sa.String(length=16), nullable=True),
        sa.Column("expected_speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("expected_mtu", sa.Integer(), nullable=True),
        sa.Column("expected_description", sa.String(length=255), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "device", "interface", name="uq_device_intent_iface"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_device_intent_tenant_device",
        "device_intent",
        ["tenant_id", "device"],
    )

    op.create_table(
        "bgp_intent",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("peer_address", sa.String(length=64), nullable=False),
        sa.Column("expected_peer_as", sa.Integer(), nullable=True),
        sa.Column("expected_session_state", sa.String(length=32), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False, server_default=sa.text("'manual'")),
        sa.Column("notes", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "device", "peer_address", name="uq_bgp_intent_peer"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_bgp_intent_tenant_device",
        "bgp_intent",
        ["tenant_id", "device"],
    )

    # Inherit tenant isolation via RLS, matching every other tenant-scoped table.
    for table in INTENT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
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
    for table in INTENT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_bgp_intent_tenant_device", table_name="bgp_intent")
    op.drop_table("bgp_intent")
    op.drop_index("ix_device_intent_tenant_device", table_name="device_intent")
    op.drop_table("device_intent")
