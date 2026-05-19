"""Transport layer abstraction over the MeshCore companion device.

The `Transport` protocol defines the surface the BBS uses. `MeshCoreTransport`
wraps `meshcore_py`; `MockTransport` is used in tests and integration runs.
"""

from .base import (
    InboundMessage,
    SendOutcome,
    Transport,
    TransportEvent,
    TransportEventType,
)
from .mock import MockTransport

__all__ = [
    "InboundMessage",
    "MockTransport",
    "SendOutcome",
    "Transport",
    "TransportEvent",
    "TransportEventType",
]
