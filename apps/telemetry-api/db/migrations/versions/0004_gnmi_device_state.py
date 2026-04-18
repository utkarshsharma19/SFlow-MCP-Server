"""gNMI device state — device_state_minute, bgp_session_minute, queue_stats_minute

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-18

Adds the storage layer for gNMI / OpenConfig telemetry. Unlike sFlow these
samples are exact, not sampling-corrected, so no sampling_rate column is
carried. Every table is tenant-scoped from the start to match PR 20.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_state_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("admin_status", sa.String(length=16), nullable=False),
        sa.Column("oper_status", sa.String(length=16), nullable=False),
        sa.Column("last_change", sa.DateTime(timezone=True), nullable=True),
        sa.Column("speed_bps", sa.BigInteger(), nullable=True),
        sa.Column("mtu", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_devstate_tenant_ts", "device_state_minute", ["tenant_id", "ts_bucket"]
    )
    op.create_index(
        "ix_devstate_device_if_ts",
        "device_state_minute",
        ["device", "interface", "ts_bucket"],
    )

    op.create_table(
        "bgp_session_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("peer_address", sa.String(length=64), nullable=False),
        sa.Column("peer_as", sa.Integer(), nullable=True),
        sa.Column("session_state", sa.String(length=32), nullable=False),
        sa.Column("uptime_seconds", sa.BigInteger(), nullable=True),
        sa.Column("prefixes_received", sa.BigInteger(), nullable=True),
        sa.Column("prefixes_sent", sa.BigInteger(), nullable=True),
        sa.Column("last_error", sa.String(length=255), nullable=True),
    )
    op.create_index(
        "ix_bgp_tenant_ts", "bgp_session_minute", ["tenant_id", "ts_bucket"]
    )
    op.create_index(
        "ix_bgp_device_peer_ts",
        "bgp_session_minute",
        ["device", "peer_address", "ts_bucket"],
    )

    op.create_table(
        "queue_stats_minute",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("ts_bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device", sa.String(length=255), nullable=False),
        sa.Column("interface", sa.String(length=255), nullable=False),
        sa.Column("queue_id", sa.Integer(), nullable=False),
        sa.Column("traffic_class", sa.Integer(), nullable=True),
        sa.Column("max_depth_bytes", sa.BigInteger(), nullable=False),
        sa.Column("avg_depth_bytes", sa.BigInteger(), nullable=False),
        sa.Column("pfc_pause_rx", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("pfc_pause_tx", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("ecn_marked_packets", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("dropped_packets", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_queue_tenant_ts", "queue_stats_minute", ["tenant_id", "ts_bucket"]
    )
    op.create_index(
        "ix_queue_device_if_q_ts",
        "queue_stats_minute",
        ["device", "interface", "queue_id", "ts_bucket"],
    )


def downgrade() -> None:
    op.drop_index("ix_queue_device_if_q_ts", table_name="queue_stats_minute")
    op.drop_index("ix_queue_tenant_ts", table_name="queue_stats_minute")
    op.drop_table("queue_stats_minute")

    op.drop_index("ix_bgp_device_peer_ts", table_name="bgp_session_minute")
    op.drop_index("ix_bgp_tenant_ts", table_name="bgp_session_minute")
    op.drop_table("bgp_session_minute")

    op.drop_index("ix_devstate_device_if_ts", table_name="device_state_minute")
    op.drop_index("ix_devstate_tenant_ts", table_name="device_state_minute")
    op.drop_table("device_state_minute")
