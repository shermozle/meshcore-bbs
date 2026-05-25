"""Real `meshcore_py` transport — updated for meshcore 2.3.x API.

Key differences from the original spec-based stubs:
  - All device commands go through self._mc.commands.* (not self._mc.*)
  - subscribe() takes EventType enum values, not strings
  - Callbacks receive Event objects; message data is in event.payload
  - get_contact_by_key_prefix() is a synchronous dict lookup
  - create_serial() / create_tcp() can return None on connection failure
  - DEVICE_INFO payload has max_contacts (not contact_count/contact_capacity)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .base import InboundMessage, SendOutcome, TransportEvent, TransportEventType

log = logging.getLogger(__name__)


class MeshCoreTransport:
    """Production transport wrapping the meshcore library."""

    def __init__(
        self,
        *,
        connection: str = "serial",
        serial_path: str = "/dev/ttyUSB0",
        baud: int = 115200,
        tcp_host: str = "",
        tcp_port: int = 5000,
        expected_pubkey: str = "",
        max_reconnect_attempts: int = 0,
    ) -> None:
        self.connection = connection
        self.serial_path = serial_path
        self.baud = baud
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.expected_pubkey = expected_pubkey.lower()
        self.max_reconnect_attempts = max_reconnect_attempts

        self._mc: Any = None  # meshcore.MeshCore
        self._events: asyncio.Queue[TransportEvent] = asyncio.Queue()
        self._self_pubkey: str = ""
        self._poll_task: asyncio.Task | None = None
        self._path_cache: dict[str, list[str]] = {}

    @property
    def self_pubkey(self) -> str:
        return self._self_pubkey

    def events(self) -> asyncio.Queue[TransportEvent]:
        return self._events

    async def start(self) -> None:
        # Lazy imports so test environments don't need the library installed.
        from meshcore import EventType, MeshCore  # type: ignore[import-not-found]

        if self.connection == "tcp":
            self._mc = await MeshCore.create_tcp(
                self.tcp_host,
                self.tcp_port,
                auto_reconnect=True,
                max_reconnect_attempts=self.max_reconnect_attempts,
            )
            connect_desc = f"{self.tcp_host}:{self.tcp_port}"
        else:
            self._mc = await MeshCore.create_serial(
                self.serial_path,
                self.baud,
                auto_reconnect=True,
                max_reconnect_attempts=self.max_reconnect_attempts,
            )
            connect_desc = self.serial_path
        if self._mc is None:
            if self.connection == "tcp":
                raise RuntimeError(
                    f"Failed to connect to companion at {connect_desc}. "
                    "Check pyMC companion TCP is enabled, host/port, and that "
                    "no other client holds the connection (one client per companion)."
                )
            raise RuntimeError(
                f"Failed to connect to companion on {connect_desc}. "
                "Check cable, firmware (must be companion, not repeater), "
                "and that no other process holds the port."
            )

        # self_info is populated by create_serial() → connect() → send_appstart().
        self_pubkey = (self._mc.self_info or {}).get("public_key", "")
        if not self_pubkey:
            raise RuntimeError("Could not read self pubkey from companion SELF_INFO")
        self._self_pubkey = self_pubkey.lower()

        if self.expected_pubkey and self.expected_pubkey != self._self_pubkey:
            raise RuntimeError(
                f"Connected device pubkey {self._self_pubkey} != expected "
                f"{self.expected_pubkey}; refusing to run"
            )

        log.info(
            "companion connected (%s) pubkey=%s",
            connect_desc,
            self._self_pubkey[:12],
        )
        await self._mc.commands.set_time(int(time.time()))
        await self._mc.ensure_contacts()

        # Subscribe to firmware events using EventType enums.
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
        self._mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
        self._mc.subscribe(EventType.PATH_UPDATE, self._on_path_update)
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        self._mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log_data)
        self._mc.subscribe(EventType.CONNECTED, self._on_connected)
        self._mc.subscribe(EventType.DISCONNECTED, self._on_disconnected)

        await self._mc.start_auto_message_fetching()

        # Polling fallback: some firmware versions don't reliably send
        # MESSAGES_WAITING push events, so messages accumulate unseen.
        # Poll every 30 s to catch anything the push mechanism missed.
        self._poll_task = asyncio.create_task(
            self._message_poll_loop(), name="msg_poll"
        )

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._mc is not None:
            try:
                await self._mc.disconnect()
            except Exception as e:
                log.warning("error during transport stop: %s", e)
            self._mc = None

    async def _message_poll_loop(self) -> None:
        """Periodically drain the companion's inbound queue as a fallback for
        firmware that doesn't send MESSAGES_WAITING push notifications."""
        while True:
            await asyncio.sleep(30)
            if self._mc is None:
                continue
            try:
                await self._mc.commands.get_msg()
            except Exception as e:
                log.debug("poll get_msg error: %s", e)

    async def send_msg(self, to_pubkey: str, body: str) -> SendOutcome:
        """Send a DM with ACK retry + flood fallback."""
        if self._mc is None:
            return SendOutcome.ERROR
        try:
            res = await self._mc.commands.send_msg_with_retry(to_pubkey, body)
        except Exception as e:
            log.warning("send_msg_with_retry raised: %s", e)
            return SendOutcome.ERROR
        return _interpret_send_result(res)

    async def send_advert(self, *, flood: bool = False) -> None:
        if self._mc is not None:
            try:
                await self._mc.commands.send_advert(flood=flood)
                log.info("advertisement sent (flood=%s)", flood)
            except Exception as e:
                log.warning("send_advert failed: %s", e)

    async def sync_time(self, epoch: int) -> None:
        if self._mc is not None:
            await self._mc.commands.set_time(epoch)

    async def contact_capacity(self) -> tuple[int, int]:
        if self._mc is None:
            return (0, 0)
        used = len(self._mc.contacts)
        try:
            event = await self._mc.commands.send_device_query()
            if not event.is_error():
                # DEVICE_INFO payload uses max_contacts for capacity.
                cap = int(event.payload.get("max_contacts") or used)
                return (used, cap)
        except Exception:
            pass
        return (used, used)

    async def prune_contact(self, pubkey: str) -> None:
        if self._mc is None:
            return
        try:
            await self._mc.commands.remove_contact(pubkey)
        except Exception as e:
            log.warning("remove_contact(%s) failed: %s", pubkey[:8], e)

    async def resolve_inbound_path(self, pubkey: str) -> list[str]:
        """Return the inbound mesh path for *pubkey* (best-effort)."""
        if self._mc is None:
            return []
        pk = pubkey.lower()
        cached = self._path_cache.get(pk)
        if cached is not None:
            return list(cached)

        path = await self._discover_inbound_path(pk)
        if not path:
            path = _path_from_contact(self._mc, pk)
        if path:
            self._path_cache[pk] = list(path)
        return path

    async def _discover_inbound_path(self, pubkey: str) -> list[str]:
        """Ask the companion for the inbound route via path discovery."""
        try:
            event = await self._mc.commands.send_path_discovery_sync(
                pubkey, min_timeout=3.0,
            )
        except Exception as e:
            log.warning("path discovery failed for %s: %s", pubkey[:12], e)
            return []
        if event is None or event.is_error():
            return []

        payload = event.payload if hasattr(event, "payload") else event
        expected = pubkey[:12]
        got = str(_get_attr(payload, "pubkey_pre") or "").lower()
        if got and got != expected:
            log.warning(
                "path discovery prefix mismatch for %s: got %s",
                pubkey[:12], got,
            )

        in_path = _get_attr(payload, "in_path") or ""
        in_hash_len = int(_get_attr(payload, "in_path_hash_len") or 1)
        if in_path:
            return _resolve_path_hex(str(in_path), in_hash_len, self._mc)

        # Some firmware builds only populate the outbound leg; reverse it.
        out_path = _get_attr(payload, "out_path") or ""
        out_hash_len = int(_get_attr(payload, "out_path_hash_len") or 1)
        if out_path:
            nodes = _resolve_path_hex(str(out_path), out_hash_len, self._mc)
            return list(reversed(nodes))
        return []

    # -- internals ------------------------------------------------------------

    def _on_contact_msg(self, event: Any) -> None:
        asyncio.create_task(self._handle_contact_msg(event))

    async def _handle_contact_msg(self, event: Any) -> None:
        # Callbacks receive Event objects; data is in event.payload.
        payload = event.payload if hasattr(event, "payload") else event
        prefix = _get_attr(payload, "pubkey_prefix") or ""
        body = _get_attr(payload, "text") or _get_attr(payload, "body") or ""

        if self._mc is None:
            return
        # get_contact_by_key_prefix is a synchronous dict lookup in 2.3.x.
        contact = self._mc.get_contact_by_key_prefix(prefix)
        if contact is None:
            # Contacts may not be loaded yet — refresh and retry once.
            await self._mc.commands.get_contacts()
            contact = self._mc.get_contact_by_key_prefix(prefix)
        if contact is None:
            log.warning("inbound from unresolved prefix %s; dropping", prefix)
            return
        pk = _normalise_pubkey(contact) or ""
        if pk == self._self_pubkey:
            log.debug("ignoring loopback message from self")
            return
        adv_name = _get_attr(contact, "adv_name") or _get_attr(contact, "name")
        # path_len == 255 means "direct" (0 hops); otherwise it's the relay count.
        path_len = _get_attr(payload, "path_len")
        if path_len is None:
            hops = None
        elif path_len == 255:
            hops = 0
        else:
            hops = int(path_len)
        path = _extract_path(payload, self._mc)
        if path:
            self._path_cache[pk.lower()] = list(path)
        await self._events.put(
            TransportEvent(
                type=TransportEventType.CONTACT_MSG_RECV,
                inbound=InboundMessage(
                    pubkey=pk,
                    adv_name=adv_name,
                    body=body,
                    received_at=int(time.time()),
                    hops=hops,
                    path=path,
                ),
            )
        )

    def _on_new_contact(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        pk = _normalise_pubkey(payload) or ""
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.NEW_CONTACT, pubkey=pk))
        )

    def _on_advertisement(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        pk = _normalise_pubkey(payload) or _pubkey_from_payload_dict(payload) or ""
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.ADVERTISEMENT, pubkey=pk))
        )

    def _on_path_update(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        pk = _pubkey_from_payload_dict(payload) or ""
        asyncio.create_task(self._emit_mesh_activity(pk))

    def _on_channel_msg(self, event: Any) -> None:
        asyncio.create_task(self._handle_channel_msg(event))

    async def _handle_channel_msg(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        pk = _origin_pubkey_from_flood_payload(payload, self._mc) or ""
        await self._emit_mesh_activity(pk)

    def _on_rx_log_data(self, event: Any) -> None:
        asyncio.create_task(self._handle_rx_log_data(event))

    async def _handle_rx_log_data(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        pk = _pubkey_from_rx_log(payload, self._mc) or ""
        await self._emit_mesh_activity(pk)

    async def _emit_mesh_activity(self, pubkey: str) -> None:
        pk = (pubkey or "").lower()
        if not pk or pk == self._self_pubkey:
            return
        await self._events.put(
            TransportEvent(type=TransportEventType.MESH_ACTIVITY, pubkey=pk)
        )

    def _on_connected(self, event: Any) -> None:
        payload = event.payload if hasattr(event, "payload") else event
        reconnected = bool(_get_attr(payload, "reconnected") or False)
        asyncio.create_task(
            self._events.put(TransportEvent(type=TransportEventType.CONNECTED, reconnected=reconnected))
        )
        if reconnected and self._mc is not None:
            asyncio.create_task(self._mc.commands.get_contacts())

    def _on_disconnected(self, event: Any) -> None:
        asyncio.create_task(self._events.put(TransportEvent(type=TransportEventType.DISCONNECTED)))


def _get_attr(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _pubkey_from_payload_dict(payload: Any) -> str | None:
    pk = _get_attr(payload, "public_key") or _get_attr(payload, "pubkey")
    if pk is None:
        return None
    if isinstance(pk, bytes):
        return pk.hex().lower()
    return str(pk).lower()


def _resolve_pubkey_from_prefix(prefix: str, mc: Any) -> str | None:
    """Map a path hash / pubkey prefix to a full contact pubkey."""
    prefix = prefix.lower().strip()
    if not prefix or mc is None:
        return None
    try:
        contact = mc.get_contact_by_key_prefix(prefix)
    except Exception:
        return None
    if contact is None:
        return None
    return _normalise_pubkey(contact)


def _origin_pubkey_from_flood_payload(payload: Any, mc: Any) -> str | None:
    """Best-effort sender pubkey from a multi-hop channel or flood observation."""
    path_len = _get_attr(payload, "path_len")
    if path_len is None or int(path_len) <= 0 or int(path_len) == 255:
        return None
    path_hex = _get_attr(payload, "path")
    if not path_hex:
        return None
    mode = _get_attr(payload, "path_hash_mode")
    hash_len = max(1, int(mode) + 1) if mode is not None else 1
    first_hop = str(path_hex).lower()[: hash_len * 2]
    return _resolve_pubkey_from_prefix(first_hop, mc)


def _pubkey_from_rx_log(payload: Any, mc: Any) -> str | None:
    """Extract a node pubkey from an overheard RF log packet when possible."""
    adv_key = _get_attr(payload, "adv_key")
    if adv_key:
        return str(adv_key).lower()
    path_hex = _get_attr(payload, "path")
    path_len = _get_attr(payload, "path_len")
    if path_hex and path_len and int(path_len) > 0:
        hash_len = int(_get_attr(payload, "path_hash_size") or 1)
        first_hop = str(path_hex).lower()[: hash_len * 2]
        pk = _resolve_pubkey_from_prefix(first_hop, mc)
        if pk:
            return pk
    return None


def _normalise_pubkey(c: Any) -> str | None:
    # meshcore 2.3.x contacts use "public_key"; keep fallbacks for robustness.
    pk = _get_attr(c, "public_key") or _get_attr(c, "pubkey") or _get_attr(c, "key")
    if pk is None:
        return None
    if isinstance(pk, bytes):
        return pk.hex().lower()
    return str(pk).lower()


def _extract_path(payload: Any, mc: Any) -> list[str]:
    """Best-effort relay path from an inbound message payload.

    CONTACT_MSG_RECV normally carries only ``path_len`` (hop count). If the
    firmware ever includes explicit path node IDs, resolve them here.
    """
    raw_path = _get_attr(payload, "path") or _get_attr(payload, "relay_path")
    if not raw_path:
        return []
    if isinstance(raw_path, str):
        hash_len = _get_attr(payload, "path_hash_size") or 1
        return _resolve_path_hex(raw_path, int(hash_len), mc)
    if not isinstance(raw_path, (list, tuple)):
        return []
    result: list[str] = []
    for node in raw_path:
        if isinstance(node, dict):
            node_hash = _get_attr(node, "hash") or ""
            result.append(_resolve_hash_to_name(str(node_hash), mc))
            continue
        node_str = node.hex().lower() if isinstance(node, bytes) else str(node).lower()
        result.append(_resolve_hash_to_name(node_str, mc))
    return result


def _resolve_hash_to_name(node_hash: str, mc: Any) -> str:
    """Map a path hash/prefix to a contact name when possible."""
    node_hash = node_hash.lower().strip()
    if not node_hash:
        return "?"
    if mc is not None:
        try:
            contact = mc.get_contact_by_key_prefix(node_hash)
            if contact is not None:
                name = _get_attr(contact, "adv_name") or _get_attr(contact, "name")
                if name:
                    return str(name)
        except Exception:
            pass
    # Show a short hash label when no contact name is known.
    return node_hash[:8] if len(node_hash) > 8 else node_hash


def _path_from_contact(mc: Any, pubkey: str) -> list[str]:
    """Fallback: use the stored outbound path on the contact (reversed)."""
    contact = mc.contacts.get(pubkey)
    if contact is None:
        contact = mc.get_contact_by_key_prefix(pubkey[:12])
    if contact is None:
        return []
    out_path = _get_attr(contact, "out_path") or ""
    if not out_path:
        return []
    mode = _get_attr(contact, "out_path_hash_mode")
    hash_len = 1 if mode is None or int(mode) < 0 else int(mode) + 1
    nodes = _resolve_path_hex(str(out_path), hash_len, mc)
    return list(reversed(nodes))


def _resolve_path_hex(path_hex: str, hash_len: int, mc: Any) -> list[str]:
    """Split a concatenated path hex string into resolved node labels."""
    path_hex = path_hex.lower().strip()
    if not path_hex:
        return []
    hash_len = max(1, int(hash_len))
    chunk_chars = hash_len * 2
    nodes: list[str] = []
    for offset in range(0, len(path_hex), chunk_chars):
        chunk = path_hex[offset : offset + chunk_chars]
        if len(chunk) < chunk_chars:
            break
        nodes.append(_resolve_hash_to_name(chunk, mc))
    return nodes


def _interpret_send_result(res: Any) -> SendOutcome:
    """Translate send_msg_with_retry result into our SendOutcome enum.

    In meshcore 2.3.x, send_msg_with_retry returns:
      - The MSG_SENT Event on success (ACK received)
      - None when all retries are exhausted without ACK
    """
    if res is None:
        return SendOutcome.NO_ACK
    # Check for Event object (normal 2.3.x path).
    event_type = _get_attr(res, "type")
    if event_type is not None:
        try:
            from meshcore import EventType  # type: ignore[import-not-found]
            if event_type == EventType.MSG_SENT:
                return SendOutcome.OK
            if event_type == EventType.ERROR:
                return SendOutcome.ERROR
            return SendOutcome.NO_ACK
        except ImportError:
            pass
    # Fallback for bool / dict variants (defensive, should not occur in practice).
    if res is True:
        return SendOutcome.OK
    if res is False:
        return SendOutcome.NO_ACK
    if isinstance(res, dict):
        if res.get("acked") or res.get("success") or res.get("status") in ("ok", "delivered"):
            return SendOutcome.OK
        if res.get("status") in ("no_ack", "timeout"):
            return SendOutcome.NO_ACK
    success = _get_attr(res, "success")
    if success is True:
        return SendOutcome.OK
    if success is False:
        return SendOutcome.NO_ACK
    return SendOutcome.ERROR
