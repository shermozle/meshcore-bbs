"""Real `meshcore_py` transport.

This module wraps the meshcore library to present the BBS's `Transport`
interface. The exact method names below mirror those referenced in the spec
(`MeshCore.create_serial`, `send_appstart`, `set_time`, `get_contacts`,
`get_contact_by_key_prefix`, `send_msg_with_retry`, etc.). If the installed
library version exposes slightly different names, adjust the wrappers here
and the rest of the application is unaffected.

The transport pushes events onto an asyncio.Queue. The library subscription
callbacks bridge into that queue; everything downstream of the queue runs in
normal asyncio task context.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .base import InboundMessage, SendOutcome, TransportEvent, TransportEventType

log = logging.getLogger(__name__)


class MeshCoreTransport:
    """Production transport. Imports `meshcore` lazily so the package is
    importable for tests in environments without the library."""

    def __init__(
        self,
        serial_path: str,
        baud: int = 115200,
        expected_pubkey: str = "",
        max_reconnect_attempts: int = 0,
    ) -> None:
        self.serial_path = serial_path
        self.baud = baud
        self.expected_pubkey = expected_pubkey.lower()
        self.max_reconnect_attempts = max_reconnect_attempts

        self._mc: Any = None  # meshcore.MeshCore
        self._events: asyncio.Queue[TransportEvent] = asyncio.Queue()
        self._self_pubkey: str = ""
        self._contact_cache: dict[str, dict[str, Any]] = {}

    @property
    def self_pubkey(self) -> str:
        return self._self_pubkey

    def events(self) -> asyncio.Queue[TransportEvent]:
        return self._events

    async def start(self) -> None:
        # Lazy import so test environments don't need the library installed.
        from meshcore import MeshCore  # type: ignore[import-not-found]

        self._mc = await MeshCore.create_serial(
            self.serial_path,
            self.baud,
            auto_reconnect=True,
            max_reconnect_attempts=self.max_reconnect_attempts,
        )
        info = await self._mc.send_appstart()
        # SELF_INFO payload normalisation -- library returns either dict or object.
        self_pubkey = _pubkey_from_self_info(info)
        if not self_pubkey:
            raise RuntimeError("Could not read self pubkey from companion SELF_INFO")
        self._self_pubkey = self_pubkey.lower()

        if self.expected_pubkey and self.expected_pubkey != self._self_pubkey:
            raise RuntimeError(
                f"Connected device pubkey {self._self_pubkey} != expected "
                f"{self.expected_pubkey}; refusing to run"
            )

        await self._mc.set_time(int(time.time()))
        await self._refresh_contacts()

        # Subscribe to firmware events.
        self._mc.subscribe("CONTACT_MSG_RECV", self._on_contact_msg)
        self._mc.subscribe("NEW_CONTACT", self._on_new_contact)
        self._mc.subscribe("ADVERTISEMENT", self._on_advertisement)
        self._mc.subscribe("CONNECTED", self._on_connected)
        self._mc.subscribe("DISCONNECTED", self._on_disconnected)

        await self._mc.start_auto_message_fetching()

    async def stop(self) -> None:
        if self._mc is not None:
            try:
                await self._mc.disconnect()
            except Exception as e:
                log.warning("error during transport stop: %s", e)
            self._mc = None

    async def send_msg(self, to_pubkey: str, body: str) -> SendOutcome:
        """Send a DM with ACK retry + flood fallback (delegated to library)."""
        if self._mc is None:
            return SendOutcome.ERROR
        try:
            res = await self._mc.send_msg_with_retry(to_pubkey, body)
        except Exception as e:
            log.warning("send_msg_with_retry raised: %s", e)
            return SendOutcome.ERROR
        return _interpret_send_result(res)

    async def sync_time(self, epoch: int) -> None:
        if self._mc is not None:
            await self._mc.set_time(epoch)

    async def contact_capacity(self) -> tuple[int, int]:
        if self._mc is None:
            return (0, 0)
        try:
            info = await self._mc.send_device_query()
        except Exception:
            return (len(self._contact_cache), len(self._contact_cache))
        used = info.get("contact_count") if isinstance(info, dict) else getattr(info, "contact_count", len(self._contact_cache))
        cap = info.get("contact_capacity") if isinstance(info, dict) else getattr(info, "contact_capacity", used)
        return (int(used or 0), int(cap or 0))

    async def prune_contact(self, pubkey: str) -> None:
        if self._mc is None:
            return
        try:
            await self._mc.remove_contact(pubkey)
        except Exception as e:
            log.warning("remove_contact(%s) failed: %s", pubkey[:8], e)

    # -- internals ------------------------------------------------------------

    async def _refresh_contacts(self) -> None:
        contacts = await self._mc.get_contacts()
        self._contact_cache = {}
        for c in contacts or []:
            pk = _normalise_pubkey(c)
            if pk:
                self._contact_cache[pk] = c

    async def _resolve_prefix(self, prefix: str) -> dict[str, Any] | None:
        prefix = prefix.lower()
        for pk, c in self._contact_cache.items():
            if pk.startswith(prefix):
                return c
        # Cache miss — refresh and try once more.
        try:
            c = await self._mc.get_contact_by_key_prefix(prefix)
            if c is not None:
                pk = _normalise_pubkey(c)
                if pk:
                    self._contact_cache[pk] = c
                return c
        except Exception as e:
            log.debug("get_contact_by_key_prefix(%s): %s", prefix, e)
        await self._refresh_contacts()
        for pk, c in self._contact_cache.items():
            if pk.startswith(prefix):
                return c
        return None

    def _on_contact_msg(self, payload: Any) -> None:
        # The library invokes callbacks; we cannot await here, so schedule it.
        asyncio.create_task(self._handle_contact_msg(payload))

    async def _handle_contact_msg(self, payload: Any) -> None:
        prefix = _get_attr(payload, "pubkey_prefix") or ""
        body = _get_attr(payload, "text") or _get_attr(payload, "body") or ""
        contact = await self._resolve_prefix(prefix)
        if contact is None:
            log.warning("inbound from unresolved prefix %s; dropping", prefix)
            return
        pk = _normalise_pubkey(contact) or ""
        if pk == self._self_pubkey:
            log.debug("ignoring loopback message from self")
            return
        adv_name = _get_attr(contact, "adv_name") or _get_attr(contact, "name")
        await self._events.put(
            TransportEvent(
                type=TransportEventType.CONTACT_MSG_RECV,
                inbound=InboundMessage(
                    pubkey=pk,
                    adv_name=adv_name,
                    body=body,
                    received_at=int(time.time()),
                ),
            )
        )

    def _on_new_contact(self, payload: Any) -> None:
        pk = _normalise_pubkey(payload) or ""
        if pk:
            self._contact_cache[pk] = payload if isinstance(payload, dict) else {"pubkey": pk}
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.NEW_CONTACT, pubkey=pk))
        )

    def _on_advertisement(self, payload: Any) -> None:
        pk = _normalise_pubkey(payload) or ""
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.ADVERTISEMENT, pubkey=pk))
        )

    def _on_connected(self, payload: Any) -> None:
        reconnected = bool(_get_attr(payload, "reconnected") or False)
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.CONNECTED, reconnected=reconnected))
        )
        if reconnected:
            asyncio.create_task(self._refresh_contacts())

    def _on_disconnected(self, payload: Any) -> None:
        asyncio.create_task(self._events.put(TransportEvent(type=TransportEventType.DISCONNECTED)))


def _get_attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _normalise_pubkey(c: Any) -> str | None:
    pk = _get_attr(c, "pubkey") or _get_attr(c, "public_key") or _get_attr(c, "key")
    if pk is None:
        return None
    if isinstance(pk, bytes):
        return pk.hex().lower()
    return str(pk).lower()


def _pubkey_from_self_info(info: Any) -> str:
    pk = _normalise_pubkey(info)
    return pk or ""


def _interpret_send_result(res: Any) -> SendOutcome:
    """Translate library-returned send result into our enum.

    The meshcore library variously returns dicts with a `status` field, or
    a result object with a `success` attribute, or simply a truthy/falsy
    value. We accept all and fail closed.
    """
    if res is True:
        return SendOutcome.OK
    if res is False or res is None:
        return SendOutcome.NO_ACK
    if isinstance(res, dict):
        if res.get("acked") or res.get("success") or res.get("status") in ("ok", "delivered"):
            return SendOutcome.OK
        if res.get("status") in ("no_ack", "timeout"):
            return SendOutcome.NO_ACK
        return SendOutcome.ERROR
    success = _get_attr(res, "success")
    if success is True:
        return SendOutcome.OK
    if success is False:
        return SendOutcome.NO_ACK
    return SendOutcome.ERROR
