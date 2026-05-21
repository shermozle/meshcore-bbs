"""Async SQLite database layer.

Schema follows spec §7. WAL mode is enabled at startup. All access goes through
this module so query strings are not scattered across services.

Migrations are managed by a `schema_version` PRAGMA-tracked counter. To add a
migration, append a new SQL block to `MIGRATIONS` — never rewrite history.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from .models import Board, BoardPost, Mail, NewsItem, OutboundMessage, User

log = logging.getLogger(__name__)


# Each list entry is a complete migration step. Index 0 is migration 1, etc.
MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE users (
      pubkey         TEXT PRIMARY KEY,
      display_name   TEXT UNIQUE,
      display_name_lc TEXT UNIQUE,
      adv_name       TEXT,
      first_seen     INTEGER NOT NULL,
      last_seen      INTEGER NOT NULL,
      msg_count      INTEGER NOT NULL DEFAULT 0,
      onboarded      INTEGER NOT NULL DEFAULT 0,
      motd_sent      INTEGER NOT NULL DEFAULT 0,
      banned         INTEGER NOT NULL DEFAULT 0,
      banned_reason  TEXT
    );
    CREATE INDEX idx_users_display ON users(display_name_lc);

    CREATE TABLE sessions (
      pubkey         TEXT PRIMARY KEY REFERENCES users(pubkey),
      state          TEXT,
      updated_at     INTEGER NOT NULL
    );

    CREATE TABLE boards (
      slug           TEXT PRIMARY KEY,
      description    TEXT,
      created_at     INTEGER NOT NULL
    );

    CREATE TABLE board_posts (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      board_slug     TEXT NOT NULL REFERENCES boards(slug),
      author_pubkey  TEXT NOT NULL REFERENCES users(pubkey),
      body           TEXT NOT NULL,
      ts             INTEGER NOT NULL,
      deleted        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_posts_board_ts ON board_posts(board_slug, ts DESC);

    CREATE TABLE mail (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      from_pubkey    TEXT NOT NULL REFERENCES users(pubkey),
      to_pubkey      TEXT NOT NULL REFERENCES users(pubkey),
      body           TEXT NOT NULL,
      sent_at        INTEGER NOT NULL,
      read_at        INTEGER,
      deleted        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_mail_to_unread ON mail(to_pubkey, read_at) WHERE deleted = 0;

    CREATE TABLE news_feeds (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      slug           TEXT UNIQUE NOT NULL,
      url            TEXT NOT NULL,
      enabled        INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE news_items (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      feed_id        INTEGER NOT NULL REFERENCES news_feeds(id),
      title          TEXT NOT NULL,
      url            TEXT,
      ts             INTEGER NOT NULL,
      hash           TEXT UNIQUE NOT NULL
    );
    CREATE INDEX idx_news_ts ON news_items(ts DESC);

    CREATE TABLE weather_cache (
      location_key   TEXT PRIMARY KEY,
      payload        TEXT NOT NULL,
      fetched_at     INTEGER NOT NULL
    );

    CREATE TABLE rate_limits (
      pubkey         TEXT NOT NULL,
      bucket         TEXT NOT NULL,
      window_start   INTEGER NOT NULL,
      count          INTEGER NOT NULL,
      PRIMARY KEY (pubkey, bucket)
    );

    CREATE TABLE audit_log (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      ts             INTEGER NOT NULL,
      actor_pubkey   TEXT,
      action         TEXT NOT NULL,
      detail         TEXT
    );

    CREATE TABLE outbound_queue (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      to_pubkey      TEXT NOT NULL,
      body           TEXT NOT NULL,
      enqueued_at    INTEGER NOT NULL,
      attempts       INTEGER NOT NULL DEFAULT 0,
      next_attempt   INTEGER NOT NULL,
      status         TEXT NOT NULL DEFAULT 'pending',
      priority       INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_outbound_pending ON outbound_queue(status, priority DESC, next_attempt);
    """,
]


class Database:
    """Owns an aiosqlite connection and exposes typed accessors."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = aiosqlite.Row
        await self._run_migrations()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not connected"
        return self._conn

    async def _run_migrations(self) -> None:
        cur = await self.conn.execute("PRAGMA user_version")
        row = await cur.fetchone()
        current = int(row[0]) if row else 0
        target = len(MIGRATIONS)
        for i in range(current, target):
            log.info("applying migration %d", i + 1)
            await self.conn.executescript(MIGRATIONS[i])
            await self.conn.execute(f"PRAGMA user_version = {i + 1}")
            await self.conn.commit()

    # -- users ----------------------------------------------------------------

    async def get_user(self, pubkey: str) -> User | None:
        cur = await self.conn.execute("SELECT * FROM users WHERE pubkey = ?", (pubkey,))
        row = await cur.fetchone()
        return _user_from_row(row) if row else None

    async def get_user_by_name(self, name: str) -> User | None:
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE display_name_lc = ?", (name.lower(),)
        )
        row = await cur.fetchone()
        return _user_from_row(row) if row else None

    async def get_user_by_prefix(self, prefix: str) -> User | None:
        """Resolve by pubkey prefix (hex). Returns first match or None.

        Used by the SEND command when the recipient is given as a pubkey prefix.
        """
        cur = await self.conn.execute(
            "SELECT * FROM users WHERE pubkey LIKE ? LIMIT 1", (prefix.lower() + "%",)
        )
        row = await cur.fetchone()
        return _user_from_row(row) if row else None

    async def upsert_user_first_seen(
        self, pubkey: str, adv_name: str | None, now: int
    ) -> tuple["User", bool]:
        """Create a stub user row if missing. Returns (user, is_new)."""
        existing = await self.get_user(pubkey)
        if existing:
            await self.conn.execute(
                "UPDATE users SET last_seen = ?, adv_name = COALESCE(?, adv_name) WHERE pubkey = ?",
                (now, adv_name, pubkey),
            )
            await self.conn.commit()
            return existing, False
        await self.conn.execute(
            """INSERT INTO users (pubkey, adv_name, first_seen, last_seen)
               VALUES (?, ?, ?, ?)""",
            (pubkey, adv_name, now, now),
        )
        await self.conn.commit()
        user = await self.get_user(pubkey)
        assert user is not None
        return user, True

    async def touch_user(self, pubkey: str, now: int) -> None:
        await self.conn.execute(
            "UPDATE users SET last_seen = ?, msg_count = msg_count + 1 WHERE pubkey = ?",
            (now, pubkey),
        )
        await self.conn.commit()

    async def set_display_name(self, pubkey: str, name: str) -> bool:
        """Set a user's display name. Returns False on uniqueness violation."""
        try:
            await self.conn.execute(
                """UPDATE users
                   SET display_name = ?, display_name_lc = ?, onboarded = 1
                   WHERE pubkey = ?""",
                (name, name.lower(), pubkey),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def mark_motd_sent(self, pubkey: str) -> None:
        await self.conn.execute(
            "UPDATE users SET motd_sent = 1 WHERE pubkey = ?", (pubkey,)
        )
        await self.conn.commit()

    async def set_banned(self, pubkey: str, banned: bool, reason: str | None = None) -> None:
        await self.conn.execute(
            "UPDATE users SET banned = ?, banned_reason = ? WHERE pubkey = ?",
            (1 if banned else 0, reason, pubkey),
        )
        await self.conn.commit()

    async def all_user_pubkeys(self) -> list[str]:
        cur = await self.conn.execute("SELECT pubkey FROM users WHERE banned = 0")
        return [r[0] for r in await cur.fetchall()]

    async def recent_active_users(self, limit: int = 5) -> list["User"]:
        """Return the most recently active onboarded users."""
        cur = await self.conn.execute(
            """SELECT * FROM users
               WHERE onboarded = 1 AND banned = 0
               ORDER BY last_seen DESC LIMIT ?""",
            (limit,),
        )
        return [_user_from_row(r) for r in await cur.fetchall()]

    # -- boards ---------------------------------------------------------------

    async def list_boards(self) -> list[Board]:
        cur = await self.conn.execute("SELECT * FROM boards ORDER BY slug")
        return [Board(slug=r["slug"], description=r["description"] or "", created_at=r["created_at"])
                for r in await cur.fetchall()]

    async def get_board(self, slug: str) -> Board | None:
        cur = await self.conn.execute("SELECT * FROM boards WHERE slug = ?", (slug.lower(),))
        row = await cur.fetchone()
        if not row:
            return None
        return Board(slug=row["slug"], description=row["description"] or "", created_at=row["created_at"])

    async def add_board(self, slug: str, description: str, now: int) -> None:
        await self.conn.execute(
            "INSERT INTO boards (slug, description, created_at) VALUES (?, ?, ?)",
            (slug.lower(), description, now),
        )
        await self.conn.commit()

    async def delete_board(self, slug: str) -> None:
        # Soft-delete posts implicitly by cascading; we delete the board row.
        # board_posts retain FK reference, so first soft-delete posts.
        await self.conn.execute(
            "UPDATE board_posts SET deleted = 1 WHERE board_slug = ?", (slug.lower(),)
        )
        await self.conn.execute("DELETE FROM boards WHERE slug = ?", (slug.lower(),))
        await self.conn.commit()

    async def add_post(self, board_slug: str, author: str, body: str, now: int) -> int:
        cur = await self.conn.execute(
            """INSERT INTO board_posts (board_slug, author_pubkey, body, ts)
               VALUES (?, ?, ?, ?)""",
            (board_slug.lower(), author, body, now),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def list_posts(self, board_slug: str, limit: int, offset: int) -> list[BoardPost]:
        cur = await self.conn.execute(
            """SELECT * FROM board_posts
               WHERE board_slug = ? AND deleted = 0
               ORDER BY ts DESC LIMIT ? OFFSET ?""",
            (board_slug.lower(), limit, offset),
        )
        return [
            BoardPost(
                id=r["id"],
                board_slug=r["board_slug"],
                author_pubkey=r["author_pubkey"],
                body=r["body"],
                ts=r["ts"],
                deleted=bool(r["deleted"]),
            )
            for r in await cur.fetchall()
        ]

    # -- mail -----------------------------------------------------------------

    async def add_mail(self, from_pk: str, to_pk: str, body: str, now: int) -> int:
        cur = await self.conn.execute(
            """INSERT INTO mail (from_pubkey, to_pubkey, body, sent_at)
               VALUES (?, ?, ?, ?)""",
            (from_pk, to_pk, body, now),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def list_mail(self, to_pk: str, limit: int = 10, offset: int = 0) -> list[Mail]:
        cur = await self.conn.execute(
            """SELECT * FROM mail
               WHERE to_pubkey = ? AND deleted = 0
               ORDER BY (read_at IS NOT NULL) ASC, sent_at DESC
               LIMIT ? OFFSET ?""",
            (to_pk, limit, offset),
        )
        return [_mail_from_row(r) for r in await cur.fetchall()]

    async def get_mail(self, mail_id: int, viewer_pk: str) -> Mail | None:
        cur = await self.conn.execute(
            """SELECT * FROM mail
               WHERE id = ? AND to_pubkey = ? AND deleted = 0""",
            (mail_id, viewer_pk),
        )
        row = await cur.fetchone()
        return _mail_from_row(row) if row else None

    async def mark_mail_read(self, mail_id: int, now: int) -> None:
        await self.conn.execute(
            "UPDATE mail SET read_at = ? WHERE id = ? AND read_at IS NULL",
            (now, mail_id),
        )
        await self.conn.commit()

    async def delete_mail(self, mail_id: int, viewer_pk: str) -> bool:
        cur = await self.conn.execute(
            "UPDATE mail SET deleted = 1 WHERE id = ? AND to_pubkey = ?",
            (mail_id, viewer_pk),
        )
        await self.conn.commit()
        return (cur.rowcount or 0) > 0

    async def count_unread(self, to_pk: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE to_pubkey = ? AND read_at IS NULL AND deleted = 0",
            (to_pk,),
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def count_total_mail(self, to_pk: str) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM mail WHERE to_pubkey = ? AND deleted = 0", (to_pk,)
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def users_with_unread_mail(self) -> list[str]:
        cur = await self.conn.execute(
            """SELECT DISTINCT to_pubkey FROM mail
               WHERE read_at IS NULL AND deleted = 0"""
        )
        return [r[0] for r in await cur.fetchall()]

    async def purge_old_read_mail(self, before_ts: int) -> int:
        cur = await self.conn.execute(
            "DELETE FROM mail WHERE read_at IS NOT NULL AND read_at < ?", (before_ts,)
        )
        await self.conn.commit()
        return cur.rowcount or 0

    # -- news -----------------------------------------------------------------

    async def upsert_news_feed(self, slug: str, url: str) -> int:
        cur = await self.conn.execute("SELECT id FROM news_feeds WHERE slug = ?", (slug,))
        row = await cur.fetchone()
        if row:
            await self.conn.execute(
                "UPDATE news_feeds SET url = ?, enabled = 1 WHERE id = ?", (url, row[0])
            )
            await self.conn.commit()
            return int(row[0])
        cur = await self.conn.execute(
            "INSERT INTO news_feeds (slug, url) VALUES (?, ?)", (slug, url)
        )
        await self.conn.commit()
        return int(cur.lastrowid or 0)

    async def disable_feeds_not_in(self, slugs: set[str]) -> None:
        """Disable any feed whose slug is not in the current config."""
        all_feeds = await self.conn.execute("SELECT slug FROM news_feeds WHERE enabled = 1")
        for row in await all_feeds.fetchall():
            if row[0] not in slugs:
                await self.conn.execute(
                    "UPDATE news_feeds SET enabled = 0 WHERE slug = ?", (row[0],)
                )
                log.info("disabled removed feed: %s", row[0])
        await self.conn.commit()

    async def list_feed_ids(self) -> list[tuple[int, str, str]]:
        cur = await self.conn.execute(
            "SELECT id, slug, url FROM news_feeds WHERE enabled = 1 ORDER BY slug"
        )
        return [(r[0], r[1], r[2]) for r in await cur.fetchall()]

    async def insert_news_item(
        self, feed_id: int, title: str, url: str | None, ts: int, h: str
    ) -> bool:
        try:
            await self.conn.execute(
                """INSERT INTO news_items (feed_id, title, url, ts, hash)
                   VALUES (?, ?, ?, ?, ?)""",
                (feed_id, title, url, ts, h),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def recent_news(self, limit: int, feed_slug: str | None = None) -> list[NewsItem]:
        if feed_slug:
            cur = await self.conn.execute(
                """SELECT i.* FROM news_items i
                   JOIN news_feeds f ON f.id = i.feed_id
                   WHERE f.slug = ?
                   ORDER BY i.ts DESC LIMIT ?""",
                (feed_slug, limit),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM news_items ORDER BY ts DESC LIMIT ?", (limit,)
            )
        return [
            NewsItem(
                id=r["id"],
                feed_id=r["feed_id"],
                title=r["title"],
                url=r["url"],
                ts=r["ts"],
                hash=r["hash"],
            )
            for r in await cur.fetchall()
        ]

    async def trim_feed_to(self, feed_id: int, keep_n: int) -> None:
        await self.conn.execute(
            """DELETE FROM news_items WHERE feed_id = ? AND id NOT IN (
                  SELECT id FROM news_items WHERE feed_id = ? ORDER BY ts DESC LIMIT ?)""",
            (feed_id, feed_id, keep_n),
        )
        await self.conn.commit()

    # -- weather cache --------------------------------------------------------

    async def get_weather_cache(self, key: str) -> tuple[str, int] | None:
        cur = await self.conn.execute(
            "SELECT payload, fetched_at FROM weather_cache WHERE location_key = ?", (key,)
        )
        row = await cur.fetchone()
        return (row[0], int(row[1])) if row else None

    async def set_weather_cache(self, key: str, payload: str, now: int) -> None:
        await self.conn.execute(
            """INSERT INTO weather_cache (location_key, payload, fetched_at)
               VALUES (?, ?, ?)
               ON CONFLICT(location_key) DO UPDATE SET payload=excluded.payload, fetched_at=excluded.fetched_at""",
            (key, payload, now),
        )
        await self.conn.commit()

    # -- rate limit -----------------------------------------------------------

    async def get_rate_window(self, pubkey: str, bucket: str) -> tuple[int, int] | None:
        cur = await self.conn.execute(
            "SELECT window_start, count FROM rate_limits WHERE pubkey = ? AND bucket = ?",
            (pubkey, bucket),
        )
        row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else None

    async def set_rate_window(self, pubkey: str, bucket: str, window_start: int, count: int) -> None:
        await self.conn.execute(
            """INSERT INTO rate_limits (pubkey, bucket, window_start, count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(pubkey, bucket) DO UPDATE SET
                  window_start=excluded.window_start,
                  count=excluded.count""",
            (pubkey, bucket, window_start, count),
        )
        await self.conn.commit()

    # -- audit ----------------------------------------------------------------

    async def audit(self, actor: str | None, action: str, detail: str = "") -> None:
        await self.conn.execute(
            "INSERT INTO audit_log (ts, actor_pubkey, action, detail) VALUES (?, ?, ?, ?)",
            (int(time.time()), actor, action, detail),
        )
        await self.conn.commit()

    async def purge_old_audit(self, before_ts: int) -> int:
        cur = await self.conn.execute("DELETE FROM audit_log WHERE ts < ?", (before_ts,))
        await self.conn.commit()
        return cur.rowcount or 0

    # -- outbound queue -------------------------------------------------------

    async def enqueue_outbound(
        self, to_pubkey: str, body: str, now: int, priority: int = 0
    ) -> int:
        cur = await self.conn.execute(
            """INSERT INTO outbound_queue
                 (to_pubkey, body, enqueued_at, next_attempt, priority)
               VALUES (?, ?, ?, ?, ?)""",
            (to_pubkey, body, now, now, priority),
        )
        await self.conn.commit()
        return cur.lastrowid or 0

    async def claim_next_outbound(self, now: int) -> OutboundMessage | None:
        cur = await self.conn.execute(
            """SELECT * FROM outbound_queue
               WHERE status = 'pending' AND next_attempt <= ?
               ORDER BY priority DESC, enqueued_at ASC
               LIMIT 1""",
            (now,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        return OutboundMessage(
            id=row["id"],
            to_pubkey=row["to_pubkey"],
            body=row["body"],
            enqueued_at=row["enqueued_at"],
            attempts=row["attempts"],
            next_attempt=row["next_attempt"],
            status=row["status"],
            priority=row["priority"],
        )

    async def mark_outbound_sent(self, msg_id: int) -> None:
        await self.conn.execute(
            "UPDATE outbound_queue SET status = 'sent' WHERE id = ?", (msg_id,)
        )
        await self.conn.commit()

    async def reschedule_outbound(self, msg_id: int, next_attempt: int, attempts: int) -> None:
        await self.conn.execute(
            "UPDATE outbound_queue SET next_attempt = ?, attempts = ? WHERE id = ?",
            (next_attempt, attempts, msg_id),
        )
        await self.conn.commit()

    async def mark_outbound_failed(self, msg_id: int) -> None:
        await self.conn.execute(
            "UPDATE outbound_queue SET status = 'failed' WHERE id = ?", (msg_id,)
        )
        await self.conn.commit()

    async def drop_stale_outbound(self, before_ts: int) -> int:
        cur = await self.conn.execute(
            """UPDATE outbound_queue SET status = 'dropped'
               WHERE status = 'pending' AND enqueued_at < ?""",
            (before_ts,),
        )
        await self.conn.commit()
        return cur.rowcount or 0

    async def outbound_pending_depth(self) -> int:
        cur = await self.conn.execute(
            "SELECT COUNT(*) FROM outbound_queue WHERE status = 'pending'"
        )
        row = await cur.fetchone()
        return int(row[0]) if row else 0

    # -- maintenance ----------------------------------------------------------

    async def vacuum(self) -> None:
        try:
            await self.conn.commit()
            await self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            await self.conn.execute("VACUUM")
            log.info("vacuum complete")
        except Exception as e:
            log.warning("vacuum skipped (db busy): %s", e)

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        return await self.conn.execute(sql, params)


def _user_from_row(row: aiosqlite.Row) -> User:
    return User(
        pubkey=row["pubkey"],
        display_name=row["display_name"],
        display_name_lc=row["display_name_lc"],
        adv_name=row["adv_name"],
        first_seen=row["first_seen"],
        last_seen=row["last_seen"],
        msg_count=row["msg_count"],
        onboarded=bool(row["onboarded"]),
        motd_sent=bool(row["motd_sent"]),
        banned=bool(row["banned"]),
        banned_reason=row["banned_reason"],
    )


def _mail_from_row(row: aiosqlite.Row) -> Mail:
    return Mail(
        id=row["id"],
        from_pubkey=row["from_pubkey"],
        to_pubkey=row["to_pubkey"],
        body=row["body"],
        sent_at=row["sent_at"],
        read_at=row["read_at"],
        deleted=bool(row["deleted"]),
    )


@asynccontextmanager
async def open_db(path: str | Path) -> AsyncIterator[Database]:
    db = Database(path)
    await db.connect()
    try:
        yield db
    finally:
        await db.close()
