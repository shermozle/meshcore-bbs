"""Path resolution and discovery backoff (BBS-17)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from bbs.transport.meshcore import MeshCoreTransport


class _FakeMc:
    is_connected = True
    contacts: dict = {}

    def get_contact_by_key_prefix(self, prefix: str):
        return None


@pytest.mark.asyncio
async def test_resolve_skips_discovery_when_disabled():
    transport = MeshCoreTransport()
    transport._mc = _FakeMc()
    pk = "a" * 64
    discover = AsyncMock(return_value=["Relay"])
    transport._discover_inbound_path = discover  # type: ignore[method-assign]

    assert await transport.resolve_inbound_path(pk, discover=False) == []
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_skips_discovery_for_direct_peer():
    transport = MeshCoreTransport()
    transport._mc = _FakeMc()
    pk = "b" * 64
    transport._direct_peers.add(pk)
    discover = AsyncMock(return_value=["Relay"])
    transport._discover_inbound_path = discover  # type: ignore[method-assign]

    assert await transport.resolve_inbound_path(pk) == []
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_backs_off_after_failed_discovery():
    transport = MeshCoreTransport()
    transport._mc = _FakeMc()
    pk = "c" * 64
    discover = AsyncMock(return_value=[])
    transport._discover_inbound_path = discover  # type: ignore[method-assign]

    assert await transport.resolve_inbound_path(pk) == []
    assert discover.await_count == 1
    assert pk in transport._path_discovery_blocked_until

    assert await transport.resolve_inbound_path(pk) == []
    assert discover.await_count == 1


@pytest.mark.asyncio
async def test_resolve_uses_contact_path_before_discovery():
    transport = MeshCoreTransport()
    pk = "d" * 64
    mc = _FakeMc()
    mc.contacts = {
        pk: {
            "public_key": pk,
            "out_path": "aabbcc",
            "out_path_hash_mode": 2,
        },
    }
    mc.get_contact_by_key_prefix = lambda prefix: mc.contacts.get(pk)  # type: ignore[method-assign]
    transport._mc = mc
    discover = AsyncMock(return_value=[])
    transport._discover_inbound_path = discover  # type: ignore[method-assign]

    path = await transport.resolve_inbound_path(pk)
    assert path  # reversed contact path
    discover.assert_not_called()
    assert transport._path_cache[pk] == path


@pytest.mark.asyncio
async def test_run_mc_serializes_concurrent_commands():
    transport = MeshCoreTransport()
    order: list[str] = []

    async def slow_a() -> None:
        order.append("a_start")
        await asyncio.sleep(0.05)
        order.append("a_end")

    async def slow_b() -> None:
        order.append("b_start")
        await asyncio.sleep(0.01)
        order.append("b_end")

    await asyncio.gather(
        transport._run_mc(slow_a(), 1.0, "a"),
        transport._run_mc(slow_b(), 1.0, "b"),
    )
    assert order == ["a_start", "a_end", "b_start", "b_end"]


@pytest.mark.asyncio
async def test_backoff_expires_and_allows_rediscovery():
    transport = MeshCoreTransport()
    transport._mc = _FakeMc()
    pk = "e" * 64
    discover = AsyncMock(return_value=[])
    transport._discover_inbound_path = discover  # type: ignore[method-assign]

    await transport.resolve_inbound_path(pk)
    transport._path_discovery_blocked_until[pk] = time.time() - 1

    await transport.resolve_inbound_path(pk)
    assert discover.await_count == 2
