"""ChatSession ORM model (PR 31).

Lives in its own module rather than ``models.py`` because the table
itself was created by migration 0013 with the ``chat_sessions`` name
but never wired to an ORM class — the model file pre-dated the
session-tracking design. Keeping it separate avoids re-flowing every
import in models.py and keeps the migration → model mapping obvious.

A chat session is one logical conversation between a chatbot user and
the LLM. The chat gateway is expected to:

  1. POST /chat-sessions on the first turn — gets back the session id
  2. PATCH /chat-sessions/{id} after each turn to bump
     tokens_in/tokens_out/tool_calls
  3. POST /chat-sessions/{id}/end on disconnect/timeout
"""
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from db.models import Base


class ChatSession(Base):
    """One row per conversation. Tenant-scoped via RLS."""

    __tablename__ = "chat_sessions"

    id = Column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    tenant_id = Column(
        UUID(as_uuid=False),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    api_key_id = Column(UUID(as_uuid=False), nullable=True)
    user_label = Column(String(128), nullable=True)
    started_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at = Column(DateTime(timezone=True), nullable=True)
    tokens_in = Column(BigInteger, nullable=False, server_default=text("0"))
    tokens_out = Column(BigInteger, nullable=False, server_default=text("0"))
    tool_calls = Column(Integer, nullable=False, server_default=text("0"))

    __table_args__ = (
        Index("ix_chat_sessions_tenant_started", "tenant_id", "started_at"),
    )
