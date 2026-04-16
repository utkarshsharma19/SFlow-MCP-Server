from pydantic import BaseModel
from datetime import datetime


class InterfaceCounter(BaseModel):
    """Raw counter snapshot from sFlow-RT."""

    agent: str
    if_index: int
    if_name: str
    if_speed: int                       # bps (link capacity)
    if_in_octets: int
    if_out_octets: int
    if_in_errors: int
    if_out_errors: int
    timestamp: datetime


class InterfaceUtilization(BaseModel):
    """Computed utilization for a time bucket."""

    ts_bucket: datetime
    device: str
    interface: str
    in_bps: int
    out_bps: int
    in_util_pct: float                  # 0.0 - 100.0
    out_util_pct: float
    error_count: int
