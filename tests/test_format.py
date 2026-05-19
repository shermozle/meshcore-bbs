"""Packet splitting and formatting tests."""

from __future__ import annotations

from bbs.format import DEFAULT_PACKET_BYTES, split_packets, truncate


class TestSplitPackets:
    def test_short_single_packet(self):
        result = split_packets("hello")
        assert result == ["hello"]

    def test_empty(self):
        assert split_packets("") == [""]

    def test_short_with_newlines(self):
        # Trailing newline stripped.
        result = split_packets("line 1\nline 2\n")
        assert result == ["line 1\nline 2"]

    def test_exactly_at_limit(self):
        body = "x" * DEFAULT_PACKET_BYTES
        result = split_packets(body)
        assert len(result) == 1

    def test_just_over_limit(self):
        # Two lines, each within budget but together exceeding it.
        body = ("x" * 80) + "\n" + ("y" * 80)
        result = split_packets(body)
        # Should produce at least 2 packets with (n/m) prefix
        assert len(result) >= 2
        for i, pkt in enumerate(result):
            assert pkt.startswith(f"({i + 1}/{len(result)})")

    def test_long_single_line_hard_wrapped(self):
        # 500 chars on one line — must be hard-wrapped.
        body = "x" * 500
        result = split_packets(body)
        assert len(result) >= 2
        # Every packet fits the budget (with prefix).
        for pkt in result:
            assert len(pkt.encode("utf-8")) <= DEFAULT_PACKET_BYTES

    def test_multi_packet_prefix_format(self):
        body = "\n".join(f"line {i}" * 5 for i in range(20))
        result = split_packets(body)
        assert len(result) >= 2
        for i, pkt in enumerate(result):
            assert pkt.startswith(f"({i + 1}/{len(result)})")

    def test_utf8_multibyte_handled(self):
        # Each emoji is 4 bytes in UTF-8.
        body = "🦘" * 50  # 200 bytes
        result = split_packets(body)
        # Should split, and each packet fits the byte budget.
        for pkt in result:
            assert len(pkt.encode("utf-8")) <= DEFAULT_PACKET_BYTES


class TestTruncate:
    def test_short_unchanged(self):
        assert truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        assert truncate("hello", 5) == "hello"

    def test_truncated_with_ellipsis(self):
        result = truncate("hello world", 8)
        assert result.endswith("...")
        assert len(result) == 8

    def test_custom_ellipsis(self):
        result = truncate("hello world", 8, ellipsis="~")
        assert result.endswith("~")
        assert len(result) == 8
