"""Onboarding name-validation tests."""

from __future__ import annotations

import time

from bbs.onboarding import (
    NAME_MAX,
    RESERVED_NAMES,
    try_set_name,
    validate_name,
)


class TestValidateName:
    def test_valid_simple(self):
        assert validate_name("alice") is None

    def test_valid_with_underscore(self):
        assert validate_name("al_ice") is None

    def test_valid_with_dash(self):
        assert validate_name("al-ice") is None

    def test_valid_with_digits(self):
        assert validate_name("alice42") is None

    def test_valid_single_char(self):
        assert validate_name("a") is None

    def test_empty_rejected(self):
        assert validate_name("") is not None

    def test_too_long_rejected(self):
        result = validate_name("a" * (NAME_MAX + 1))
        assert result is not None
        assert "too long" in result.lower() or "max" in result.lower()

    def test_spaces_rejected(self):
        assert validate_name("al ice") is not None

    def test_special_chars_rejected(self):
        for bad in ("al!ce", "al@ce", "al.ce", "al/ce", "al$ce"):
            assert validate_name(bad) is not None, f"should reject {bad!r}"

    def test_reserved_names_rejected(self):
        for name in RESERVED_NAMES:
            result = validate_name(name)
            assert result is not None, f"should reject reserved {name!r}"
            assert "reserved" in result.lower()

    def test_reserved_check_case_insensitive(self):
        assert validate_name("Admin") is not None
        assert validate_name("ADMIN") is not None


class TestTrySetName:
    async def test_first_set_succeeds(self, db, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        ok, reply = await try_set_name(db, "a" * 64, "alice", cfg.bbs)
        assert ok
        assert "alice" in reply
        # User is now onboarded.
        u = await db.get_user("a" * 64)
        assert u.onboarded
        assert u.display_name == "alice"
        assert u.motd_sent

    async def test_duplicate_rejected(self, db, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await db.upsert_user_first_seen("b" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "alice", cfg.bbs)
        ok, reply = await try_set_name(db, "b" * 64, "alice", cfg.bbs)
        assert not ok
        assert "taken" in reply.lower()

    async def test_duplicate_case_insensitive(self, db, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        await db.upsert_user_first_seen("b" * 64, None, int(time.time()))
        await try_set_name(db, "a" * 64, "alice", cfg.bbs)
        ok, _ = await try_set_name(db, "b" * 64, "ALICE", cfg.bbs)
        assert not ok

    async def test_invalid_name_rejected(self, db, cfg):
        await db.upsert_user_first_seen("a" * 64, None, int(time.time()))
        ok, reply = await try_set_name(db, "a" * 64, "bad!name", cfg.bbs)
        assert not ok
        assert reply.startswith("!")
