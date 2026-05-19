"""Command parser tests."""

import pytest

from bbs.commands import help_text, parse


class TestParse:
    def test_empty(self):
        assert parse("") is None
        assert parse("   ") is None

    def test_simple_verb(self):
        p = parse("HELP")
        assert p.verb == "HELP"
        assert p.args == []
        assert p.rest == ""

    def test_case_insensitive_verb(self):
        assert parse("help").verb == "HELP"
        assert parse("Help").verb == "HELP"

    def test_args(self):
        p = parse("READ general 2")
        assert p.verb == "READ"
        assert p.args == ["general", "2"]
        assert p.rest == "general 2"

    def test_post_rest(self):
        p = parse("POST general hello world this is text")
        assert p.verb == "POST"
        assert p.args == ["general", "hello", "world", "this", "is", "text"]
        assert p.rest == "general hello world this is text"

    def test_admin_subverb(self):
        p = parse("ADMIN BAN abc123")
        assert p.verb == "ADMIN BAN"
        assert p.args == ["abc123"]
        assert p.rest == "abc123"

    def test_admin_board_add(self):
        p = parse("ADMIN BOARD ADD swap A board for swaps")
        assert p.verb == "ADMIN BOARD ADD"
        assert p.args == ["swap", "A", "board", "for", "swaps"]
        assert p.rest == "swap A board for swaps"

    def test_admin_broadcast(self):
        p = parse("ADMIN BROADCAST hello everyone")
        assert p.verb == "ADMIN BROADCAST"
        assert p.rest == "hello everyone"

    def test_multiple_whitespace(self):
        p = parse("HELP    WHOAMI")
        assert p.verb == "HELP"
        assert p.args == ["WHOAMI"]


class TestHelp:
    def test_overview(self):
        text = help_text()
        assert "HELP" in text
        assert "NEWS" in text

    def test_specific_topic(self):
        assert "NAME" in help_text("NAME")
        assert "READ" in help_text("READ")

    def test_unknown_topic(self):
        assert "?" in help_text("NOTACOMMAND")
