"""Command parsing.

The wire format is a single line: `VERB [arg1 arg2 ...]`. Verbs are
case-insensitive; arguments are whitespace-separated unless the command
specifies a "rest" argument (e.g. POST <board> <text...>).

`parse(text)` returns a tuple (verb, args, rest) where:
  - verb: uppercase verb name
  - args: list of leading whitespace-separated tokens
  - rest: the remainder of the line after stripping the verb (used by
    commands that take free-form text)

Help topics live here so the help text travels with the command vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class ParsedCommand:
    verb: str
    args: list[str]
    rest: str

    @property
    def admin(self) -> bool:
        return self.verb == "ADMIN"


def parse(line: str) -> ParsedCommand | None:
    line = (line or "").strip()
    if not line:
        return None
    tokens = WHITESPACE_RE.split(line)
    verb = tokens[0].upper()
    args = tokens[1:]

    # For ADMIN subcommands, treat the next token as the verb suffix.
    if verb == "ADMIN" and args:
        verb = f"ADMIN {args[0].upper()}"
        args = args[1:]
        # Nested: BOARD ADD / BOARD DEL
        if args and verb in ("ADMIN BOARD",):
            verb = f"{verb} {args[0].upper()}"
            args = args[1:]

    # `rest` is what's left after the verb prefix.
    prefix_words = verb.split(" ")
    # Strip the first len(prefix_words) tokens from the original line.
    rest_match = re.match(r"^\s*" + r"\s+".join(re.escape(w) for w in prefix_words) + r"\s*", line, re.IGNORECASE)
    rest = line[rest_match.end():] if rest_match else line
    return ParsedCommand(verb=verb, args=args, rest=rest.strip())


HELP_OVERVIEW = (
    "Cmds: HELP WHOAMI NAME WHO PING NEWS WX BOARDS READ POST\n"
    "MAIL INBOX READMAIL SEND DELETE STATUS\n"
    "HELP <cmd> for details."
)

HELP_TOPICS: dict[str, str] = {
    "HELP": "HELP [topic] - list cmds or detail on one.",
    "WHOAMI": "WHOAMI - show your name and pubkey prefix.",
    "NAME": "NAME <new> - set display name (1-10 chars, A-Z 0-9 _ - emoji).",
    "WHO": "WHO - list up to 5 recently active users with hop count.",
    "PING": "PING - pong with the mesh path taken to reach the BBS.",
    "NEWS": "NEWS [feed] - top headlines, optionally from one feed.",
    "WX": "WX [station] - weather summary (default: BBS-local).",
    "BOARDS": "BOARDS - list public boards.",
    "READ": "READ <board> [page] - read posts (5/page, newest first).",
    "POST": "POST <board> <text> - post to a board.",
    "MAIL": "MAIL - show unread/total counts.",
    "INBOX": "INBOX [page] - list inbox, unread first.",
    "READMAIL": "READMAIL <id> - read a mail and mark it read.",
    "SEND": "SEND <user> <text> - send mail (name, partial name, or pubkey).",
    "DELETE": "DELETE <id> - delete a mail.",
    "STATUS": "STATUS - BBS uptime, version, outbound queue depth.",
    "ADMIN": "ADMIN BAN|UNBAN|BOARD ADD|BOARD DEL|BROADCAST",
}


def help_text(topic: str | None = None) -> str:
    if not topic:
        return HELP_OVERVIEW
    return HELP_TOPICS.get(topic.upper(), f"? Unknown topic: {topic}")
