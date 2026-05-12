"""add LLM-token columns to tenant_quotas + chat session table

Revision ID: 0013
Revises: 0012
Create Date: 2026-05-12

Chatbots burn money on LLM tokens, not on tool calls. The existing
``calls_this_period`` / ``bytes_out_this_period`` counters describe what
the MCP server emits; this migration adds the *consumer-side* dimension
the chat-gateway charges back against.

Two additions:

1. ``tenant_quotas.llm_tokens_this_period`` + ``token_limit`` — the
   chat-gateway calls a new ``/tool-audit/charge-tokens`` endpoint with
   the prompt+completion token count after each turn. Same period-key
   semantics as the call counter; same upsert path.

2. ``chat_sessions`` — minimal session bookkeeping for the future chat
   gateway. We register it here so the schema is in place; the gateway
   itself lands in a follow-up PR.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "tenant_quotas",
        sa.Column(
            "llm_tokens_this_period",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "tenant_quotas",
        sa.Column("token_limit", sa.BigInteger(), nullable=True),
    )

    op.create_table(
        "chat_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=False), nullable=True),
        sa.Column("user_label", sa.String(length=128), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "tokens_in",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tokens_out",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "tool_calls",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_chat_sessions_tenant_started",
        "chat_sessions",
        ["tenant_id", "started_at"],
    )

    op.execute("ALTER TABLE chat_sessions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chat_sessions FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON chat_sessions
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
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON chat_sessions")
    op.drop_index("ix_chat_sessions_tenant_started", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_column("tenant_quotas", "token_limit")
    op.drop_column("tenant_quotas", "llm_tokens_this_period")
