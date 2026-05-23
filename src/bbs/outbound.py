"""Persistent outbound queue worker.

The dispatcher enqueues replies into the DB rather than sending directly,
so that:
  - Concurrent requests don't trample each other on the radio.
  - Sends survive crashes.
  - We can throttle globally and per-recipient.

Throttling:
  - At most one send every `outbound_min_interval_ms` globally.
  - At most one send every `outbound_per_recipient_min_interval_ms` to any
    given recipient.

Retry:
  - send returning NO_ACK or ERROR triggers exponential backoff.
  - Retries are moved to the back of the pending queue (same priority tier) so
    other recipients are not starved by a flaky node.
  - After MAX_ATTEMPTS, the row is marked 'failed' and an audit entry written.

Drop:
  - Rows older than `MAX_AGE_SECONDS` while still pending are marked 'dropped'.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import LimitsConfig
from .db import Database
from .transport.base import SendOutcome, Transport

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
MAX_AGE_SECONDS = 24 * 3600
BACKOFF_BASE_SECONDS = 30


class OutboundWorker:
    def __init__(self, db: Database, transport: Transport, limits: LimitsConfig) -> None:
        self.db = db
        self.transport = transport
        self.limits = limits
        self._last_send_at: float = 0.0
        self._last_send_to: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.sends_attempted = 0
        self.sends_succeeded = 0
        self.sends_failed = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbound-worker")

    async def stop(self, drain_timeout_seconds: float = 30.0) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=drain_timeout_seconds)
            except asyncio.TimeoutError:
                log.warning("outbound worker drain timed out after %ss", drain_timeout_seconds)
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _run(self) -> None:
        log.info("outbound worker started")
        try:
            while not self._stop.is_set():
                try:
                    sent_something = await self._tick()
                except Exception:
                    log.exception("outbound worker tick failed")
                    sent_something = False
                # Sleep enough to honour the min-interval if we just sent,
                # else a short poll interval.
                if sent_something:
                    await self._sleep_until_next_send_allowed()
                else:
                    await asyncio.sleep(0.5)
        finally:
            log.info("outbound worker stopped")

    async def _tick(self) -> bool:
        now = int(time.time())
        # Periodically drop stale rows.
        await self.db.drop_stale_outbound(now - MAX_AGE_SECONDS)
        msg = await self.db.claim_next_outbound(now)
        if msg is None:
            return False

        # Per-recipient throttle: if the last send to this recipient was too
        # recent, push next_attempt out and skip this row this tick.
        recip_min_gap = self.limits.outbound_per_recipient_min_interval_ms / 1000
        last_to_recip = self._last_send_to.get(msg.to_pubkey, 0.0)
        if last_to_recip and (time.time() - last_to_recip) < recip_min_gap:
            new_next = int(last_to_recip + recip_min_gap) + 1
            await self.db.reschedule_outbound(msg.id, new_next, msg.attempts)
            return False

        self.sends_attempted += 1
        outcome = await self.transport.send_msg(msg.to_pubkey, msg.body)
        self._last_send_at = time.time()
        self._last_send_to[msg.to_pubkey] = self._last_send_at

        if outcome is SendOutcome.OK:
            await self.db.mark_outbound_sent(msg.id)
            self.sends_succeeded += 1
            log.debug("sent %d to %s (%d bytes)", msg.id, msg.to_pubkey[:8], len(msg.body))
            return True

        # Retry path
        attempts = msg.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            await self.db.mark_outbound_failed(msg.id)
            await self.db.audit(
                None, "outbound_failed",
                f"to={msg.to_pubkey[:12]} outcome={outcome.value} attempts={attempts}",
            )
            self.sends_failed += 1
            log.warning("dropping outbound %d after %d attempts (outcome=%s)",
                        msg.id, attempts, outcome.value)
            return True

        backoff = BACKOFF_BASE_SECONDS * (2 ** (attempts - 1))
        next_at = int(time.time()) + backoff
        await self.db.reschedule_outbound(msg.id, next_at, attempts)
        log.info("retrying outbound %d in %ds (attempt %d/%d, outcome=%s)",
                 msg.id, backoff, attempts, MAX_ATTEMPTS, outcome.value)
        return True

    async def _sleep_until_next_send_allowed(self) -> None:
        gap = self.limits.outbound_min_interval_ms / 1000
        elapsed = time.time() - self._last_send_at
        if elapsed < gap:
            await asyncio.sleep(gap - elapsed)
