"""Outbound queue worker tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from bbs.outbound import OutboundWorker
from bbs.transport.base import SendOutcome
from bbs.transport.mock import MockTransport


class TestOutboundQueue:
    async def test_enqueue_and_send(self, db, transport, cfg):
        await db.enqueue_outbound("pk1", "hello", int(time.time()))
        worker = OutboundWorker(db, transport, cfg.limits)
        worker.start()
        # Spin briefly.
        for _ in range(50):
            await asyncio.sleep(0.02)
            if transport.sent:
                break
        await worker.stop(drain_timeout_seconds=1.0)
        assert ("pk1", "hello") in transport.sent

    async def test_priority_ordering(self, db, transport, cfg):
        now = int(time.time())
        # Enqueue low priority first, then high priority.
        await db.enqueue_outbound("pk1", "low", now, priority=1)
        await db.enqueue_outbound("pk1", "high", now, priority=10)
        worker = OutboundWorker(db, transport, cfg.limits)
        worker.start()
        # Wait for both to drain.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if len(transport.sent) >= 2:
                break
        await worker.stop(drain_timeout_seconds=1.0)
        assert len(transport.sent) == 2
        # High priority should be sent before low.
        assert transport.sent[0][1] == "high"
        assert transport.sent[1][1] == "low"

    async def test_no_ack_retries(self, db, transport, cfg):
        transport.next_send_outcome["pk1"] = SendOutcome.NO_ACK
        msg_id = await db.enqueue_outbound("pk1", "fails", int(time.time()))
        worker = OutboundWorker(db, transport, cfg.limits)
        worker.start()
        # Spin enough for one attempt.
        for _ in range(30):
            await asyncio.sleep(0.05)
            if transport.sent:
                break
        await worker.stop(drain_timeout_seconds=1.0)
        # The send was attempted, but the row should be rescheduled, not sent.
        msg = await db.claim_next_outbound(int(time.time()) + 1_000_000)
        # Either status='pending' with attempts>0, or already marked failed
        # after >MAX_ATTEMPTS attempts. With BACKOFF_BASE=30s, just one attempt
        # is realistic here.
        cur = await db.execute("SELECT attempts, status FROM outbound_queue WHERE id = ?", (msg_id,))
        row = await cur.fetchone()
        assert row[0] >= 1  # at least one attempt
        assert row[1] in ("pending", "failed")

    async def test_drop_stale(self, db):
        # Enqueue with an old timestamp.
        await db.enqueue_outbound("pk1", "old", int(time.time()) - 25 * 3600)
        dropped = await db.drop_stale_outbound(int(time.time()) - 24 * 3600)
        assert dropped == 1

    async def test_queue_depth(self, db):
        for i in range(5):
            await db.enqueue_outbound(f"pk{i}", f"msg{i}", int(time.time()))
        depth = await db.outbound_pending_depth()
        assert depth == 5

    async def test_retry_requeue_goes_to_back(self, db):
        now = int(time.time())
        old_id = await db.enqueue_outbound("pk1", "first", now - 100, priority=10)
        await db.enqueue_outbound("pk2", "second", now, priority=10)
        await db.reschedule_outbound(old_id, now, 1)
        claimed = await db.claim_next_outbound(now)
        assert claimed is not None
        assert claimed.to_pubkey == "pk2"

    async def test_failed_send_lets_other_recipients_through(self, db, transport, cfg):
        """A NO_ACK retry must not block messages to other nodes."""
        transport.next_send_outcome["pk1"] = SendOutcome.NO_ACK
        now = int(time.time())
        await db.enqueue_outbound("pk1", "stuck", now, priority=10)
        await db.enqueue_outbound("pk2", "ok", now, priority=10)
        worker = OutboundWorker(db, transport, cfg.limits)
        worker.start()
        for _ in range(50):
            await asyncio.sleep(0.02)
            if any(pk == "pk2" for pk, _ in transport.sent):
                break
        await worker.stop(drain_timeout_seconds=1.0)
        assert ("pk2", "ok") in transport.sent
        idx_pk2 = transport.sent.index(("pk2", "ok"))
        assert transport.sent[:idx_pk2].count(("pk1", "stuck")) <= 1
