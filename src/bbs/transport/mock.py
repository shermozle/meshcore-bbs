"""In-memory mock transport.

Used by integration tests (and `python -m bbs --mock` for offline dev). Sent
messages are recorded; inbound events can be injected via `inject_inbound`.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

from .base import InboundMessage, SendOutcome, TransportEvent, TransportEventType


class MockTransport:
    def __init__(self, self_pubkey: str = "a" * 64, contact_capacity_max: int = 200) -> None:
        self._self_pubkey = self_pubkey
        self._events: asyncio.Queue[TransportEvent] = asyncio.Queue()
        # Recorded sends for assertions:
        self.sent: list[tuple[str, str]] = []
        # When set, send_msg returns this for the matching recipient pubkey.
        self.next_send_outcome: dict[str, SendOutcome] = defaultdict(lambda: SendOutcome.OK)
        self._contact_pubkeys: set[str] = set()
        self._contact_capacity = contact_capacity_max
        self._inbound_paths: dict[str, list[str]] = {}
        self.adverts_sent: list[bool] = []
        self._started = False
        self._stopped = False

    @property
    def self_pubkey(self) -> str:
        return self._self_pubkey

    @property
    def radio_available(self) -> bool:
        return not self._stopped

    async def start(self) -> None:
        self._started = True
        await self._events.put(TransportEvent(type=TransportEventType.CONNECTED))

    async def stop(self) -> None:
        self._stopped = True
        await self._events.put(TransportEvent(type=TransportEventType.DISCONNECTED))

    async def send_msg(self, to_pubkey: str, body: str) -> SendOutcome:
        self.sent.append((to_pubkey, body))
        return self.next_send_outcome[to_pubkey]

    def events(self) -> asyncio.Queue[TransportEvent]:
        return self._events

    async def send_advert(self, *, flood: bool = False) -> None:
        self.adverts_sent.append(flood)

    async def sync_time(self, epoch: int) -> None:
        return None

    async def contact_capacity(self) -> tuple[int, int]:
        return (len(self._contact_pubkeys), self._contact_capacity)

    async def prune_contact(self, pubkey: str) -> None:
        self._contact_pubkeys.discard(pubkey)
        self._inbound_paths.pop(pubkey, None)

    async def resolve_inbound_path(self, pubkey: str, *, discover: bool = True) -> list[str]:
        del discover  # mock has no radio path discovery
        return list(self._inbound_paths.get(pubkey.lower(), []))

    # Test helpers --------------------------------------------------------

    async def inject_mesh_activity(self, pubkey: str) -> None:
        """Simulate overheard mesh traffic (advert, channel, flood) from a node."""
        self._contact_pubkeys.add(pubkey)
        await self._events.put(
            TransportEvent(type=TransportEventType.MESH_ACTIVITY, pubkey=pubkey.lower())
        )

    async def inject_inbound(
        self,
        pubkey: str,
        body: str,
        adv_name: str | None = None,
        received_at: int | None = None,
        hops: int | None = None,
        path: list[str] | None = None,
    ) -> None:
        self._contact_pubkeys.add(pubkey)
        if path is not None:
            self._inbound_paths[pubkey] = list(path)
        await self._events.put(
            TransportEvent(
                type=TransportEventType.CONTACT_MSG_RECV,
                inbound=InboundMessage(
                    pubkey=pubkey,
                    adv_name=adv_name,
                    body=body,
                    received_at=received_at if received_at is not None else int(time.time()),
                    hops=hops,
                    path=path if path is not None else [],
                ),
            )
        )

    def last_sent_to(self, pubkey: str) -> str | None:
        for pk, body in reversed(self.sent):
            if pk == pubkey:
                return body
        return None

    def all_sent_to(self, pubkey: str) -> list[str]:
        return [b for pk, b in self.sent if pk == pubkey]
