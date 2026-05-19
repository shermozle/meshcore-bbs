"""Sliding-window rate limiter tests."""

from __future__ import annotations

import time

import pytest

from bbs.rate_limit import RateLimit, RateLimiter


class TestRateLimiter:
    async def test_first_request_allowed(self, db):
        rl = RateLimiter(db)
        d = await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        assert d.allowed

    async def test_under_limit_allowed(self, db):
        rl = RateLimiter(db)
        for _ in range(4):
            d = await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
            assert d.allowed

    async def test_at_limit_allowed(self, db):
        rl = RateLimiter(db)
        for _ in range(5):
            d = await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
            assert d.allowed

    async def test_over_limit_denied(self, db):
        rl = RateLimiter(db)
        for _ in range(5):
            await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        d = await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        assert not d.allowed
        assert d.retry_in_seconds > 0

    async def test_separate_buckets_independent(self, db):
        rl = RateLimiter(db)
        for _ in range(5):
            await rl.check_and_consume("pk1", "bucket_a", RateLimit(5, 60))
        # bucket_b is fresh
        d = await rl.check_and_consume("pk1", "bucket_b", RateLimit(5, 60))
        assert d.allowed

    async def test_separate_pubkeys_independent(self, db):
        rl = RateLimiter(db)
        for _ in range(5):
            await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        # pk2 is fresh
        d = await rl.check_and_consume("pk2", "test", RateLimit(5, 60))
        assert d.allowed

    async def test_window_resets_after_expiry(self, db):
        rl = RateLimiter(db)
        # Use a 1-second window. Fill it and then sleep.
        for _ in range(3):
            await rl.check_and_consume("pk1", "test", RateLimit(3, 1))
        # Wait for the window to expire.
        import asyncio
        await asyncio.sleep(1.1)
        d = await rl.check_and_consume("pk1", "test", RateLimit(3, 1))
        assert d.allowed

    async def test_retry_in_seconds_bounded(self, db):
        rl = RateLimiter(db)
        for _ in range(5):
            await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        d = await rl.check_and_consume("pk1", "test", RateLimit(5, 60))
        assert 1 <= d.retry_in_seconds <= 60
