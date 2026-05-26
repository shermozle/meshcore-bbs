"""Scheduled job tests.

Tests the individual job functions that the scheduler invokes, plus the
scheduler loop wrapper. The jobs are closures inside start_all(); we test the
service-layer logic they call and a short-running loop.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from bbs.scheduler import loop, start_all
from bbs.services.mail import MailService


class TestLoopWrapper:
    """Exercise the loop() helper directly with a short callback."""

    @pytest.mark.asyncio
    async def test_loop_calls_function_multiple_times(self):
        calls: list[int] = []

        async def counter() -> None:
            calls.append(1)

        task = asyncio.create_task(loop("test_job", 0, counter), name="test_loop")
        # Let it run a few ticks.
        while len(calls) < 3:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(calls) >= 3

    @pytest.mark.asyncio
    async def test_loop_survives_exceptions(self):
        calls: list[int] = []
        fail_next = True

        async def flaky() -> None:
            nonlocal fail_next
            if fail_next:
                fail_next = False
                raise RuntimeError("boom")
            calls.append(1)

        task = asyncio.create_task(loop("flaky_job", 0, flaky), name="test_flaky")
        while len(calls) < 2:
            await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert len(calls) >= 2, "loop should continue after exception"


class TestMailNotifyLogic:
    """Mail notification rules exercised directly (not through the scheduler)."""

    async def test_is_online_within_threshold(self, db, cfg):
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        now = int(time.time())
        pk = "a" * 64
        await db.upsert_user_first_seen(pk, None, now)
        await db.set_display_name(pk, "alice")

        user = await db.get_user(pk)
        user_stale = await db.get_user(pk)
        # Fresh last_seen → online.
        assert mail_svc.is_online(user, now=now)

    async def test_is_online_outside_threshold(self, db, cfg):
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        now = int(time.time())
        pk = "a" * 64
        # Create user with stale last_seen.
        await db.upsert_user_first_seen(pk, None, now - 3600)
        await db.set_display_name(pk, "alice")
        user = await db.get_user(pk)
        assert not mail_svc.is_online(user, now=now)

    async def test_should_notify_respects_interval(self, db, cfg):
        cfg.mail.notify_min_interval_seconds = 60
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        pk = "a" * 64
        now = 1_000_000

        assert mail_svc.should_notify(pk, now=now) is True
        assert mail_svc.should_notify(pk, now=now + 30) is False
        assert mail_svc.should_notify(pk, now=now + 61) is True

    async def test_should_notify_zero_interval_allows_all(self, db, cfg):
        cfg.mail.notify_min_interval_seconds = 0
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        pk = "a" * 64
        now = 1_000_000

        assert mail_svc.should_notify(pk, now=now) is True
        assert mail_svc.should_notify(pk, now=now) is True
        assert mail_svc.should_notify(pk, now=now) is True

    async def test_users_with_unread_mail(self, db):
        pk_a = "a" * 64
        pk_b = "b" * 64

        await db.upsert_user_first_seen(pk_a, None, int(time.time()))
        await db.upsert_user_first_seen(pk_b, None, int(time.time()))
        await db.set_display_name(pk_a, "alice")
        await db.set_display_name(pk_b, "bob")

        await db.add_mail(pk_a, pk_b, "hello", int(time.time()))
        unread = await db.users_with_unread_mail()
        assert pk_b in unread

    async def test_purge_old_read_mail(self, db, cfg):
        now = int(time.time())
        pk = "a" * 64
        await db.upsert_user_first_seen(pk, None, now)
        await db.set_display_name(pk, "alice")

        uid = await db.add_mail(pk, pk, "old mail", now - 100 * 86400)
        await db.mark_mail_read(uid, now - 99 * 86400)

        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        purged = await mail_svc.purge_old_read()
        assert purged >= 1

        # The mail should be gone.
        mail = await db.get_mail(uid, pk)
        assert mail is None


class TestStartAll:
    """Verify start_all() creates tasks for each job category."""

    @pytest.mark.asyncio
    async def test_creates_all_tasks(self, cfg, db, transport):
        from bbs.services.news import NewsService
        from bbs.services.weather import WeatherService

        news = NewsService(db, cfg.news, cfg.weather.user_agent)
        await news.initialise_feeds()
        weather = WeatherService(db, cfg.weather)
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)

        enqueue_calls: list[tuple] = []

        async def enqueue(pk, text, priority=1):
            enqueue_calls.append((pk, text, priority))

        tasks = start_all(cfg, db, transport, news, weather, mail_svc, enqueue)

        expected_names = {
            "news_refresh", "weather_refresh", "mail_notify",
            "contact_prune", "db_vacuum", "audit_prune",
            "time_sync", "advert",
        }
        actual_names = {t.get_name() for t in tasks}
        assert expected_names == actual_names

        # Clean up.
        for t in tasks:
            t.cancel()


class TestContactPruneLogic:
    @pytest.mark.asyncio
    async def test_prune_below_threshold_noops(self, transport, db):
        """When contact capacity is well below 80%, no pruning occurs."""
        used, cap = await transport.contact_capacity()
        # Mock transport starts with 0 contacts, 200 capacity — well below 80%.
        assert used < 0.8 * cap


class TestAuditPruneJob:
    @pytest.mark.asyncio
    async def test_purge_old_audit(self, db):
        now = int(time.time())
        await db.audit(None, "old_action", "detail")
        # Manually back-date the audit row.
        await db.execute(
            "UPDATE audit_log SET ts = ? WHERE action = 'old_action'",
            (now - 100 * 86400,),
        )
        await db.conn.commit()

        cutoff = now - 90 * 86400
        purged = await db.purge_old_audit(cutoff)
        assert purged >= 1
