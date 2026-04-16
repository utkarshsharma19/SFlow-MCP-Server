from pydantic import BaseModel, Field
from datetime import datetime


class FlowRecord(BaseModel):
    """Raw flow record as returned by sFlow-RT RESTflow API."""

    agent: str                          # Source device IP
    input_if_index: int
    output_if_index: int
    src_ip: str
    dst_ip: str
    protocol: int                       # IANA protocol number
    bytes: int                          # RAW sample bytes (not estimated)
    packets: int                        # RAW sample packets
    sampling_rate: int                  # e.g. 1000 means 1-in-1000 sampled
    timestamp: datetime


class FlowSummary(BaseModel):
    """Normalized, estimated-volume record stored in DB."""

    ts_bucket: datetime                 # Minute-aligned
    device: str
    interface: str
    src_ip: str
    dst_ip: str
    protocol: int
    bytes_estimated: int = Field(..., description="bytes * sampling_rate")
    packets_estimated: int
    sampling_rate: int                  # ALWAYS store with the estimate
