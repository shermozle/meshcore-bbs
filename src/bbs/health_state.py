"""Shared runtime health flags for HTTP and the event pump."""

from __future__ import annotations

from dataclasses import dataclass

HEALTH_HEARTBEAT_THRESHOLD = 600  # seconds


@dataclass
class HealthState:
    transport_connected: bool = False
    last_event_at: float = 0.0
