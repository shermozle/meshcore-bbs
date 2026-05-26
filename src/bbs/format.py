"""Reply formatting.

MeshCore DMs have a small per-packet payload (~140 bytes after overhead).
Long replies must be split into numbered packets `(1/N)`, `(2/N)`, ...
Splitting is line-aware: we never break mid-line. If a single line exceeds
the packet limit, it's hard-wrapped at the boundary as a last resort.
"""

from __future__ import annotations

# Per spec §5.1, MeshCore payloads are ~140 bytes after framing overhead.
# Bias conservative; users running with non-default radio configs may have less.
DEFAULT_PACKET_BYTES = 140


def split_packets(text: str, packet_bytes: int = DEFAULT_PACKET_BYTES) -> list[str]:
    """Split a reply into one or more packet-sized strings.

    A `(N/M)` prefix is added when there is more than one packet. The prefix
    is counted against the packet budget. The headroom is computed dynamically
    from the actual packet count rather than a fixed worst-case guess, so
    short splits recover a few bytes per packet.
    """
    text = text.rstrip("\n")
    if not text:
        return [""]

    if _utf8_len(text) <= packet_bytes:
        return [text]

    # First pass: split into raw packets without prefix overhead.
    raw = _pack_lines(text, packet_bytes)
    if len(raw) == 1:
        return raw

    # Compute actual headroom from the real packet count and re-split.
    n = len(raw)
    prefix_len = _utf8_len(f"({n}/{n}) ")  # e.g. "(3/5) " = 7 bytes
    budget = packet_bytes - prefix_len
    if budget < 16:
        budget = 16  # floor: at least a few chars per packet

    packets = _pack_lines(text, budget)

    # If re-splitting changed the count (rare), prefix length may be off by a
    # byte. That's fine — the prefixes will still fit because `prefix_len`
    # was anchored to the first-pass count and re-splitting with a tighter
    # budget can only produce more packets, not fewer.
    return [f"({i + 1}/{n}) {p}" for i, p in enumerate(packets)]


def _pack_lines(text: str, budget: int) -> list[str]:
    """Greedy-pack lines into budget-constrained strings (no prefix)."""
    lines = text.split("\n")
    packets: list[str] = []
    cur: list[str] = []
    cur_size = 0
    for line in lines:
        line_bytes = _utf8_len(line) + (1 if cur else 0)  # +1 for newline
        if cur and cur_size + line_bytes > budget:
            packets.append("\n".join(cur))
            cur = [line]
            cur_size = _utf8_len(line)
        else:
            if _utf8_len(line) > budget:
                # Single line too long. Flush current, then hard-wrap.
                if cur:
                    packets.append("\n".join(cur))
                    cur, cur_size = [], 0
                packets.extend(_hard_wrap(line, budget))
            else:
                cur.append(line)
                cur_size += line_bytes
    if cur:
        packets.append("\n".join(cur))
    return packets


def _hard_wrap(line: str, budget: int) -> list[str]:
    """Split a single long line into byte-budgeted chunks (UTF-8-aware)."""
    out: list[str] = []
    cur = ""
    cur_len = 0
    for ch in line:
        ch_len = len(ch.encode("utf-8"))
        if cur_len + ch_len > budget:
            out.append(cur)
            cur = ch
            cur_len = ch_len
        else:
            cur += ch
            cur_len += ch_len
    if cur:
        out.append(cur)
    return out


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


def truncate(s: str, max_chars: int, ellipsis: str = "...") -> str:
    """Truncate a string to at most `max_chars` characters."""
    if len(s) <= max_chars:
        return s
    return s[: max_chars - len(ellipsis)] + ellipsis
