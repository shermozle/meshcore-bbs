"""Application entry point.

`python -m bbs` starts the BBS.

The boot sequence (spec §10.1):
  1. Open SQLite, run migrations.
  2. Start transport (real or mock per --mock).
  3. sync_time, warm contacts cache (transport-internal).
  4. Initialise services.
  5. Start outbound queue worker.
  6. Start scheduled jobs.
  7. Start health endpoint.
  8. Subscribe to event queue and dispatch.

SIGTERM / SIGINT drains the queue (with a 30s budget) and shuts down cleanly.
SIGHUP reloads config in place.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config
from .db import Database, open_db
from .dispatcher import Dispatcher
from .health import HealthState, Metrics, start_health_server, start_metrics_server
from .log import configure_logging
from .outbound import OutboundWorker
from .rate_limit import RateLimiter
from .scheduler import start_all as start_scheduled_jobs
from .services.admin import AdminService
from .services.boards import BoardsService
from .services.mail import MailService
from .services.news import NewsService
from .services.weather import WeatherService
from .transport.base import Transport, TransportEventType
from .transport.mock import MockTransport

log = logging.getLogger(__name__)


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meshcore-bbs", description="MeshCore BBS")
    p.add_argument(
        "--config",
        default=os.environ.get("BBS_CONFIG", "/data/config.yaml"),
        help="Path to config.yaml (default: $BBS_CONFIG or /data/config.yaml)",
    )
    p.add_argument(
        "--db",
        default=os.environ.get("BBS_DB", "/data/bbs.db"),
        help="Path to SQLite DB (default: $BBS_DB or /data/bbs.db)",
    )
    p.add_argument(
        "--mock",
        action="store_true",
        help="Use the in-memory mock transport (no hardware required)",
    )
    return p


async def make_transport(cfg: Config, use_mock: bool) -> Transport:
    if use_mock:
        log.warning("starting with MOCK transport — no hardware will be touched")
        return MockTransport()
    # Lazy import: if --mock is used, we never need the meshcore library.
    from .transport.meshcore import MeshCoreTransport

    return MeshCoreTransport(
        serial_path=cfg.device.serial_path,
        baud=cfg.device.baud,
        expected_pubkey=cfg.device.expected_pubkey,
    )


async def run(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config)
    configure_logging(cfg.logging)
    log.info("starting meshcore-bbs, config=%s db=%s", args.config, args.db)

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)

    async with open_db(args.db) as db:
        transport = await make_transport(cfg, args.mock)
        await transport.start()

        health_state = HealthState(transport_connected=True, last_event_at=time.time())
        metrics = Metrics() if cfg.metrics.enabled else None

        rate_limiter = RateLimiter(db)
        news = NewsService(db, cfg.news, cfg.weather.user_agent)
        await news.initialise_feeds()
        weather = WeatherService(db, cfg.weather)
        boards = BoardsService(db, cfg.bbs.max_msg_chars)
        mail_svc = MailService(db, cfg.mail, cfg.bbs.max_msg_chars)
        admin_svc = AdminService(db, cfg.bbs)

        started_at = int(time.time())
        dispatcher = Dispatcher(
            cfg=cfg,
            db=db,
            transport=transport,
            rate_limiter=rate_limiter,
            news=news,
            weather=weather,
            boards=boards,
            mail=mail_svc,
            admin=admin_svc,
            started_at=started_at,
        )

        outbound = OutboundWorker(db, transport, cfg.limits)
        outbound.start()

        # Scheduler needs to be able to enqueue notifications.
        async def enqueue_reply(pk: str, text: str, priority: int = 1) -> None:
            await dispatcher._enqueue_reply(pk, text, priority=priority)  # noqa: SLF001

        sched_tasks = start_scheduled_jobs(
            cfg, db, transport, news, weather, mail_svc, enqueue_reply,
        )

        health_runner = await start_health_server(cfg.health, db, health_state, metrics)
        metrics_runner = await start_metrics_server(cfg.metrics, metrics) if metrics else None

        await db.audit(None, "startup", f"version={__version__} pubkey={transport.self_pubkey[:12]}")
        log.info("BBS ready. self_pubkey=%s", transport.self_pubkey[:12])

        # Signal handling.
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _sig_handler(signame: str) -> None:
            log.info("received %s, shutting down", signame)
            stop_event.set()

        def _sighup_handler() -> None:
            log.info("SIGHUP received, reloading config")
            cfg.reload()

        for sig in ("SIGTERM", "SIGINT"):
            try:
                loop.add_signal_handler(getattr(signal, sig), lambda s=sig: _sig_handler(s))
            except NotImplementedError:
                pass  # Windows / no signal support
        try:
            loop.add_signal_handler(signal.SIGHUP, _sighup_handler)
        except (AttributeError, NotImplementedError):
            pass

        # Main event-pump loop.
        event_queue = transport.events()
        pump_task = asyncio.create_task(
            _event_pump(event_queue, dispatcher, health_state, metrics),
            name="event_pump",
        )

        await stop_event.wait()

        log.info("draining outbound queue and stopping")
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        for t in sched_tasks:
            t.cancel()
        await outbound.stop(drain_timeout_seconds=30.0)
        await transport.stop()
        await health_runner.cleanup()
        if metrics_runner is not None:
            await metrics_runner.cleanup()
        await db.audit(None, "shutdown", "")
        log.info("shutdown complete")
        return 0


async def _event_pump(
    queue: asyncio.Queue, dispatcher: Dispatcher,
    health_state: HealthState, metrics: Metrics | None,
) -> None:
    while True:
        event = await queue.get()
        health_state.last_event_at = time.time()
        try:
            if event.type == TransportEventType.CONTACT_MSG_RECV and event.inbound is not None:
                if metrics is not None:
                    metrics.messages_in.inc()
                await dispatcher.handle_inbound(event.inbound)
            elif event.type == TransportEventType.CONNECTED:
                health_state.transport_connected = True
                if event.reconnected and metrics is not None:
                    metrics.serial_reconnects.inc()
                log.info("transport connected (reconnected=%s)", event.reconnected)
            elif event.type == TransportEventType.DISCONNECTED:
                health_state.transport_connected = False
                log.warning("transport disconnected")
            elif event.type == TransportEventType.NEW_CONTACT:
                log.debug("new contact: %s", (event.pubkey or "")[:12])
            elif event.type == TransportEventType.ADVERTISEMENT:
                # Touch last_seen if known.
                if event.pubkey:
                    user = await dispatcher.db.get_user(event.pubkey)
                    if user is not None:
                        await dispatcher.db.touch_user(event.pubkey, int(time.time()))
        except Exception:
            log.exception("event handler crashed for %s", event.type)


def main() -> int:
    args = build_argparser().parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("fatal error in main")
        return 1


if __name__ == "__main__":
    sys.exit(main())
