"""Display-name validation and matching helpers."""

from __future__ import annotations

import re
import unicodedata

NAME_MIN = 1
NAME_MAX = 10

# Characters we never allow in display names (whitespace, ASCII punctuation, etc.).
_DISALLOWED = re.compile(
    r"[\s\x00-\x1f\x7f-\x9f"
    r"!@#$%^&*()+=[\]{}|\\;:'\",.<>/?`~]"
)

# Emoji blocks commonly used on MeshCore handles (flags, symbols, emoticons, etc.).
_EMOJI_CHAR = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\u200d\ufe0f"
    "]",
    flags=re.UNICODE,
)


def is_emoji_char(ch: str) -> bool:
    if len(ch) != 1:
        return False
    if ch in "\u200d\ufe0f":
        return True
    return _EMOJI_CHAR.fullmatch(ch) is not None


def is_allowed_name_char(ch: str) -> bool:
    if len(ch) != 1:
        return False
    if ch.isascii() and (ch.isalnum() or ch in "_-"):
        return True
    return is_emoji_char(ch)


def validate_name_chars(name: str) -> str | None:
    """Return None if every character is allowed, else an error string."""
    if _DISALLOWED.search(name):
        return "! Bad chars, use A-Z 0-9 _ - emoji"
    for ch in name:
        if not is_allowed_name_char(ch):
            cat = unicodedata.category(ch)
            # Reject letters/numbers outside ASCII (e.g. Cyrillic) — emoji only.
            if cat[0] in "LN":
                return "! Bad chars, use A-Z 0-9 _ - emoji"
            if not is_emoji_char(ch):
                return "! Bad chars, use A-Z 0-9 _ - emoji"
    return None
