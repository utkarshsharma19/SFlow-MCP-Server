from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, Dict, Any
import uuid


class AnomalyEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ts: datetime
    scope: str                          # 'global' | 'device:hostname' | 'interface:dev/if'
    anomaly_type: str                   # 'spike' | 'shift' | 'threshold_breach' | 'new_talker'
    severity: str                       # 'low' | 'medium' | 'high' | 'critical'
    summary: str                        # Human-readable description
    metadata: Optional[Dict[str, Any]] = None
