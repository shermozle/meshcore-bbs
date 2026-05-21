"""Scheduled background jobs.

Each job is a simple `asyncio.Task` looping `sleep -> run`. We deliberately
avoid apscheduler to keep the dependency footprint small (spec §11).
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import Config
from .db import Database
from .services.mail import MailService
from .services.news import NewsService, schedule_news_refresh
from .services.weather import WeatherService
from .transport.base import Transport

log = logging.getLogger(__name__)


async def loop(name: str, interval_seconds: int, fn) -> None:
    """Wrap `fn` in a sleep loop with logged exceptions."""
    # Slight stagger so jobs don't all fire simultaneously at startup.
    await asyncio.sleep(min(30, interval_seconds // 10 or 1))
    while True:
        try:
            await fn()
        except Exception:
            log.exception("scheduled job %s failed", name)
        await asyncio.sleep(interval_seconds)


def start_all(
    cfg: Config,
    db: Database,
    transport: Transport,
    news: NewsService,
    weather: WeatherService,
    mail_svc: MailService,
    enqueue_reply,  # callable: (pubkey, text, priority=1) -> awaitable
) -> list[asyncio.Task]:
    tasks: list[asyncio.Task] = []

    # News refresh — uses its own scheduler so the initial tick happens fast.
    tasks.append(asyncio.create_task(
        schedule_news_refresh(news, cfg.news.refresh_interval_seconds),
        name="news_refresh",
    ))

    # Weather refresh (local default).
    async def wx_job() -> None:
        await weather.summary_for()
    tasks.append(asyncio.create_task(
        loop("weather_refresh", 3600, wx_job), name="weather_refresh"
    ))

    # Mail notify: push "you have mail" to online recipients (rate-limited).
    async def mail_notify_job() -> None:
        recipients = await db.users_with_unread_mail()
        now = int(time.time())
        for pk in recipients:
            user = await db.get_user(pk)
            if user is None or not user.onboarded:
                continue
            if not mail_svc.is_online(user, now=now):
                continue
            if not mail_svc.should_notify(pk, now=now):
                continue
            unread = await db.count_unread(pk)
            await enqueue_reply(pk, f"! {unread} new mail. INBOX to view.", 1)
    tasks.append(asyncio.create_task(
        loop("mail_notify", 300, mail_notify_job), name="mail_notify"
    ))

    # Contact prune: evict idle contacts when capacity is tight.
    async def contact_prune_job() -> None:
        used, cap = await transport.contact_capacity()
        if cap == 0:
            return
        if used > 0.8 * cap:
            log.warning("contact list %d/%d (>80%%); pruning", used, cap)
            # Pick the N least-recently-seen contacts and prune them.
            cutoff = int(time.time()) - cfg.contacts.prune_after_days * 86400
            cur = await db.execute(
                """SELECT pubkey FROM users
                   WHERE last_seen < ? AND banned = 0
                   ORDER BY last_seen ASC LIMIT 10""",
                (cutoff,),
            )
            rows = await cur.fetchall()
            for r in rows:
                await transport.prune_contact(r[0])
    tasks.append(asyncio.create_task(
        loop("contact_prune", 86400, contact_prune_job), name="contact_prune",
    ))

    # DB vacuum and audit/mail purge.
    async def db_vacuum_job() -> None:
        await db.vacuum()
    tasks.append(asyncio.create_task(
        loop("db_vacuum", 86400 * 7, db_vacuum_job), name="db_vacuum",
    ))

    async def audit_prune_job() -> None:
        cutoff = int(time.time()) - 90 * 86400
        await db.purge_old_audit(cutoff)
        await mail_svc.purge_old_read()
    tasks.append(asyncio.create_task(
        loop("audit_prune", 86400, audit_prune_job), name="audit_prune",
    ))

    # Time resync every 6 hours.
    async def time_sync_job() -> None:
        await transport.sync_time(int(time.time()))
    tasks.append(asyncio.create_task(
        loop("time_sync", 6 * 3600, time_sync_job), name="time_sync",
    ))

    return tasks
