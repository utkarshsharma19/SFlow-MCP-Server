"""MCP tool call audit + per-tenant per-tool quota

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-18

Two enterprise-facing additions:

- ``tool_call_audit`` — one row per MCP tool invocation. Separate from
  the HTTP ``audit_log`` because regulators and security teams care
  specifically about what the LLM called, with what arguments (hashed),
  and how much it got back.

- ``tenant_quotas`` — per-tenant per-tool call/byte counters roll up by
  day-anchored ``period_start``. The MCP audit endpoint increments
  counters atomically and refuses calls when limits are reached.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tool_call_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("args_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "args_truncated",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("response_bytes", sa.Integer(), nullable=True),
        sa.Column("confidence_band", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index(
        "ix_tool_audit_tenant_ts", "tool_call_audit", ["tenant_id", "ts"]
    )
    op.create_index(
        "ix_tool_audit_tool_ts", "tool_call_audit", ["tool_name", "ts"]
    )

    op.create_table(
        "tenant_quotas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "calls_this_period",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "bytes_out_this_period",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("call_limit", sa.BigInteger(), nullable=True),
        sa.Column("byte_limit", sa.BigInteger(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "tenant_id", "tool_name", "period_start", name="uq_tenant_quota_period"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_tenant_quotas_tenant", "tenant_quotas", ["tenant_id"]
    )

    # Enable RLS on both new tables so they inherit tenant isolation.
    for table in ("tool_call_audit", "tenant_quotas"):
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
    for table in ("tool_call_audit", "tenant_quotas"):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_tenant_quotas_tenant", table_name="tenant_quotas")
    op.drop_table("tenant_quotas")
    op.drop_index("ix_tool_audit_tool_ts", table_name="tool_call_audit")
    op.drop_index("ix_tool_audit_tenant_ts", table_name="tool_call_audit")
    op.drop_table("tool_call_audit")
