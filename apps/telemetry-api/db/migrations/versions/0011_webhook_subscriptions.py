"""webhook subscriptions for critical anomalies

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-10

Most enterprise consumers expect push notification on critical events,
not poll. We model that explicitly as a per-tenant subscription set:
each row carries the target URL and the ``secret_ref`` of the HMAC key
stored in ``encrypted_secrets``. The dispatcher loop reads new critical
anomalies, signs the payload with HMAC-SHA256, and POSTs.

We deliberately don't store delivery state on the subscription row —
delivery attempts are append-only in ``webhook_deliveries`` so an
operator can replay or audit without the row mutating underneath them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


WEBHOOK_TABLES = ("webhook_subscriptions", "webhook_deliveries")


def upgrade() -> None:
    op.create_table(
        "webhook_subscriptions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("target_url", sa.String(length=2048), nullable=False),
        sa.Column(
            "secret_ref",
            sa.String(length=255),
            nullable=False,
            comment="key in encrypted_secrets (secret_kind=webhook_secret)",
        ),
        sa.Column(
            "severity_min",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'critical'"),
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_webhook_sub_tenant_active",
        "webhook_subscriptions",
        ["tenant_id", "is_active"],
    )

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=False),
            nullable=False,
        ),
        sa.Column(
            "anomaly_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=512), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.UniqueConstraint(
            "subscription_id",
            "anomaly_id",
            name="uq_webhook_delivery_per_anomaly",
        ),
        sa.ForeignKeyConstraint(
            ["subscription_id"],
            ["webhook_subscriptions.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_tenant_ts",
        "webhook_deliveries",
        ["tenant_id", "ts"],
    )

    for table in WEBHOOK_TABLES:
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
    for table in WEBHOOK_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
    op.drop_index("ix_webhook_deliveries_tenant_ts", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_sub_tenant_active", table_name="webhook_subscriptions")
    op.drop_table("webhook_subscriptions")
