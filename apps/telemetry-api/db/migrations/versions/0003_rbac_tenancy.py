"""RBAC + multi-tenancy — tenants, api_keys, audit_log + tenant_id on telemetry tables

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-17

Adds the tenancy foundation. A well-known default tenant is seeded so
existing single-tenant installs keep working after migrating.
"""
import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_TENANT_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_DEV_KEY = "dev-insecure-key"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )

    op.create_table(
        "api_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash", name="uq_api_keys_key_hash"),
    )
    op.create_index("ix_api_keys_tenant", "api_keys", ["tenant_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=255), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_audit_log_tenant_ts", "audit_log", ["tenant_id", "ts"])

    # Seed the default tenant + a dev API key so existing deployments keep
    # working. Production installs should rotate this immediately.
    op.execute(
        sa.text(
            "INSERT INTO tenants (id, slug, name) VALUES "
            "(:id, 'default', 'Default Tenant')"
        ).bindparams(id=DEFAULT_TENANT_ID)
    )
    op.execute(
        sa.text(
            "INSERT INTO api_keys (tenant_id, key_hash, role, name) VALUES "
            "(:tid, :kh, 'tenant_admin', 'dev-insecure-key')"
        ).bindparams(tid=DEFAULT_TENANT_ID, kh=_sha256(DEFAULT_DEV_KEY))
    )

    # Backfill tenant_id on existing telemetry tables using the default tenant.
    for table in (
        "flow_summary_minute",
        "interface_utilization_minute",
        "baseline_snapshots",
        "anomaly_events",
    ):
        op.add_column(
            table,
            sa.Column(
                "tenant_id",
                postgresql.UUID(as_uuid=False),
                nullable=True,
            ),
        )
        op.execute(
            sa.text(f"UPDATE {table} SET tenant_id = :tid").bindparams(
                tid=DEFAULT_TENANT_ID
            )
        )
        op.alter_column(table, "tenant_id", nullable=False)

    op.create_index("ix_flow_tenant_ts", "flow_summary_minute", ["tenant_id", "ts_bucket"])
    op.create_index(
        "ix_util_tenant_ts",
        "interface_utilization_minute",
        ["tenant_id", "ts_bucket"],
    )
    op.create_index("ix_baseline_tenant", "baseline_snapshots", ["tenant_id"])
    op.create_index("ix_anomaly_tenant_ts", "anomaly_events", ["tenant_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_anomaly_tenant_ts", table_name="anomaly_events")
    op.drop_index("ix_baseline_tenant", table_name="baseline_snapshots")
    op.drop_index("ix_util_tenant_ts", table_name="interface_utilization_minute")
    op.drop_index("ix_flow_tenant_ts", table_name="flow_summary_minute")

    for table in (
        "flow_summary_minute",
        "interface_utilization_minute",
        "baseline_snapshots",
        "anomaly_events",
    ):
        op.drop_column(table, "tenant_id")

    op.drop_index("ix_audit_log_tenant_ts", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_api_keys_tenant", table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_table("tenants")
