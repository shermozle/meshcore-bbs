"""Mail presence from mesh activity (not only BBS DMs)."""

from __future__ import annotations

import asyncio
import time

import pytest

from .test_dispatcher import ALICE_PK, BOB_PK, _onboard, _send


@pytest.mark.asyncio
async def test_mesh_activity_refreshes_last_seen_without_msg_count(dispatcher, transport):
    await _onboard(dispatcher, transport, BOB_PK, "bob")
    user = await dispatcher.db.get_user(BOB_PK)
    assert user is not None
    before_seen = user.last_seen
    before_count = user.msg_count

    await asyncio.sleep(1.1)
    await dispatcher.record_mesh_activity(BOB_PK)

    user = await dispatcher.db.get_user(BOB_PK)
    assert user is not None
    assert user.last_seen > before_seen
    assert user.msg_count == before_count


@pytest.mark.asyncio
async def test_mail_notify_when_online_via_mesh_activity(dispatcher, transport):
    """Unread mail + recent mesh activity (no recent BBS DM) allows notification."""
    await _onboard(dispatcher, transport, ALICE_PK, "alice")
    await _onboard(dispatcher, transport, BOB_PK, "bob")

    await _send(dispatcher, transport, ALICE_PK, "SEND bob ping")

    stale = int(time.time()) - 3600
    await dispatcher.db.conn.execute(
        "UPDATE users SET last_seen = ? WHERE pubkey = ?",
        (stale, BOB_PK),
    )
    await dispatcher.db.conn.commit()
    bob = await dispatcher.db.get_user(BOB_PK)
    assert not dispatcher.mail.is_online(bob)

    await dispatcher.record_mesh_activity(BOB_PK)
    bob = await dispatcher.db.get_user(BOB_PK)
    assert dispatcher.mail.is_online(bob)
    assert dispatcher.mail.should_notify(BOB_PK)
    assert await dispatcher.db.count_unread(BOB_PK) == 1
