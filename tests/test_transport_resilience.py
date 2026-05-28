"""Transport resilience helpers (timeouts, radio_available)."""

from __future__ import annotations

import asyncio

import pytest

from bbs.transport.meshcore import MeshCoreTransport, _CMD_DEVICE_QUERY_TIMEOUT


@pytest.mark.asyncio
async def test_run_mc_times_out():
    transport = MeshCoreTransport()

    async def slow() -> str:
        await asyncio.sleep(60)
        return "ok"

    result = await transport._run_mc(slow(), timeout=0.05, label="slow")
    assert result is None


@pytest.mark.asyncio
async def test_run_mc_returns_result():
    transport = MeshCoreTransport()

    async def fast() -> int:
        return 42

    result = await transport._run_mc(fast(), _CMD_DEVICE_QUERY_TIMEOUT, "fast")
    assert result == 42


@pytest.mark.asyncio
async def test_mock_radio_available_lifecycle():
    from bbs.transport.mock import MockTransport

    t = MockTransport()
    assert t.radio_available
    await t.start()
    assert t.radio_available
    await t.stop()
    assert not t.radio_available
