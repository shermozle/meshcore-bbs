"""Web dashboard API and HTML UI for the health HTTP server."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import Config
from .db import Database
from .dispatcher import Dispatcher
from .health import Metrics
from .health_state import HEALTH_HEARTBEAT_THRESHOLD, HealthState
from .db import OUTBOUND_PAUSE_SECONDS
from .dispatcher import _fmt_path
from .outbound import OutboundWorker
from .transport.base import Transport

log = logging.getLogger(__name__)

_HISTORY_DAYS = 14
_LOG_TAIL_DEFAULT = 200
_LOG_TAIL_MAX = 2000


@dataclass
class DashboardDeps:
    """Runtime handles exposed to the dashboard API."""

    cfg: Config
    db: Database
    state: HealthState
    dispatcher: Dispatcher
    outbound: OutboundWorker
    transport: Transport
    metrics: Metrics | None
    log_path: str


def _health_problems(state: HealthState, now: float) -> list[str]:
    problems: list[str] = []
    if not state.transport_connected:
        problems.append("transport_disconnected")
    if (now - state.last_event_at) > HEALTH_HEARTBEAT_THRESHOLD and state.last_event_at > 0:
        problems.append("no_recent_events")
    return problems


async def _check_db(db: Database) -> list[str]:
    try:
        cur = await db.execute("SELECT 1")
        await cur.fetchone()
        return []
    except Exception as e:
        return [f"db_error:{e}"]


async def build_status(deps: DashboardDeps) -> dict:
    now = time.time()
    problems = _health_problems(deps.state, now) + await _check_db(deps.db)
    uptime = int(now - deps.dispatcher.started_at)
    last_event_age: int | None = None
    if deps.state.last_event_at > 0:
        last_event_age = int(now - deps.state.last_event_at)

    contacts_used, contacts_cap = 0, 0
    try:
        contacts_used, contacts_cap = await deps.transport.contact_capacity()
    except Exception:
        log.debug("contact_capacity unavailable", exc_info=True)

    queue_pending = await deps.db.outbound_pending_depth()

    return {
        "status": "ok" if not problems else "unhealthy",
        "problems": problems,
        "version": __version__,
        "bbs_name": deps.cfg.bbs.name,
        "location": deps.cfg.weather.location_name,
        "uptime_seconds": uptime,
        "started_at": deps.dispatcher.started_at,
        "self_pubkey_prefix": deps.transport.self_pubkey[:12],
        "transport_connected": deps.state.transport_connected,
        "last_event_age_seconds": last_event_age,
        "contacts_used": contacts_used,
        "contacts_capacity": contacts_cap,
        "outbound_pending": queue_pending,
        "outbound_sends": {
            "attempted": deps.outbound.sends_attempted,
            "succeeded": deps.outbound.sends_succeeded,
            "failed": deps.outbound.sends_failed,
        },
        "metrics_enabled": deps.metrics is not None,
    }


async def build_stats(deps: DashboardDeps) -> dict:
    async def scalar(sql: str) -> int:
        cur = await deps.db.execute(sql)
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    counts = {
        "users": await scalar("SELECT COUNT(*) FROM users"),
        "onboarded": await scalar("SELECT COUNT(*) FROM users WHERE onboarded=1"),
        "banned": await scalar("SELECT COUNT(*) FROM users WHERE banned=1"),
        "boards": await scalar("SELECT COUNT(*) FROM boards"),
        "board_posts": await scalar(
            "SELECT COUNT(*) FROM board_posts WHERE deleted=0"
        ),
        "mail_unread": await scalar(
            "SELECT COUNT(*) FROM mail WHERE read_at IS NULL AND deleted=0"
        ),
        "mail_read": await scalar(
            "SELECT COUNT(*) FROM mail WHERE read_at IS NOT NULL AND deleted=0"
        ),
        "news_items": await scalar("SELECT COUNT(*) FROM news_items"),
        "audit_rows": await scalar("SELECT COUNT(*) FROM audit_log"),
        "rate_limit_buckets": await scalar("SELECT COUNT(*) FROM rate_limits"),
    }

    cur = await deps.db.execute(
        "SELECT status, COUNT(*) AS n FROM outbound_queue GROUP BY status"
    )
    outbound_by_status = {row[0]: int(row[1]) for row in await cur.fetchall()}

    cur = await deps.db.execute(
        "SELECT fetched_at FROM weather_cache ORDER BY fetched_at DESC LIMIT 1"
    )
    row = await cur.fetchone()
    weather_fetched_at = int(row[0]) if row else None

    cur = await deps.db.execute("SELECT COUNT(*) FROM news_feeds WHERE enabled=1")
    row = await cur.fetchone()
    news_feeds_enabled = int(row[0]) if row else 0

    return {
        "counts": counts,
        "outbound_by_status": outbound_by_status,
        "weather_fetched_at": weather_fetched_at,
        "news_feeds_enabled": news_feeds_enabled,
    }


async def build_activity(deps: DashboardDeps) -> dict:
    cur = await deps.db.execute(
        """SELECT
             COALESCE(display_name, substr(pubkey, 1, 8)) AS who,
             substr(pubkey, 1, 12) AS pubkey_prefix,
             msg_count,
             last_seen,
             last_hops,
             onboarded,
             banned
           FROM users
           ORDER BY last_seen DESC
           LIMIT 15"""
    )
    users = [
        {
            "who": r[0],
            "pubkey_prefix": r[1],
            "msg_count": r[2],
            "last_seen": r[3],
            "last_hops": r[4],
            "onboarded": bool(r[5]),
            "banned": bool(r[6]),
        }
        for r in await cur.fetchall()
    ]

    cur = await deps.db.execute(
        """SELECT ts, substr(COALESCE(actor_pubkey, ''), 1, 12) AS actor,
                  action, substr(COALESCE(detail, ''), 1, 80) AS detail
           FROM audit_log
           ORDER BY ts DESC
           LIMIT 25"""
    )
    audit = [
        {"ts": r[0], "actor": r[1] or None, "action": r[2], "detail": r[3]}
        for r in await cur.fetchall()
    ]

    cur = await deps.db.execute(
        """SELECT id, substr(to_pubkey, 1, 12), status, attempts,
                  enqueued_at, priority
           FROM outbound_queue
           WHERE status = 'pending'
           ORDER BY priority DESC, enqueued_at ASC
           LIMIT 20"""
    )
    pending_outbound = [
        {
            "id": r[0],
            "to_prefix": r[1],
            "status": r[2],
            "attempts": r[3],
            "enqueued_at": r[4],
            "priority": r[5],
        }
        for r in await cur.fetchall()
    ]

    return {"recent_users": users, "audit": audit, "pending_outbound": pending_outbound}


async def build_queue(deps: DashboardDeps) -> dict:
    """Full pending outbound queue with paths and message context."""
    messages = await deps.db.list_pending_outbound(limit=100)
    path_cache: dict[str, list[str]] = {}
    name_cache: dict[str, str | None] = {}
    pause_cache: dict[str, int | None] = {}
    items: list[dict] = []
    now = int(time.time())

    for msg in messages:
        pk = msg.to_pubkey
        if pk not in path_cache:
            try:
                path_cache[pk] = await deps.transport.resolve_inbound_path(pk)
            except Exception:
                log.debug("path resolve failed for %s", pk[:12], exc_info=True)
                path_cache[pk] = []
        if pk not in name_cache:
            user = await deps.db.get_user(pk)
            name_cache[pk] = user.display_name if user else None
        if pk not in pause_cache:
            pause_cache[pk] = await deps.db.get_outbound_pause_until(pk)

        if msg.attempts > 0:
            nature = "retry"
        else:
            nature = msg.msg_kind

        path = path_cache[pk]
        paused_until = pause_cache[pk]
        items.append({
            "id": msg.id,
            "to_pubkey_prefix": pk[:12],
            "to_name": name_cache[pk],
            "path": path,
            "path_display": _fmt_path(path) or None,
            "nature": nature,
            "trigger_command": msg.trigger_command,
            "attempts": msg.attempts,
            "priority": msg.priority,
            "enqueued_at": msg.enqueued_at,
            "next_attempt": msg.next_attempt,
            "ready": msg.next_attempt <= now and not paused_until,
            "paused_until": paused_until,
            "body_preview": msg.body[:80],
        })

    return {"pending": items, "depth": len(items)}


async def build_history(deps: DashboardDeps) -> dict:
    since = int(time.time()) - _HISTORY_DAYS * 86400

    cur = await deps.db.execute(
        """SELECT date(last_seen, 'unixepoch') AS day, COUNT(*) AS n
           FROM users
           WHERE last_seen >= ?
           GROUP BY day
           ORDER BY day""",
        (since,),
    )
    active_by_day = [{"day": r[0], "count": int(r[1])} for r in await cur.fetchall()]

    cur = await deps.db.execute(
        """SELECT date(ts, 'unixepoch') AS day, action, COUNT(*) AS n
           FROM audit_log
           WHERE ts >= ?
           GROUP BY day, action
           ORDER BY day""",
        (since,),
    )
    audit_by_day: dict[str, dict[str, int]] = {}
    for day, action, n in await cur.fetchall():
        audit_by_day.setdefault(day, {})[action] = int(n)

    cur = await deps.db.execute(
        """SELECT date(first_seen, 'unixepoch') AS day, COUNT(*) AS n
           FROM users
           WHERE first_seen >= ?
           GROUP BY day
           ORDER BY day""",
        (since,),
    )
    new_users_by_day = [{"day": r[0], "count": int(r[1])} for r in await cur.fetchall()]

    cur = await deps.db.execute(
        """SELECT date(enqueued_at, 'unixepoch') AS day, status, COUNT(*) AS n
           FROM outbound_queue
           WHERE enqueued_at >= ?
           GROUP BY day, status
           ORDER BY day""",
        (since,),
    )
    outbound_by_day: dict[str, dict[str, int]] = {}
    for day, status, n in await cur.fetchall():
        outbound_by_day.setdefault(day, {})[status] = int(n)

    return {
        "days": _HISTORY_DAYS,
        "active_users_by_day": active_by_day,
        "new_users_by_day": new_users_by_day,
        "audit_by_day": audit_by_day,
        "outbound_by_day": outbound_by_day,
    }


def _read_log_tail(path: str, offset: int, max_lines: int) -> tuple[list[str], int, int]:
    """Return (lines, new_offset, file_size). offset is byte position."""
    p = Path(path)
    if not p.is_file():
        return [], 0, 0

    size = p.stat().st_size
    if offset > size:
        offset = size

    with p.open("rb") as f:
        if offset == 0 and max_lines > 0:
            # Tail mode: read last max_lines from file.
            chunk = min(size, 256 * 1024)
            f.seek(max(0, size - chunk))
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()[-max_lines:]
            return lines, size, size

        f.seek(offset)
        data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return lines, size, size


def register_dashboard_routes(app: web.Application, deps: DashboardDeps) -> None:
    """Attach dashboard HTML and JSON API routes to an aiohttp app."""

    async def api_status(_: web.Request) -> web.Response:
        return web.json_response(await build_status(deps))

    async def api_stats(_: web.Request) -> web.Response:
        return web.json_response(await build_stats(deps))

    async def api_activity(_: web.Request) -> web.Response:
        return web.json_response(await build_activity(deps))

    async def api_queue(_: web.Request) -> web.Response:
        return web.json_response(await build_queue(deps))

    def _parse_queue_msg_id(request: web.Request) -> int | None:
        try:
            return int(request.match_info["msg_id"])
        except (KeyError, ValueError):
            return None

    async def _queue_action_response(
        request: web.Request, action: str,
    ) -> web.Response:
        msg_id = _parse_queue_msg_id(request)
        if msg_id is None:
            return web.json_response({"ok": False, "error": "invalid message id"}, status=400)

        if action == "remove":
            msg = await deps.db.cancel_outbound(msg_id)
            if msg is None:
                return web.json_response({"ok": False, "error": "not found"}, status=404)
            await deps.db.audit(
                None, "queue_remove", f"id={msg_id} to={msg.to_pubkey[:12]}",
            )
            return web.json_response({"ok": True, "id": msg_id})

        if action == "move-back":
            msg = await deps.db.move_outbound_to_back(msg_id)
            if msg is None:
                return web.json_response({"ok": False, "error": "not found"}, status=404)
            await deps.db.audit(
                None, "queue_move_back", f"id={msg_id} to={msg.to_pubkey[:12]}",
            )
            return web.json_response({"ok": True, "id": msg_id})

        if action == "pause-user":
            pending = await deps.db.get_pending_outbound(msg_id)
            if pending is None:
                return web.json_response({"ok": False, "error": "not found"}, status=404)
            until = await deps.db.pause_outbound_recipient(pending.to_pubkey)
            await deps.db.audit(
                None,
                "queue_pause_user",
                f"id={msg_id} to={pending.to_pubkey[:12]} until={until} "
                f"seconds={OUTBOUND_PAUSE_SECONDS}",
            )
            return web.json_response({
                "ok": True,
                "id": msg_id,
                "paused_until": until,
                "pause_seconds": OUTBOUND_PAUSE_SECONDS,
            })

        return web.json_response({"ok": False, "error": "unknown action"}, status=400)

    async def api_queue_remove(request: web.Request) -> web.Response:
        return await _queue_action_response(request, "remove")

    async def api_queue_move_back(request: web.Request) -> web.Response:
        return await _queue_action_response(request, "move-back")

    async def api_queue_pause_user(request: web.Request) -> web.Response:
        return await _queue_action_response(request, "pause-user")

    async def api_history(_: web.Request) -> web.Response:
        return web.json_response(await build_history(deps))

    async def api_logs(request: web.Request) -> web.Response:
        try:
            offset = int(request.query.get("offset", "0"))
        except ValueError:
            offset = 0
        try:
            lines_n = int(request.query.get("lines", str(_LOG_TAIL_DEFAULT)))
        except ValueError:
            lines_n = _LOG_TAIL_DEFAULT
        lines_n = max(1, min(lines_n, _LOG_TAIL_MAX))

        path = deps.log_path
        if not path:
            return web.json_response({"lines": [], "offset": 0, "size": 0, "path": None})

        log_lines, new_offset, size = _read_log_tail(path, offset, lines_n)
        return web.json_response({
            "lines": log_lines,
            "offset": new_offset,
            "size": size,
            "path": path,
        })

    async def api_advert(_: web.Request) -> web.Response:
        try:
            await deps.transport.send_advert(flood=True)
        except Exception as e:
            log.warning("manual flood advert failed: %s", e)
            return web.json_response({"ok": False, "error": str(e)}, status=500)
        await deps.db.audit(None, "advert", "source=dashboard flood=1")
        return web.json_response({"ok": True})

    async def api_logs_stream(request: web.Request) -> web.StreamResponse:
        """Server-Sent Events stream of new log lines."""
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await resp.prepare(request)

        path = deps.log_path
        if not path or not Path(path).is_file():
            await resp.write(b"event: error\ndata: no log file\n\n")
            await resp.write_eof()
            return resp

        offset = Path(path).stat().st_size
        try:
            while True:
                if request.transport is None or request.transport.is_closing():
                    break
                lines, offset, _ = _read_log_tail(path, offset, _LOG_TAIL_MAX)
                for line in lines:
                    # SSE requires each line escaped; keep payload single-line.
                    safe = line.replace("\r", "").replace("\n", " ")
                    await resp.write(f"data: {safe}\n\n".encode())
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            await resp.write_eof()
        return resp

    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/advert", api_advert)
    app.router.add_get("/api/stats", api_stats)
    app.router.add_get("/api/activity", api_activity)
    app.router.add_get("/api/queue", api_queue)
    app.router.add_post("/api/queue/{msg_id}/remove", api_queue_remove)
    app.router.add_post("/api/queue/{msg_id}/move-back", api_queue_move_back)
    app.router.add_post("/api/queue/{msg_id}/pause-user", api_queue_pause_user)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/logs", api_logs)
    app.router.add_get("/api/logs/stream", api_logs_stream)

    async def dashboard_index(_: web.Request) -> web.Response:
        html = _load_dashboard_html()
        return web.Response(text=html, content_type="text/html")

    app.router.add_get("/dashboard", dashboard_index)


def _load_dashboard_html() -> str:
    from importlib import resources

    return resources.files("bbs").joinpath("static", "dashboard.html").read_text(encoding="utf-8")
