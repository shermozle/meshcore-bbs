"""News service: RSS ingestion + recent headlines.

Scheduled job `refresh_all` pulls every enabled feed every
`refresh_interval_seconds`, dedupes by hash, and trims each feed to N items.

`recent_text(...)` returns a packet-ready string of the most recent N items,
either across all feeds or filtered to one feed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import feedparser
import httpx

from ..config import NewsConfig
from ..db import Database
from ..format import truncate
from ..models import NewsItem

log = logging.getLogger(__name__)

HEADLINE_MAX_CHARS = 100


class NewsService:
    def __init__(self, db: Database, cfg: NewsConfig, user_agent: str) -> None:
        self.db = db
        self.cfg = cfg
        self.user_agent = user_agent

    async def initialise_feeds(self) -> None:
        """Make sure feed rows exist for every configured feed."""
        for feed in self.cfg.feeds:
            await self.db.upsert_news_feed(feed.slug, feed.url)

    async def refresh_all(self) -> int:
        """Refresh every enabled feed. Returns total new items inserted."""
        feeds = await self.db.list_feed_ids()
        total_new = 0
        async with httpx.AsyncClient(
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/rss+xml, application/xml, text/xml, */*",
                "Accept-Language": "en-AU,en;q=0.9",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            for feed_id, slug, url in feeds:
                try:
                    inserted = await self._refresh_one(client, feed_id, slug, url)
                    total_new += inserted
                except Exception as e:
                    log.warning("feed %s refresh failed: %s", slug, e)
        return total_new

    async def _refresh_one(
        self, client: httpx.AsyncClient, feed_id: int, slug: str, url: str
    ) -> int:
        log.debug("refreshing feed %s", slug)
        resp = await client.get(url)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        new_count = 0
        for entry in parsed.entries[: self.cfg.max_items_per_feed]:
            title = (entry.get("title") or "").strip()
            if not title:
                continue
            link = entry.get("link")
            published = _entry_ts(entry) or int(time.time())
            h = hashlib.sha256(f"{feed_id}|{title}|{link or ''}".encode()).hexdigest()
            if await self.db.insert_news_item(feed_id, title, link, published, h):
                new_count += 1
        await self.db.trim_feed_to(feed_id, self.cfg.max_items_per_feed)
        log.info("feed %s: %d new", slug, new_count)
        return new_count

    async def recent_text(self, limit: int = 5, feed_slug: str | None = None) -> str:
        items = await self.db.recent_news(limit=limit, feed_slug=feed_slug)
        if not items:
            return "No news yet."
        return "\n".join(_format_item(i + 1, item) for i, item in enumerate(items))


def _entry_ts(entry: Any) -> int | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return int(time.mktime(t))
            except Exception:
                continue
    return None


def _format_item(idx: int, item: NewsItem) -> str:
    title = truncate(item.title, HEADLINE_MAX_CHARS)
    return f"[{idx}] {title}"


async def schedule_news_refresh(svc: NewsService, interval_seconds: int) -> None:
    """Long-running task: refresh every interval_seconds."""
    # Initial refresh on startup; small delay so transport can come up first.
    await asyncio.sleep(5)
    while True:
        try:
            await svc.refresh_all()
        except Exception:
            log.exception("news refresh failed")
        await asyncio.sleep(interval_seconds)
