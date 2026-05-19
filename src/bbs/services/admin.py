"""Admin commands.

Admins are identified by full pubkey listed in `bbs.admin_pubkeys`. Admin
commands are dispatched only when the caller's pubkey is in that list (the
check is performed by the dispatcher; this module trusts the caller).

BROADCAST is rate-limited and requires a two-step confirmation flow so a
fat-finger can't spray the entire user base.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from ..config import BBSConfig
from ..db import Database

log = logging.getLogger(__name__)


@dataclass
class PendingBroadcast:
    text: str
    created_at: int


class AdminService:
    def __init__(self, db: Database, bbs_cfg: BBSConfig) -> None:
        self.db = db
        self.bbs_cfg = bbs_cfg
        # In-memory per-admin pending broadcasts. Keyed by admin pubkey.
        self._pending: dict[str, PendingBroadcast] = {}

    def is_admin(self, pubkey: str) -> bool:
        return pubkey.lower() in {p.lower() for p in self.bbs_cfg.admin_pubkeys}

    async def ban(self, actor_pk: str, target_prefix: str, reason: str = "") -> str:
        target = await self.db.get_user_by_prefix(target_prefix)
        if target is None:
            return f"! No user matching {target_prefix}"
        if target.pubkey == actor_pk:
            return "! Cannot ban self."
        await self.db.set_banned(target.pubkey, True, reason or None)
        await self.db.audit(actor_pk, "ban", f"target={target.pubkey[:12]} reason={reason}")
        return f"OK banned {target.display_name or target.pubkey[:8]}"

    async def unban(self, actor_pk: str, target_prefix: str) -> str:
        target = await self.db.get_user_by_prefix(target_prefix)
        if target is None:
            return f"! No user matching {target_prefix}"
        await self.db.set_banned(target.pubkey, False, None)
        await self.db.audit(actor_pk, "unban", f"target={target.pubkey[:12]}")
        return f"OK unbanned {target.display_name or target.pubkey[:8]}"

    def stage_broadcast(self, actor_pk: str, text: str) -> str:
        self._pending[actor_pk] = PendingBroadcast(text=text, created_at=int(time.time()))
        n = len(text)
        return (
            f"Will send to ALL users ({n} chars). "
            f"Confirm with: ADMIN BROADCAST CONFIRM"
        )

    async def confirm_broadcast(self, actor_pk: str) -> tuple[str, list[str], str]:
        """Returns (status, recipient_pubkeys, body). recipient_pubkeys is
        empty on error; caller is responsible for enqueueing the sends."""
        pending = self._pending.pop(actor_pk, None)
        if pending is None:
            return "! No pending broadcast.", [], ""
        if int(time.time()) - pending.created_at > 120:
            return "! Confirmation expired (>2 min).", [], ""
        recipients = await self.db.all_user_pubkeys()
        await self.db.audit(
            actor_pk, "broadcast",
            f"recipients={len(recipients)} chars={len(pending.text)}",
        )
        return f"OK broadcasting to {len(recipients)} users.", recipients, pending.text
