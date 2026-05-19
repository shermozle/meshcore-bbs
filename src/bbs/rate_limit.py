"""Sliding-window rate limiter.

Each (pubkey, bucket) pair has a current window start and count. When a
request arrives:
  - If now - window_start >= window_seconds, reset window to (now, 1).
  - Else increment count. If count > limit, deny.

This is a fixed-window approximation, which is the right precision/cost
trade-off for an airtime-limited radio. A precise sliding window would store
each timestamp, which is wasteful for the protection level we need.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .db import Database


@dataclass
class RateLimit:
    limit: int
    window_seconds: int


@dataclass
class Decision:
    allowed: bool
    retry_in_seconds: int = 0


class RateLimiter:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def check_and_consume(self, pubkey: str, bucket: str, limit: RateLimit) -> Decision:
        now = int(time.time())
        window = await self.db.get_rate_window(pubkey, bucket)
        if window is None:
            await self.db.set_rate_window(pubkey, bucket, now, 1)
            return Decision(allowed=True)

        window_start, count = window
        if now - window_start >= limit.window_seconds:
            await self.db.set_rate_window(pubkey, bucket, now, 1)
            return Decision(allowed=True)

        if count + 1 > limit.limit:
            return Decision(
                allowed=False,
                retry_in_seconds=max(1, limit.window_seconds - (now - window_start)),
            )

        await self.db.set_rate_window(pubkey, bucket, window_start, count + 1)
        return Decision(allowed=True)
