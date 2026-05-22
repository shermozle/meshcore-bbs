"""HTTP /health (and optional /metrics) endpoint.

The health endpoint returns 200 when:
  - Transport reports connected.
  - DB is writable.
  - The event loop has serviced an event or scheduled job in the last 10 min.

Hook this into the Unraid container health check.

If `metrics.enabled` is true in config, a Prometheus exposition is served on
a separate port. Counters/gauges are updated from elsewhere in the app via
the `Metrics` singleton.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from aiohttp import web
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)

from .config import HealthConfig, MetricsConfig
from .db import Database
from .health_state import HEALTH_HEARTBEAT_THRESHOLD, HealthState

log = logging.getLogger(__name__)


class Metrics:
    """Container for Prometheus metrics. Owns its own registry so multiple
    instances (e.g. in tests) don't clash."""

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.messages_in = Counter(
            "bbs_messages_in_total", "Total inbound messages", registry=self.registry
        )
        self.messages_out = Counter(
            "bbs_messages_out_total", "Total outbound sends",
            ["outcome"], registry=self.registry,
        )
        self.commands = Counter(
            "bbs_commands_total", "Total commands handled",
            ["verb"], registry=self.registry,
        )
        self.queue_depth = Gauge(
            "bbs_outbound_queue_depth", "Pending outbound rows",
            registry=self.registry,
        )
        self.user_count = Gauge(
            "bbs_users_total", "Total users (onboarded + not)",
            registry=self.registry,
        )
        self.serial_reconnects = Counter(
            "bbs_serial_reconnects_total", "Serial reconnect events",
            registry=self.registry,
        )


async def make_health_app(
    db: Database,
    state: HealthState,
    metrics: Metrics | None,
    dashboard: object | None = None,
) -> web.Application:
    from .dashboard import DashboardDeps, register_dashboard_routes

    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        if dashboard is not None:
            from .dashboard import build_status

            payload = await build_status(dashboard)  # type: ignore[arg-type]
            status_code = 200 if payload["status"] == "ok" else 503
            return web.json_response(payload, status=status_code)

        now = time.time()
        problems: list[str] = []
        if not state.transport_connected:
            problems.append("transport_disconnected")
        if (now - state.last_event_at) > HEALTH_HEARTBEAT_THRESHOLD and state.last_event_at > 0:
            problems.append("no_recent_events")
        try:
            cur = await db.execute("SELECT 1")
            await cur.fetchone()
        except Exception as e:
            problems.append(f"db_error:{e}")

        if problems:
            return web.json_response({"status": "unhealthy", "problems": problems}, status=503)
        return web.json_response({"status": "ok"})

    async def root(_: web.Request) -> web.Response:
        if dashboard is not None:
            raise web.HTTPFound("/dashboard")
        return web.Response(text="meshcore-bbs\n")

    app.router.add_get("/", root)
    app.router.add_get("/health", health)

    if dashboard is not None:
        register_dashboard_routes(app, dashboard)  # type: ignore[arg-type]

    if metrics is not None:
        async def metrics_handler(_: web.Request) -> web.Response:
            body = generate_latest(metrics.registry)
            return web.Response(body=body, content_type=CONTENT_TYPE_LATEST)
        app.router.add_get("/metrics", metrics_handler)

    return app


async def start_health_server(
    cfg: HealthConfig,
    db: Database,
    state: HealthState,
    metrics: Metrics | None,
    dashboard: object | None = None,
) -> web.AppRunner:
    app = await make_health_app(db, state, metrics, dashboard)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.http_host, cfg.http_port)
    await site.start()
    log.info("health server listening on %s:%d", cfg.http_host, cfg.http_port)
    return runner


async def start_metrics_server(
    cfg: MetricsConfig, metrics: Metrics
) -> web.AppRunner | None:
    if not cfg.enabled:
        return None
    app = web.Application()

    async def metrics_handler(_: web.Request) -> web.Response:
        body = generate_latest(metrics.registry)
        return web.Response(body=body, content_type=CONTENT_TYPE_LATEST)

    app.router.add_get("/metrics", metrics_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, cfg.http_host, cfg.http_port)
    await site.start()
    log.info("metrics server listening on %s:%d", cfg.http_host, cfg.http_port)
    return runner
