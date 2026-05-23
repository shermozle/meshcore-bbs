"""Transport interface.

The BBS uses only this surface to talk to MeshCore. The two concrete
implementations are `MeshCoreTransport` (production) and `MockTransport`
(tests / dev runs without hardware).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class TransportEventType(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    CONTACT_MSG_RECV = "contact_msg_recv"
    NEW_CONTACT = "new_contact"
    ADVERTISEMENT = "advertisement"


@dataclass
class InboundMessage:
    """A decrypted DM from a contact.

    `pubkey` is always the full 64-char hex Curve25519 key, resolved by the
    transport from the wire-level prefix.
    `hops` is the mesh hop count (0 = direct, None = unknown).
    """

    pubkey: str
    adv_name: str | None
    body: str
    received_at: int
    hops: int | None = None
    path: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.path is None:
            self.path = []


@dataclass
class TransportEvent:
    type: TransportEventType
    inbound: InboundMessage | None = None
    pubkey: str | None = None
    reconnected: bool = False


class SendOutcome(str, Enum):
    OK = "ok"             # ACK received, message delivered
    NO_ACK = "no_ack"     # path lost / recipient unreachable
    ERROR = "error"       # serial/firmware/local error; retry may succeed


class Transport(Protocol):
    """Surface the BBS dispatcher and outbound queue use."""

    @property
    def self_pubkey(self) -> str: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...

    async def send_msg(self, to_pubkey: str, body: str) -> SendOutcome:
        """Send a DM. The transport handles ACK-based retry + flood fallback
        internally; this method returns only the final outcome.
        """
        ...

    def events(self) -> asyncio.Queue[TransportEvent]:
        """Return a queue the dispatcher reads from. Events are pushed by the
        transport as they arrive from the firmware.
        """
        ...

    async def send_advert(self) -> None: ...
    async def sync_time(self, epoch: int) -> None: ...
    async def contact_capacity(self) -> tuple[int, int]:
        """Return (used, capacity)."""
        ...

    async def prune_contact(self, pubkey: str) -> None: ...

    async def resolve_inbound_path(self, pubkey: str) -> list[str]:
        """Best-effort inbound route for *pubkey* (empty when unknown)."""
        ...
