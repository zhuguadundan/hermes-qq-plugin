"""Shared types for the Hermes QQ native adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class QQEventSource:
    user_id: str
    group_id: Optional[str]
    message_id: Optional[str]
    self_id: Optional[str]
    raw: Dict[str, Any]
