"""End-to-end dispatcher tests using the MockTransport.

These exercise the full inbound→dispatch→enqueue path. Outbound delivery is
tested via the helper `drain_replies` which spins an `OutboundWorker` to
drain the queue into the mock transport.
"""

from __future__ import annotations

import time

import pytest

from bbs.transport.mock import MockTransport
from tests.conftest import drain_replies

ALICE_PK = "1" * 64
BOB_PK = "2" * 64


async def _send(
    dispatcher,
    transport: MockTransport,
    pubkey: str,
    body: str,
    *,
    hops: int | None = None,
    path: list[str] | None = None,
) -> list[str]:
    """Inject an inbound message and drain whatever the dispatcher emits."""
    from bbs.transport.base import InboundMessage
    inbound = InboundMessage(
        pubkey=pubkey,
        adv_name=None,
        body=body,
        received_at=int(time.time()),
        hops=hops,
        path=path if path is not None else [],
    )
    await dispatcher.handle_inbound(inbound)
    return await drain_replies(dispatcher, transport, pubkey)


class TestOnboarding:
    async def test_first_message_gets_welcome(self, dispatcher, transport):
        replies = await _send(dispatcher, transport, ALICE_PK, "HELP")
        # First message from an unknown pubkey -> welcome, not HELP output.
        assert len(replies) == 1
        assert "Welcome" in replies[0]
        assert "NAME" in replies[0]

    async def test_name_command_completes_onboarding(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")  # welcome
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "NAME alice")
        assert len(replies) == 1
        assert "OK" in replies[0] and "alice" in replies[0]

    async def test_other_command_after_onboarding_works(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")
        await _send(dispatcher, transport, ALICE_PK, "NAME alice")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "WHOAMI")
        assert len(replies) == 1
        assert "alice" in replies[0]

    async def test_reserved_name_rejected(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "NAME admin")
        assert "Reserved" in replies[0]

    async def test_too_long_name_rejected(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "NAME thisistoolongname")
        assert "too long" in replies[0].lower() or "max" in replies[0].lower()

    async def test_duplicate_name_rejected(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")
        await _send(dispatcher, transport, ALICE_PK, "NAME alice")
        await _send(dispatcher, transport, BOB_PK, "HELP")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, "NAME alice")
        assert "taken" in replies[0].lower()

    async def test_bad_chars_rejected(self, dispatcher, transport):
        await _send(dispatcher, transport, ALICE_PK, "HELP")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "NAME bad name")
        # "bad name" parses as NAME with first arg "bad" — but that's valid.
        # Test with explicitly bad chars instead:
        transport.sent.clear()
        replies = await _send(dispatcher, transport, ALICE_PK, "NAME bad!name")
        assert "Bad chars" in replies[0] or "chars" in replies[0].lower()


async def _onboard(dispatcher, transport: MockTransport, pubkey: str, name: str) -> None:
    await _send(dispatcher, transport, pubkey, "HELP")
    await _send(dispatcher, transport, pubkey, f"NAME {name}")
    transport.sent.clear()


class TestPing:
    async def test_ping_direct(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "PING", hops=0)
        assert len(replies) == 1
        assert "PONG (direct)" in replies[0]
        assert "via" not in replies[0]

    async def test_ping_with_hops_and_path(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(
            dispatcher,
            transport,
            ALICE_PK,
            "PING",
            hops=2,
            path=["NorthRepeater", "SouthRepeater"],
        )
        assert len(replies) == 1
        assert "PONG (2 hops)" in replies[0]
        assert "via NorthRepeater → SouthRepeater" in replies[0]

    async def test_ping_resolves_path_via_transport(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        transport._inbound_paths[ALICE_PK] = ["RelayA", "RelayB"]
        replies = await _send(dispatcher, transport, ALICE_PK, "PING", hops=2)
        assert "via RelayA → RelayB" in replies[0]


class TestHelp:
    async def test_help_after_onboarding(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "HELP")
        assert len(replies) >= 1
        assert "HELP" in replies[0]

    async def test_help_topic(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "HELP NAME")
        assert "NAME" in replies[0]


class TestBoards:
    async def test_no_boards(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "BOARDS")
        assert "No boards" in replies[0]

    async def test_post_and_read(self, dispatcher, transport, db):
        await db.add_board("general", "General chat", int(time.time()))
        await _onboard(dispatcher, transport, ALICE_PK, "alice")

        replies = await _send(dispatcher, transport, ALICE_PK, "POST general Hello world")
        assert "OK" in replies[0]

        replies = await _send(dispatcher, transport, ALICE_PK, "READ general")
        # The reply should include the author name and post body.
        text = "\n".join(replies)
        assert "alice" in text
        assert "Hello world" in text

    async def test_post_to_nonexistent_board(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "POST doesnotexist Hi")
        assert "not found" in replies[0]

    async def test_post_too_long(self, dispatcher, transport, db):
        await db.add_board("general", "x", int(time.time()))
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        long_body = "x" * 300
        replies = await _send(dispatcher, transport, ALICE_PK, f"POST general {long_body}")
        assert "Too long" in replies[0] or "max" in replies[0].lower()


class TestMail:
    async def test_send_to_unknown(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "SEND bob hello")
        assert "No such user" in replies[0]

    async def test_roundtrip(self, dispatcher, transport, db):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        await _onboard(dispatcher, transport, BOB_PK, "bob")

        replies = await _send(dispatcher, transport, ALICE_PK, "SEND bob hi there")
        assert "OK" in replies[0]

        # Bob may receive a "new mail" push notification *and* the MAIL reply.
        # Tolerate either ordering.
        replies = await _send(dispatcher, transport, BOB_PK, "MAIL")
        joined = " | ".join(replies)
        assert "1 unread" in joined or "1 new mail" in joined

        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, "INBOX")
        text = "\n".join(replies)
        assert "alice" in text and "hi there" in text

        # Extract mail id from the reply
        import re
        m = re.search(r"\[(\d+)\]", text)
        assert m
        mail_id = int(m.group(1))

        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, f"READMAIL {mail_id}")
        text = "\n".join(replies)
        assert "alice" in text and "hi there" in text

        # Now mail is read
        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, "MAIL")
        assert "0 unread" in " | ".join(replies)

    async def test_delete_mail(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        await _onboard(dispatcher, transport, BOB_PK, "bob")

        await _send(dispatcher, transport, ALICE_PK, "SEND bob test")
        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, "INBOX")
        import re
        mail_id = int(re.search(r"\[(\d+)\]", "\n".join(replies)).group(1))

        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, f"DELETE {mail_id}")
        assert "deleted" in " | ".join(replies)

        transport.sent.clear()
        replies = await _send(dispatcher, transport, BOB_PK, "MAIL")
        assert "0 unread" in " | ".join(replies)


class TestAdmin:
    """Admin pubkey "a"*64 is set in cfg."""

    ADMIN_PK = "a" * 64

    async def test_non_admin_blocked(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "ADMIN BAN bob")
        # Non-admin gets a generic "unknown" response.
        assert "Unknown" in replies[0] or "?" in replies[0]

    async def test_admin_creates_board(self, dispatcher, transport):
        await _onboard(dispatcher, transport, self.ADMIN_PK, "admin1")
        replies = await _send(dispatcher, transport, self.ADMIN_PK,
                              "ADMIN BOARD ADD swap For swapping things")
        assert "OK" in replies[0] and "swap" in replies[0]

        replies = await _send(dispatcher, transport, self.ADMIN_PK, "BOARDS")
        assert "swap" in replies[0]

    async def test_admin_ban_user(self, dispatcher, transport, db):
        await _onboard(dispatcher, transport, self.ADMIN_PK, "admin1")
        await _onboard(dispatcher, transport, ALICE_PK, "alice")

        replies = await _send(dispatcher, transport, self.ADMIN_PK,
                              f"ADMIN BAN {ALICE_PK[:12]}")
        assert "OK" in replies[0] and "banned" in replies[0].lower()

        # Now alice's commands should be dropped silently.
        transport.sent.clear()
        await _send(dispatcher, transport, ALICE_PK, "WHOAMI")
        # No reply because banned users are dropped.
        assert transport.all_sent_to(ALICE_PK) == []


class TestStatus:
    async def test_status_shows_uptime(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "STATUS")
        assert "v" in replies[0] or "up" in replies[0]


class TestUnknownCommand:
    async def test_unknown_verb(self, dispatcher, transport):
        await _onboard(dispatcher, transport, ALICE_PK, "alice")
        replies = await _send(dispatcher, transport, ALICE_PK, "BOGUS")
        assert "Unknown" in replies[0]


class TestLoopback:
    async def test_self_message_dropped(self, dispatcher, transport):
        """A message from our own pubkey must be dropped silently."""
        from bbs.transport.base import InboundMessage
        inbound = InboundMessage(
            pubkey=transport.self_pubkey,
            adv_name=None,
            body="HELP",
            received_at=int(time.time()),
        )
        await dispatcher.handle_inbound(inbound)
        # No outbound enqueued.
        depth = await dispatcher.db.outbound_pending_depth()
        assert depth == 0
