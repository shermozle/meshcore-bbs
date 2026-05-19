"""Public message boards."""

from __future__ import annotations

import re
import time

from ..db import Database
from ..format import truncate
from ..models import BoardPost

SLUG_PATTERN = re.compile(r"^[a-z0-9_-]{1,16}$")

POSTS_PER_PAGE = 5


class BoardsService:
    def __init__(self, db: Database, max_body_chars: int) -> None:
        self.db = db
        self.max_body_chars = max_body_chars

    async def list_text(self) -> str:
        boards = await self.db.list_boards()
        if not boards:
            return "No boards yet."
        return "\n".join(
            f"{b.slug}: {truncate(b.description, 60)}" if b.description else b.slug
            for b in boards
        )

    async def read_text(self, slug: str, page: int = 1) -> str:
        slug = slug.lower()
        board = await self.db.get_board(slug)
        if board is None:
            return f"! Board \"{slug}\" not found. Try: BOARDS"
        offset = max(0, (page - 1) * POSTS_PER_PAGE)
        posts = await self.db.list_posts(slug, POSTS_PER_PAGE, offset)
        if not posts:
            return f"{slug}: no posts." if page == 1 else f"{slug}: no more posts."

        lines = [await self._render_post(p) for p in posts]
        text = "\n".join(lines)
        # Cheap "more" hint: if we got a full page, suggest there could be more.
        if len(posts) == POSTS_PER_PAGE:
            text += f"\n[more: READ {slug} {page + 1}]"
        return text

    async def _render_post(self, p: BoardPost) -> str:
        author = await self.db.get_user(p.author_pubkey)
        name = (author.display_name if author and author.display_name else p.author_pubkey[:8])
        body = truncate(p.body, 120)
        return f"[{p.id}] {name}: {body}"

    async def post(self, slug: str, author_pk: str, body: str) -> str:
        slug = slug.lower()
        if not SLUG_PATTERN.match(slug):
            return f"! Bad board name."
        board = await self.db.get_board(slug)
        if board is None:
            return f"! Board \"{slug}\" not found. Try: BOARDS"
        body = body.strip()
        if not body:
            return "! Empty post."
        if len(body) > self.max_body_chars:
            return f"! Too long, max {self.max_body_chars} chars"
        body = _strip_control_chars(body)
        post_id = await self.db.add_post(slug, author_pk, body, int(time.time()))
        return f"OK [id={post_id}]"

    async def add_board(self, slug: str, description: str) -> str:
        slug = slug.lower()
        if not SLUG_PATTERN.match(slug):
            return "! Bad board name. Use [a-z0-9_-], 1-16 chars."
        existing = await self.db.get_board(slug)
        if existing:
            return f"! Board \"{slug}\" exists."
        await self.db.add_board(slug, description.strip(), int(time.time()))
        return f"OK [board={slug}]"

    async def delete_board(self, slug: str) -> str:
        slug = slug.lower()
        existing = await self.db.get_board(slug)
        if not existing:
            return f"! Board \"{slug}\" not found."
        await self.db.delete_board(slug)
        return f"OK [board={slug} deleted]"


def _strip_control_chars(s: str) -> str:
    return "".join(ch for ch in s if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
