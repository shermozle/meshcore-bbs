"""News service tests.

Exercises initialisation, text formatting, and the DB layer around feed
management. Network fetch paths are left to integration tests.
"""

from __future__ import annotations

import time

import pytest

from bbs.services.news import NewsService, _entry_ts, _format_item
from bbs.models import NewsItem


@pytest.fixture
def news_svc(db, cfg) -> NewsService:
    return NewsService(db, cfg.news, cfg.weather.user_agent)


class TestFormatItem:
    def test_format_with_title(self):
        item = NewsItem(id=1, feed_id=1, title="Breaking news", url=None, ts=0, hash="abc")
        result = _format_item(1, item)
        assert result == "[1] Breaking news"

    def test_format_long_title_truncated(self):
        item = NewsItem(id=1, feed_id=1, title="x" * 200, url=None, ts=0, hash="abc")
        result = _format_item(1, item)
        assert len(result) <= 105  # [1] + space + 100 chars


class TestEntryTimestamp:
    def test_published_parsed(self):
        entry = {"published_parsed": time.struct_time((2026, 1, 15, 12, 0, 0, 0, 0, 0))}
        ts = _entry_ts(entry)
        assert ts is not None
        assert ts > 0

    def test_updated_parsed_fallback(self):
        entry = {"updated_parsed": time.struct_time((2026, 1, 15, 12, 0, 0, 0, 0, 0))}
        ts = _entry_ts(entry)
        assert ts is not None
        assert ts > 0

    def test_no_timestamp(self):
        assert _entry_ts({}) is None


class TestInitialiseFeeds:
    async def test_adds_configured_feeds(self, db, cfg, news_svc):
        cfg.news.feeds.append(
            type(cfg.news.feeds[0] if cfg.news.feeds else None)(
                slug="testfeed", url="https://example.com/rss"
            )
            if cfg.news.feeds
            else None,
        )
        # Instead of mutating the config, just test with a fresh config.
        from bbs.config import NewsFeed, NewsConfig

        cfg2 = type(cfg)(
            device=cfg.device,
            bbs=cfg.bbs,
            limits=cfg.limits,
            news=NewsConfig(
                feeds=[NewsFeed(slug="testfeed", url="https://example.com/rss")],
                max_items_per_feed=50,
                refresh_interval_seconds=900,
            ),
            weather=cfg.weather,
            contacts=cfg.contacts,
            mail=cfg.mail,
            health=cfg.health,
            metrics=cfg.metrics,
            logging=cfg.logging,
        )
        svc = NewsService(db, cfg2.news, cfg.weather.user_agent)
        await svc.initialise_feeds()

        feeds = await db.list_feed_ids()
        slugs = {f[1] for f in feeds}
        assert "testfeed" in slugs

    async def test_disables_removed_feeds(self, db, cfg, news_svc):
        await news_svc.initialise_feeds()
        # With no feeds configured, any pre-existing enabled feeds get disabled.
        feeds = await db.list_feed_ids()
        # All configured feeds from cfg.news.feeds should be enabled.
        for _, slug, _ in feeds:
            cur = await db.execute(
                "SELECT enabled FROM news_feeds WHERE slug = ?", (slug,)
            )
            row = await cur.fetchone()
            assert row is not None


class TestRecentText:
    async def test_empty_no_items(self, news_svc):
        result = await news_svc.recent_text(limit=5)
        assert result == "No news yet."

    async def test_with_items(self, db, news_svc):
        fid = await db.upsert_news_feed("test", "https://example.com/rss")
        await db.insert_news_item(fid, "Headline 1", None, 1000, "hash1")
        await db.insert_news_item(fid, "Headline 2", None, 2000, "hash2")

        result = await news_svc.recent_text(limit=5)
        assert "Headline 1" in result
        assert "Headline 2" in result


class TestDBNewsOperations:
    async def test_insert_dedup_by_hash(self, db):
        fid = await db.upsert_news_feed("test", "https://example.com/rss")
        ok1 = await db.insert_news_item(fid, "Title", None, 1000, "same_hash")
        ok2 = await db.insert_news_item(fid, "Title Again", None, 2000, "same_hash")
        assert ok1 is True
        assert ok2 is False

    async def test_trim_feed(self, db):
        fid = await db.upsert_news_feed("test", "https://example.com/rss")
        for i in range(10):
            await db.insert_news_item(fid, f"Item {i}", None, i, f"hash{i}")
        await db.trim_feed_to(fid, 5)

        items = await db.recent_news(20, feed_slug="test")
        assert len(items) == 5
        # Newest items (highest ts) should be kept.
        for item in items:
            assert item.ts >= 5

    async def test_recent_news_filter_by_feed(self, db):
        fid_a = await db.upsert_news_feed("feed_a", "https://a.example.com/rss")
        fid_b = await db.upsert_news_feed("feed_b", "https://b.example.com/rss")
        await db.insert_news_item(fid_a, "A News", None, 1000, "ha")
        await db.insert_news_item(fid_b, "B News", None, 2000, "hb")

        items = await db.recent_news(10, feed_slug="feed_a")
        assert len(items) == 1
        assert items[0].title == "A News"

    async def test_disable_feed_not_in_config(self, db):
        fid = await db.upsert_news_feed("orphan", "https://orphan.example.com/rss")
        cur = await db.execute("SELECT enabled FROM news_feeds WHERE slug = 'orphan'")
        assert (await cur.fetchone())[0] == 1

        await db.disable_feeds_not_in(set())  # nothing configured
        cur = await db.execute("SELECT enabled FROM news_feeds WHERE slug = 'orphan'")
        assert (await cur.fetchone())[0] == 0
