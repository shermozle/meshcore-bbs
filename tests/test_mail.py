"""Mail recipient resolution tests."""

from __future__ import annotations

import time

import pytest

from bbs.services.mail import MailService
from bbs.onboarding import try_set_name


@pytest.fixture
def mail_svc(db, cfg) -> MailService:
    return MailService(db, cfg.mail, cfg.bbs.max_msg_chars)


class TestResolveRecipient:
    async def test_exact_name(self, db, mail_svc, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "alice", cfg.bbs)
        res = await mail_svc.resolve_recipient("alice")
        assert res.user is not None
        assert res.user.display_name == "alice"

    async def test_partial_name_match(self, db, mail_svc, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "🗼VK2VSR", cfg.bbs)
        res = await mail_svc.resolve_recipient("VK2VSR")
        assert res.user is not None
        assert res.user.display_name == "🗼VK2VSR"

    async def test_partial_case_insensitive(self, db, mail_svc, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "🗼VK2VSR", cfg.bbs)
        res = await mail_svc.resolve_recipient("vk2vsr")
        assert res.user is not None

    async def test_ambiguous_partial(self, db, mail_svc, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await db.upsert_user_first_seen("b" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "alice", cfg.bbs)
        await try_set_name(db, "b" * 64, "alicia", cfg.bbs)
        res = await mail_svc.resolve_recipient("ali")
        assert res.user is None
        assert res.ambiguous_names == ["alice", "alicia"]

    async def test_unknown(self, db, mail_svc):
        res = await mail_svc.resolve_recipient("nobody")
        assert res.user is None
        assert res.ambiguous_names is None
