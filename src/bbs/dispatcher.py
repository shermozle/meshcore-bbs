"""Inbound dispatcher.

For every `CONTACT_MSG_RECV` event:
  1. Look up / create the user row.
  2. Check ban state.
  3. Enforce inbound rate limit.
  4. If user is not onboarded, run onboarding flow.
  5. Otherwise, parse and route the command.
  6. Enqueue reply onto the outbound queue.

Replies (and notifications) are always *enqueued*, never sent directly.
"""

from __future__ import annotations

import logging
import time

from . import __version__, commands, onboarding
from .config import Config
from .db import Database
from .format import split_packets
from .rate_limit import Decision, RateLimit, RateLimiter
from .services.admin import AdminService
from .services.boards import BoardsService
from .services.mail import MailService
from .services.news import NewsService
from .services.weather import WeatherService
from .transport.base import InboundMessage, Transport

log = logging.getLogger(__name__)

PRIORITY_NORMAL = 10
PRIORITY_NOTIFICATION = 1  # background pushes


class Dispatcher:
    def __init__(
        self,
        cfg: Config,
        db: Database,
        transport: Transport,
        rate_limiter: RateLimiter,
        news: NewsService,
        weather: WeatherService,
        boards: BoardsService,
        mail: MailService,
        admin: AdminService,
        started_at: int,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.transport = transport
        self.rate_limiter = rate_limiter
        self.news = news
        self.weather = weather
        self.boards = boards
        self.mail = mail
        self.admin = admin
        self.started_at = started_at
        self._reply_command: str | None = None

    async def handle_inbound(self, inbound: InboundMessage) -> None:
        if inbound.pubkey == self.transport.self_pubkey.lower():
            return  # loopback safety

        now = int(time.time())
        user, is_new = await self.db.upsert_user_first_seen(inbound.pubkey, inbound.adv_name, now)
        await self.db.touch_user(inbound.pubkey, now, hops=inbound.hops)

        display = user.display_name or inbound.adv_name or inbound.pubkey[:12]
        hops_str = f"hops={inbound.hops}" if inbound.hops is not None else "hops=?"
        log.info("inbound from %s (%s) %s: %r", display, inbound.pubkey[:12], hops_str,
                 inbound.body[:80])

        if user.banned:
            log.info("dropping inbound from banned user %s", inbound.pubkey[:12])
            return

        if is_new:
            await self._notify_admins_new_user(display, inbound.pubkey)

        is_admin = self.admin.is_admin(inbound.pubkey)

        # Inbound rate limit (sliding-window). Direct (hops=0) and admins bypass.
        # 1 hop → 4× limit, 2 hops → 2×, 3+ hops → base limit.
        if not is_admin and inbound.hops != 0:
            mul = _hop_multiplier(inbound.hops)
            for bucket, base_limit in (
                ("inbound_min", RateLimit(self.cfg.limits.inbound_per_minute, 60)),
                ("inbound_hour", RateLimit(self.cfg.limits.inbound_per_hour, 3600)),
            ):
                scaled = RateLimit(base_limit.limit * mul, base_limit.window_seconds)
                decision = await self.rate_limiter.check_and_consume(inbound.pubkey, bucket, scaled)
                if not decision.allowed:
                    await self._reply_throttled(inbound.pubkey, decision)
                    return

        # Onboarding gate: any user without a display name set goes through
        # the onboarding state machine first.
        if not user.onboarded:
            await self._handle_onboarding(inbound, user.motd_sent)
            return

        # Parse and dispatch the command.
        parsed = commands.parse(inbound.body)
        if parsed is None:
            await self._enqueue_reply(inbound.pubkey, "? Empty. Try: HELP")
            return

        await self._handle_command(inbound, parsed)

    async def record_mesh_activity(self, pubkey: str | None) -> None:
        """Mark a known BBS user active on the mesh (mail notification presence)."""
        if not pubkey:
            return
        pk = pubkey.lower()
        if pk == self.transport.self_pubkey.lower():
            return
        now = int(time.time())
        if await self.db.touch_user_activity(pk, now):
            log.debug("mesh activity from %s", pk[:12])

    async def _reply_throttled(self, pubkey: str, decision: Decision) -> None:
        # Throttled reply: at most one per minute to avoid feedback loops.
        # We approximate this by piggybacking on the rate limiter itself.
        notify_decision = await self.rate_limiter.check_and_consume(
            pubkey, "rate_notify", RateLimit(1, 60)
        )
        if notify_decision.allowed:
            await self._enqueue_reply(
                pubkey,
                f"! Rate limited. Try again in {decision.retry_in_seconds}s.",
            )

    # -- onboarding -----------------------------------------------------------

    async def _handle_onboarding(self, inbound: InboundMessage, motd_sent: bool) -> None:
        # If this is the very first message we've seen and motd_sent is 0,
        # send the welcome regardless of body content.
        parsed = commands.parse(inbound.body)
        if parsed is None or parsed.verb != "NAME":
            # Send welcome (or reminder) and stop.
            await self._enqueue_reply(
                inbound.pubkey, onboarding.welcome_text(self.cfg.bbs.name)
            )
            return

        name = parsed.args[0] if parsed.args else ""
        if not name:
            await self._enqueue_reply(inbound.pubkey, "! NAME <yourname>")
            return

        ok, reply = await onboarding.try_set_name(self.db, inbound.pubkey, name, self.cfg.bbs)
        await self._enqueue_reply(inbound.pubkey, reply)

    # -- command dispatch -----------------------------------------------------

    async def _handle_command(
        self, inbound: InboundMessage, parsed: commands.ParsedCommand
    ) -> None:
        v = parsed.verb
        pk = inbound.pubkey
        user = await self.db.get_user(pk)
        user_display = (user.display_name if user else None) or inbound.adv_name or pk[:12]

        log.info("cmd %s from %s (%s)", v, user_display, pk[:12])

        self._reply_command = v
        try:
            if v == "HELP":
                topic = parsed.args[0] if parsed.args else None
                await self._enqueue_reply(pk, commands.help_text(topic))
            elif v == "WHO":
                await self._handle_who(pk)
            elif v == "PING":
                await self._handle_ping(inbound)
            elif v == "WHOAMI":
                await self._handle_whoami(pk)
            elif v == "NAME":
                await self._handle_name(pk, parsed)
            elif v == "NEWS":
                await self._handle_news(pk, parsed)
            elif v == "WX":
                await self._handle_wx(pk, parsed)
            elif v == "BOARDS":
                await self._enqueue_reply(pk, await self.boards.list_text())
            elif v == "READ":
                await self._handle_read(pk, parsed)
            elif v == "POST":
                await self._handle_post(pk, parsed)
            elif v == "MAIL":
                await self._enqueue_reply(pk, await self.mail.counts_text(pk))
            elif v == "INBOX":
                page = _maybe_int(parsed.args, 0, default=1)
                await self._enqueue_reply(pk, await self.mail.inbox_text(pk, page))
            elif v == "READMAIL":
                await self._handle_readmail(pk, parsed)
            elif v == "SEND":
                await self._handle_send(pk, parsed)
            elif v == "DELETE":
                await self._handle_delete_mail(pk, parsed)
            elif v == "STATUS":
                await self._handle_status(pk)
            elif v == "ADVERT":
                await self._handle_advert(pk)
            elif v.startswith("ADMIN"):
                await self._handle_admin(pk, parsed)
            else:
                await self._enqueue_reply(pk, "? Unknown command. Try: HELP")
        except Exception:
            log.exception("command handler crashed: verb=%s", v)
            await self._enqueue_reply(pk, "! Internal error.")
        finally:
            self._reply_command = None

    # -- individual handlers --------------------------------------------------

    async def _handle_whoami(self, pk: str) -> None:
        user = await self.db.get_user(pk)
        if user is None:
            await self._enqueue_reply(pk, "? Unknown.")
            return
        name = user.display_name or "(unset)"
        await self._enqueue_reply(pk, f"{name} {pk[:12]}")

    async def _handle_who(self, pk: str) -> None:
        users = await self.db.recent_active_users(5)
        if not users:
            await self._enqueue_reply(pk, "No active users yet.")
            return
        now = int(time.time())
        lines = []
        for u in users:
            name = u.display_name or u.adv_name or u.pubkey[:8]
            age = _fmt_age(now - u.last_seen)
            hops_str = f" {u.last_hops}hop" if u.last_hops is not None else ""
            lines.append(f"{name} ({age}{hops_str})")
        await self._enqueue_reply(pk, "\n".join(lines))

    async def _handle_ping(self, inbound: InboundMessage) -> None:
        if inbound.hops is None:
            hops_str = "?"
        elif inbound.hops == 0:
            hops_str = "direct"
        else:
            hops_str = f"{inbound.hops} hop{'s' if inbound.hops != 1 else ''}"

        # Direct (0 hops) messages carry no relay path; do not substitute a
        # cached or discovered route — that is outbound routing, not this RX.
        if inbound.hops == 0:
            path: list[str] = []
        else:
            path = list(inbound.path)
            if not path:
                path = await self.transport.resolve_inbound_path(inbound.pubkey)

        path_str = _fmt_path(path)
        if path_str:
            await self._enqueue_reply(
                inbound.pubkey, f"PONG ({hops_str}) via {path_str}",
            )
        else:
            await self._enqueue_reply(inbound.pubkey, f"PONG ({hops_str})")

    async def _notify_admins_new_user(self, display: str, pubkey: str) -> None:
        msg = f"New user: {display} ({pubkey[:12]})"
        for admin_pk in self.cfg.bbs.admin_pubkeys:
            if admin_pk.lower() != pubkey.lower():
                await self._enqueue_reply(admin_pk.lower(), msg, priority=PRIORITY_NOTIFICATION)

    async def _handle_name(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not parsed.args:
            await self._enqueue_reply(pk, "! NAME <new>")
            return
        new_name = parsed.args[0]
        ok, reply = await onboarding.try_set_name(self.db, pk, new_name, self.cfg.bbs)
        await self._enqueue_reply(pk, reply)

    async def _handle_news(self, pk: str, parsed: commands.ParsedCommand) -> None:
        # First positional arg can be either a feed slug or a page number.
        feed: str | None = None
        page = 1
        for a in parsed.args:
            if a.isdigit():
                page = max(1, int(a))
            else:
                feed = a.lower()
        # NEWS pagination is naive — 5 per page, ordered newest first.
        per_page = 5
        # We fetch one page worth + skip for offset by over-fetching.
        items_needed = per_page * page
        items = await self.db.recent_news(items_needed, feed_slug=feed)
        page_items = items[(page - 1) * per_page : page * per_page]
        if not page_items:
            await self._enqueue_reply(
                pk, "No news." if page == 1 else f"No more news (page {page})."
            )
            return
        lines = [f"[{(page - 1) * per_page + i + 1}] {item.title[:100]}" for i, item in enumerate(page_items)]
        if len(items) > page * per_page:
            cmd = f"NEWS {feed} {page + 1}" if feed else f"NEWS {page + 1}"
            lines.append(f"[more: {cmd}]")
        await self._enqueue_reply(pk, "\n".join(lines))

    async def _handle_wx(self, pk: str, parsed: commands.ParsedCommand) -> None:
        # Cache-miss path could trigger an HTTP fetch — rate limit specifically.
        # Best-effort: if the cache is fresh, skip the per-minute limit.
        location = parsed.args[0] if parsed.args else None
        # Apply a per-min HTTP rate limit, but only enforce if there's a cache miss.
        text = await self.weather.summary_for(location)
        await self._enqueue_reply(pk, text)

    async def _handle_read(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not parsed.args:
            await self._enqueue_reply(pk, "! READ <board> [page]")
            return
        slug = parsed.args[0]
        page = _maybe_int(parsed.args, 1, default=1)
        await self._enqueue_reply(pk, await self.boards.read_text(slug, page))

    async def _handle_post(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not parsed.args:
            await self._enqueue_reply(pk, "! POST <board> <text>")
            return
        # Per-resource rate limits for POST.
        for bucket, limit in (
            ("post_hour", RateLimit(self.cfg.limits.post_per_hour, 3600)),
            ("post_day", RateLimit(self.cfg.limits.post_per_day, 86400)),
        ):
            d = await self.rate_limiter.check_and_consume(pk, bucket, limit)
            if not d.allowed:
                await self._enqueue_reply(pk, f"! Rate limited. Try again in {d.retry_in_seconds}s.")
                return
        slug = parsed.args[0]
        # `rest` includes `<slug> <text...>`; strip the slug.
        body = parsed.rest
        if body.lower().startswith(slug.lower()):
            body = body[len(slug):].strip()
        if not body:
            await self._enqueue_reply(pk, "! POST <board> <text>")
            return
        await self._enqueue_reply(pk, await self.boards.post(slug, pk, body))

    async def _handle_readmail(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not parsed.args or not parsed.args[0].isdigit():
            await self._enqueue_reply(pk, "! READMAIL <id>")
            return
        mail_id = int(parsed.args[0])
        await self._enqueue_reply(pk, await self.mail.read_mail(pk, mail_id))

    async def _handle_send(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if len(parsed.args) < 1 or not parsed.rest:
            await self._enqueue_reply(pk, "! SEND <user> <text>")
            return
        # Daily mail rate limit.
        d = await self.rate_limiter.check_and_consume(
            pk, "mail_day", RateLimit(self.cfg.limits.mail_send_per_day, 86400)
        )
        if not d.allowed:
            await self._enqueue_reply(pk, f"! Rate limited. Try again in {d.retry_in_seconds}s.")
            return
        recipient_id = parsed.args[0]
        # `rest` is "<recipient> <body>"; strip recipient.
        body = parsed.rest
        if body.lower().startswith(recipient_id.lower()):
            body = body[len(recipient_id):].strip()
        if not body:
            await self._enqueue_reply(pk, "! SEND <user> <text>")
            return
        ok, reply, recipient = await self.mail.send(pk, recipient_id, body)
        await self._enqueue_reply(pk, reply)
        if ok and recipient is not None:
            await self._maybe_notify_recipient(recipient.pubkey)

    async def _maybe_notify_recipient(self, recipient_pk: str) -> None:
        recipient = await self.db.get_user(recipient_pk)
        if recipient is None or not recipient.onboarded:
            return
        if not self.mail.is_online(recipient):
            return  # deferred to the scheduled job
        if not self.mail.should_notify(recipient_pk):
            return
        unread = await self.db.count_unread(recipient_pk)
        await self._enqueue_reply(
            recipient_pk, f"! {unread} new mail. INBOX to view.", priority=PRIORITY_NOTIFICATION
        )

    async def _handle_delete_mail(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not parsed.args or not parsed.args[0].isdigit():
            await self._enqueue_reply(pk, "! DELETE <id>")
            return
        mail_id = int(parsed.args[0])
        await self._enqueue_reply(pk, await self.mail.delete_mail(pk, mail_id))

    async def _handle_status(self, pk: str) -> None:
        uptime = int(time.time()) - self.started_at
        depth = await self.db.outbound_pending_depth()
        await self._enqueue_reply(
            pk,
            f"v{__version__} up {_fmt_uptime(uptime)} q={depth}",
        )

    async def _handle_advert(self, pk: str) -> None:
        if not self.admin.is_admin(pk):
            await self._enqueue_reply(pk, "? Unknown command. Try: HELP")
            return
        await self.transport.send_advert(flood=True)
        await self.db.audit(pk, "advert", "flood=1")
        await self._enqueue_reply(pk, "OK flood advert sent")

    async def _handle_admin(self, pk: str, parsed: commands.ParsedCommand) -> None:
        if not self.admin.is_admin(pk):
            await self._enqueue_reply(pk, "? Unknown command. Try: HELP")
            return

        v = parsed.verb
        if v == "ADMIN BAN":
            if not parsed.args:
                await self._enqueue_reply(pk, "! ADMIN BAN <prefix>")
                return
            reply = await self.admin.ban(pk, parsed.args[0], " ".join(parsed.args[1:]))
            await self._enqueue_reply(pk, reply)
        elif v == "ADMIN UNBAN":
            if not parsed.args:
                await self._enqueue_reply(pk, "! ADMIN UNBAN <prefix>")
                return
            reply = await self.admin.unban(pk, parsed.args[0])
            await self._enqueue_reply(pk, reply)
        elif v == "ADMIN BOARD ADD":
            if len(parsed.args) < 1:
                await self._enqueue_reply(pk, "! ADMIN BOARD ADD <slug> <desc>")
                return
            slug = parsed.args[0]
            desc = parsed.rest
            if desc.lower().startswith(slug.lower()):
                desc = desc[len(slug):].strip()
            reply = await self.boards.add_board(slug, desc)
            await self._enqueue_reply(pk, reply)
        elif v == "ADMIN BOARD DEL":
            if not parsed.args:
                await self._enqueue_reply(pk, "! ADMIN BOARD DEL <slug>")
                return
            reply = await self.boards.delete_board(parsed.args[0])
            await self._enqueue_reply(pk, reply)
        elif v == "ADMIN BROADCAST":
            # Two-step: stage, then confirm with `ADMIN BROADCAST CONFIRM`.
            if parsed.args and parsed.args[0].upper() == "CONFIRM":
                status, recipients, body = await self.admin.confirm_broadcast(pk)
                await self._enqueue_reply(pk, status)
                for r in recipients:
                    if r != pk:
                        await self._enqueue_reply(r, body, priority=PRIORITY_NOTIFICATION)
            else:
                text = parsed.rest.strip()
                if not text:
                    await self._enqueue_reply(pk, "! ADMIN BROADCAST <text>")
                    return
                await self._enqueue_reply(pk, self.admin.stage_broadcast(pk, text))
        else:
            await self._enqueue_reply(pk, "! ADMIN BAN|UNBAN|BOARD ADD|BOARD DEL|BROADCAST")

    # -- enqueueing -----------------------------------------------------------

    async def _enqueue_reply(
        self,
        pk: str,
        text: str,
        priority: int = PRIORITY_NORMAL,
        *,
        trigger_command: str | None = None,
    ) -> None:
        depth = await self.db.outbound_pending_depth()
        if depth >= self.cfg.limits.outbound_queue_max_depth and priority < PRIORITY_NORMAL:
            log.warning("outbound queue depth %d; dropping low-priority msg to %s",
                        depth, pk[:8])
            return
        cmd = trigger_command if trigger_command is not None else self._reply_command
        msg_kind = "notification" if priority < PRIORITY_NORMAL else "response"
        packets = split_packets(text)
        now = int(time.time())
        for p in packets:
            await self.db.enqueue_outbound(
                pk, p, now, priority=priority, trigger_command=cmd, msg_kind=msg_kind,
            )


def _maybe_int(args: list[str], idx: int, default: int) -> int:
    if idx < len(args) and args[idx].isdigit():
        return int(args[idx])
    return default


def _fmt_path(path: list[str]) -> str:
    """Format a mesh path as human-readable node names or short hash labels."""
    if not path:
        return ""
    return " → ".join(p[:16] if len(p) > 16 else p for p in path)


def _hop_multiplier(hops: int | None) -> int:
    """Return rate-limit multiplier based on hop count.

    Higher multiplier = more permissive (caller multiplies configured limit).
    hops=None (unknown) treated as 1 hop.
    """
    if hops is None or hops == 1:
        return 4
    if hops == 2:
        return 2
    return 1


def _fmt_age(secs: int) -> str:
    if secs < 120:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}min ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _fmt_uptime(secs: int) -> str:
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins}m"
    return f"{mins}m"
