#!/usr/bin/env python3
"""Seed a dev database with boards, users, news items, and mail.

Useful for testing the dispatcher locally before exposing to real users.

Usage:
    python scripts/seed_dev_db.py /path/to/bbs.db
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import sys
import time
from pathlib import Path

# Make src/ importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from bbs.db import Database  # noqa: E402


BOARDS = [
    ("general", "General chat and announcements"),
    ("swap", "Buy / sell / trade"),
    ("emergency", "Emergency comms only"),
]

SAMPLE_USERS = [
    ("alice", "Alice"),
    ("bob", "Bob"),
    ("charlie", "Charlie"),
    ("delta", "Delta"),
]

NEWS_ITEMS = [
    "RBA holds rates at 4.1%",
    "Bushfires in VIC east, 3 homes lost",
    "Cricket: AUS beat IND by 4 wkts",
    "Sydney trains: signal failure on Western line",
    "Federal budget delayed 2 weeks",
    "ANZAC Day march draws record attendance",
    "Wallabies announce new captain",
    "Heavy rain expected across NSW north coast",
    "Tech sector hiring continues to slow",
    "Submarine deal: timeline pushed back",
]

POSTS = [
    ("general", "alice", "Anyone tested the new repeater on Mt Tomah?"),
    ("general", "bob", "Got it linked last night. Coverage is great"),
    ("general", "charlie", "Will be in the area Saturday — should I bring my V3?"),
    ("swap", "bob", "Spare RAK4631 for sale, $40 ono"),
    ("swap", "alice", "Looking for a T-Beam Supreme if anyone has one"),
    ("emergency", "delta", "Test post, please ignore."),
]

MAILS = [
    ("alice", "bob", "Hey, what time are you on the air tonight?"),
    ("bob", "alice", "Around 8pm, on channel 1."),
    ("charlie", "alice", "Got your contact card from the repeater — thanks!"),
]


def fake_pubkey(name: str) -> str:
    """Stable per-name pubkey derived from the name."""
    h = hashlib.sha256(name.encode()).hexdigest()
    return h[:64]


async def seed(db_path: str) -> None:
    print(f"Seeding {db_path}")
    db = Database(db_path)
    await db.connect()
    try:
        now = int(time.time())

        # Boards
        for slug, desc in BOARDS:
            existing = await db.get_board(slug)
            if existing:
                print(f"  board {slug} exists, skipping")
            else:
                await db.add_board(slug, desc, now)
                print(f"  + board {slug}")

        # Users (onboarded with display names)
        pubkeys: dict[str, str] = {}
        for handle, _adv in SAMPLE_USERS:
            pk = fake_pubkey(handle)
            pubkeys[handle] = pk
            existing = await db.get_user(pk)
            if existing and existing.onboarded:
                print(f"  user {handle} exists, skipping")
                continue
            await db.upsert_user_first_seen(pk, handle, now)
            ok = await db.set_display_name(pk, handle)
            if ok:
                print(f"  + user {handle} ({pk[:8]})")
            else:
                print(f"  ! could not set name for {handle}")

        # News
        # Create a single 'dev' feed first.
        feed_id = await db.upsert_news_feed("dev", "local://dev-seed")
        for i, title in enumerate(NEWS_ITEMS):
            ts = now - (i * 600)  # spaced 10 min apart, newest first
            h = hashlib.sha256(f"dev|{title}".encode()).hexdigest()
            inserted = await db.insert_news_item(feed_id, title, None, ts, h)
            if inserted:
                print(f"  + news: {title}")

        # Posts
        for slug, author, body in POSTS:
            pk = pubkeys.get(author)
            if pk:
                await db.add_post(slug, pk, body, now)
        print(f"  + {len(POSTS)} posts")

        # Mail
        for from_h, to_h, body in MAILS:
            from_pk = pubkeys.get(from_h)
            to_pk = pubkeys.get(to_h)
            if from_pk and to_pk:
                await db.add_mail(from_pk, to_pk, body, now)
        print(f"  + {len(MAILS)} mails")

        print("Done.")
    finally:
        await db.close()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <db_path>", file=sys.stderr)
        return 1
    asyncio.run(seed(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
