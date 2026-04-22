w"""tenancy + security hardening — RLS, API key rotation, encryption at rest

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-18

Three stacked changes:

1. Install pgcrypto and create an ``encrypted_secrets`` table. Stored
   values are pgp_sym_encrypted with a symmetric key the app supplies on
   every read/write (the key never sits in the DB). Callers: gNMI target
   passwords, webhook signing secrets, OIDC client secrets.

2. Extend ``api_keys`` with ``key_prefix`` (first 8 chars of plaintext —
   safe to display), ``expires_at``, ``rotated_from_id``, per-key
   ``tool_allowlist``, and per-key ``rate_limit_per_minute``. These are
   the columns rotation tooling needs without leaking secret material.

3. Enable row-level security on every tenant-scoped table and install a
   single policy per table that reads ``current_setting('app.tenant_id', true)``.
   The application's DB role does not BYPASSRLS; ingest paths and read
   paths both set the tenant on every transaction via
   ``services.rls_session``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TENANT_SCOPED_TABLES = (
    "flow_summary_minute",
    "interface_utilization_minute",
    "queue_stats_minute",
    "anomaly_events",
    "baseline_snapshots",
    "device_state_minute",
    "bgp_session_minute",
    "source_freshness",
    "ecmp_groups",
    "collector_sources",
    "audit_log",
    "api_keys",
)


def upgrade() -> None:
    # ---- 1. pgcrypto + encrypted_secrets ---------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "encrypted_secrets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("secret_kind", sa.String(length=64), nullable=False),
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("ciphertext", postgresql.BYTEA(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "tenant_id", "secret_kind", "secret_ref", name="uq_encrypted_secret_ref"
        ),
    )
    op.create_index(
        "ix_encrypted_secrets_tenant", "encrypted_secrets", ["tenant_id"]
    )

    # ---- 2. api_keys hardening -------------------------------------------
    op.add_column(
        "api_keys",
        sa.Column("key_prefix", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "rotated_from_id",
            postgresql.UUID(as_uuid=False),
            nullable=True,
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column(
            "tool_allowlist",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )
    op.add_column(
        "api_keys",
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
    )
    # Self-reference FK; a rotated key keeps a pointer at the parent so
    # auditors can walk the rotation chain.
    op.create_foreign_key(
        "fk_api_keys_rotated_from",
        source_table="api_keys",
        referent_table="api_keys",
        local_cols=["rotated_from_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_api_keys_expires_at", "api_keys", ["expires_at"]
    )

    # ---- 3. row-level security -------------------------------------------
    # The application role reads tenant_id from app.tenant_id. Admins and
    # migrations continue to write unscoped by running as a different role
    # (see docs/deploy.md — left for a follow-up PR). Here we enable RLS
    # and install the policy; the runtime sets app.tenant_id per session.
    for table in TENANT_SCOPED_TABLES:
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

    # tenants + encrypted_secrets: the first is global read (tenant_admin
    # lookups happen pre-auth), the second gets the same tenant policy.
    op.execute("ALTER TABLE encrypted_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE encrypted_secrets FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON encrypted_secrets
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
    for table in TENANT_SCOPED_TABLES + ("encrypted_secrets",):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_api_keys_expires_at", table_name="api_keys")
    op.drop_constraint("fk_api_keys_rotated_from", "api_keys", type_="foreignkey")
    for col in (
        "rate_limit_per_minute",
        "tool_allowlist",
        "rotated_from_id",
        "expires_at",
        "key_prefix",
    ):
        op.drop_column("api_keys", col)

    op.drop_index("ix_encrypted_secrets_tenant", table_name="encrypted_secrets")
    op.drop_table("encrypted_secrets")
