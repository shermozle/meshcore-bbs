"""Domain models.

These are plain dataclasses, not ORM rows — the DB layer maps to/from these.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class User:
    pubkey: str
    display_name: str | None
    display_name_lc: str | None
    adv_name: str | None
    first_seen: int
    last_seen: int
    msg_count: int = 0
    onboarded: bool = False
    motd_sent: bool = False
    banned: bool = False
    banned_reason: str | None = None
    last_hops: int | None = None


@dataclass
class Board:
    slug: str
    description: str
    created_at: int


@dataclass
class BoardPost:
    id: int
    board_slug: str
    author_pubkey: str
    body: str
    ts: int
    deleted: bool = False


@dataclass
class Mail:
    id: int
    from_pubkey: str
    to_pubkey: str
    body: str
    sent_at: int
    read_at: int | None = None
    deleted: bool = False


@dataclass
class NewsItem:
    id: int
    feed_id: int
    title: str
    url: str | None
    ts: int
    hash: str


@dataclass
class OutboundMessage:
    id: int
    to_pubkey: str
    body: str
    enqueued_at: int
    attempts: int
    next_attempt: int
    status: str
    priority: int  # higher = sooner
    trigger_command: str | None = None
    msg_kind: str = "response"
