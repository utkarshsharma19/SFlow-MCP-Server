"""map chatbot users to per-tenant API keys

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-18

A chatbot gateway that serves multiple operators needs to talk to
FlowMind on behalf of each user — without revealing the gateway's
master key or scoping every user to one tenant. This migration adds
the mapping table the gateway resolves through:

    chat_user_id  →  tenant_id, api_key_id

The api_key referenced here must already exist in api_keys (issued via
``seed.py create-key``); we keep the FK so a deleted key cascades the
mapping rather than orphaning it. Tenant is denormalized for the same
reason ``audit_log.tenant_id`` is: the mapping is itself an audit
record of "which user has access to which tenant", and we want it
queryable without joining api_keys.

The gateway is expected to authenticate the chat user via its own
mechanism (SSO, JWT, etc.) and then call the resolver — we don't add
a second auth layer here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chat_user_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "chat_user_id",
            sa.String(length=255),
            nullable=False,
            comment="Opaque identifier — gateway sets to whatever SSO yields.",
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=False), nullable=False),
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
        sa.Column("last_resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "chat_user_id", "tenant_id", name="uq_chat_user_per_tenant"
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["api_key_id"], ["api_keys.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_chat_user_keys_user",
        "chat_user_keys",
        ["chat_user_id", "is_active"],
    )
    op.create_index(
        "ix_chat_user_keys_tenant",
        "chat_user_keys",
        ["tenant_id"],
    )

    # RLS: same shape as every other tenant-scoped table. The resolver
    # endpoint *bypasses* RLS for the lookup (the gateway doesn't yet
    # know which tenant the user maps to), but reads from tenant_admin
    # tooling are RLS-scoped.
    op.execute("ALTER TABLE chat_user_keys ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_user_keys FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON chat_user_keys
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
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_user_keys")
    op.drop_index("ix_chat_user_keys_tenant", table_name="chat_user_keys")
    op.drop_index("ix_chat_user_keys_user", table_name="chat_user_keys")
    op.drop_table("chat_user_keys")
