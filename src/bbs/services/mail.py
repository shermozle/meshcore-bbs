"""User-to-user mail.

Recipients can be addressed by display name (unique) or by pubkey prefix
(>=6 hex chars, returns first match).

"Online" for the purpose of notification means: the recipient has DM'd the
BBS within `online_threshold_seconds` (default 15 min). If online, we push a
notification at SEND time. Otherwise we defer; the periodic `mail_notify`
scheduled job picks it up on the recipient's next interaction, which by then
will have refreshed their `last_seen`.
"""

from __future__ import annotations

import logging
import time

from ..config import MailConfig
from ..db import Database
from ..format import truncate
from ..models import Mail, User

log = logging.getLogger(__name__)

INBOX_PAGE_SIZE = 5
PREVIEW_CHARS = 40


class MailService:
    def __init__(self, db: Database, cfg: MailConfig, max_body_chars: int) -> None:
        self.db = db
        self.cfg = cfg
        self.max_body_chars = max_body_chars
        # Notification throttle, in-memory; survives only the process lifetime
        # which is fine — the scheduled job is the durable path.
        self._last_notify_at: dict[str, float] = {}

    async def resolve_recipient(self, identifier: str) -> User | None:
        if not identifier:
            return None
        # Display-name resolution first; recipient must be onboarded.
        by_name = await self.db.get_user_by_name(identifier)
        if by_name and by_name.onboarded and not by_name.banned:
            return by_name
        # Pubkey-prefix fallback. Require at least 6 hex chars to reduce
        # collision risk.
        cleaned = identifier.lower()
        if len(cleaned) >= 6 and all(c in "0123456789abcdef" for c in cleaned):
            by_prefix = await self.db.get_user_by_prefix(cleaned)
            if by_prefix and by_prefix.onboarded and not by_prefix.banned:
                return by_prefix
        return None

    async def send(self, from_pk: str, recipient_id: str, body: str) -> tuple[bool, str, User | None]:
        recipient = await self.resolve_recipient(recipient_id)
        if recipient is None:
            return False, "! No such user.", None
        body = body.strip()
        if not body:
            return False, "! Empty mail.", None
        if len(body) > self.max_body_chars:
            return False, f"! Too long, max {self.max_body_chars} chars", None
        mail_id = await self.db.add_mail(from_pk, recipient.pubkey, body, int(time.time()))
        return True, f"OK [mail={mail_id}]", recipient

    async def counts_text(self, viewer_pk: str) -> str:
        unread = await self.db.count_unread(viewer_pk)
        total = await self.db.count_total_mail(viewer_pk)
        return f"Mail: {unread} unread, {total} total."

    async def inbox_text(self, viewer_pk: str, page: int = 1) -> str:
        offset = max(0, (page - 1) * INBOX_PAGE_SIZE)
        mails = await self.db.list_mail(viewer_pk, INBOX_PAGE_SIZE, offset)
        if not mails:
            return "Inbox empty." if page == 1 else "No more mail."
        lines: list[str] = []
        for m in mails:
            sender = await self.db.get_user(m.from_pubkey)
            sname = sender.display_name if sender and sender.display_name else m.from_pubkey[:8]
            unread_mark = "*" if m.read_at is None else " "
            preview = truncate(m.body, PREVIEW_CHARS)
            lines.append(f"{unread_mark}[{m.id}] {sname}: {preview}")
        text = "\n".join(lines)
        if len(mails) == INBOX_PAGE_SIZE:
            text += f"\n[more: INBOX {page + 1}]"
        return text

    async def read_mail(self, viewer_pk: str, mail_id: int) -> str:
        m = await self.db.get_mail(mail_id, viewer_pk)
        if m is None:
            return "! Mail not found."
        sender = await self.db.get_user(m.from_pubkey)
        sname = sender.display_name if sender and sender.display_name else m.from_pubkey[:8]
        await self.db.mark_mail_read(mail_id, int(time.time()))
        return f"From {sname}:\n{m.body}"

    async def delete_mail(self, viewer_pk: str, mail_id: int) -> str:
        ok = await self.db.delete_mail(mail_id, viewer_pk)
        return f"OK [mail={mail_id} deleted]" if ok else "! Mail not found."

    def is_online(self, user: User, now: int | None = None) -> bool:
        n = now if now is not None else int(time.time())
        return (n - user.last_seen) <= self.cfg.online_threshold_seconds

    def should_notify(self, recipient_pk: str, now: int | None = None) -> bool:
        n = now if now is not None else time.time()
        last = self._last_notify_at.get(recipient_pk, 0.0)
        if (n - last) < self.cfg.notify_min_interval_seconds:
            return False
        self._last_notify_at[recipient_pk] = n
        return True

    async def purge_old_read(self) -> int:
        if self.cfg.read_retention_days <= 0:
            return 0
        cutoff = int(time.time()) - self.cfg.read_retention_days * 86400
        return await self.db.purge_old_read_mail(cutoff)
