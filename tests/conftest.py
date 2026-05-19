"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest_asyncio

from bbs.config import (
    BBSConfig,
    Config,
    ContactsConfig,
    DeviceConfig,
    HealthConfig,
    LimitsConfig,
    LoggingConfig,
    MailConfig,
    MetricsConfig,
    NewsConfig,
    WeatherConfig,
)
from bbs.db import Database
from bbs.dispatcher import Dispatcher
from bbs.outbound import OutboundWorker
from bbs.rate_limit import RateLimiter
from bbs.services.admin import AdminService
from bbs.services.boards import BoardsService
from bbs.services.mail import MailService
from bbs.services.news import NewsService
from bbs.services.weather import WeatherService
from bbs.transport.mock import MockTransport


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Database:
    db_path = tmp_path / "test.db"
    d = Database(str(db_path))
    await d.connect()
    yield d
    await d.close()


@pytest_asyncio.fixture
def cfg() -> Config:
    return Config(
        device=DeviceConfig(),
        bbs=BBSConfig(
            name="TestBBS",
            motd="Test MOTD",
            admin_pubkeys=["a" * 64],  # an admin pubkey for tests
            default_location="Sydney",
            max_msg_chars=200,
        ),
        limits=LimitsConfig(
            inbound_per_hour=100,
            inbound_per_minute=30,
            post_per_hour=20,
            post_per_day=50,
            mail_send_per_day=20,
            outbound_min_interval_ms=0,
            outbound_per_recipient_min_interval_ms=0,
            outbound_queue_max_depth=1000,
        ),
        news=NewsConfig(feeds=[], max_items_per_feed=50, refresh_interval_seconds=900),
        weather=WeatherConfig(),
        contacts=ContactsConfig(),
        mail=MailConfig(
            online_threshold_seconds=900,
            notify_min_interval_seconds=0,  # disable throttle for tests
            read_retention_days=90,
        ),
        health=HealthConfig(),
        metrics=MetricsConfig(),
        logging=LoggingConfig(level="WARNING", path=""),
    )


@pytest_asyncio.fixture
def transport() -> MockTransport:
    return MockTransport(self_pubkey="b" * 64)


@pytest_asyncio.fixture
async def dispatcher(cfg: Config, db: Database, transport: MockTransport) -> Dispatcher:
    rl = RateLimiter(db)
    news = NewsService(db, cfg.news, cfg.weather.user_agent)
    await news.initialise_feeds()
    weather = WeatherService(db, cfg.weather)
    boards = BoardsService(db, cfg.bbs.max_msg_chars)
    mail = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
    admin = AdminService(db, cfg.bbs)
    return Dispatcher(
        cfg=cfg, db=db, transport=transport, rate_limiter=rl,
        news=news, weather=weather, boards=boards, mail=mail,
        admin=admin, started_at=int(time.time()),
    )


@pytest_asyncio.fixture
async def outbound_worker(
    db: Database, transport: MockTransport, cfg: Config
) -> OutboundWorker:
    w = OutboundWorker(db, transport, cfg.limits)
    w.start()
    yield w
    await w.stop(drain_timeout_seconds=2.0)


async def drain_replies(
    dispatcher: Dispatcher, transport: MockTransport, pubkey: str,
    timeout: float = 1.0,
) -> list[str]:
    """Helper: spin the outbound worker briefly and return everything sent to pubkey."""
    worker = OutboundWorker(dispatcher.db, transport, dispatcher.cfg.limits)
    worker.start()
    # Wait long enough for the worker to drain a few items.
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        depth = await dispatcher.db.outbound_pending_depth()
        if depth == 0:
            break
        await asyncio.sleep(0.02)
    await worker.stop(drain_timeout_seconds=1.0)
    return transport.all_sent_to(pubkey)
