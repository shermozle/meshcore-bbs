"""First-contact onboarding (spec §4.4 and §19.2).

When a never-seen pubkey DMs the BBS, the user is created with onboarded=0
and prompted for a display name. Until the name is set, all other commands
are rejected with `! Set a name first: NAME <yourname>`.

After successful naming, the user is `onboarded=1` and a one-time MOTD with a
HELP pointer is appended to the success reply (then `motd_sent` is set so we
don't repeat it).
"""

from __future__ import annotations

import re

from .config import BBSConfig
from .db import Database

RESERVED_NAMES = frozenset(
    {"admin", "bbs", "system", "me", "all", "help", "root", "operator", "op"}
)

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_MIN = 1
NAME_MAX = 10


def welcome_text(bbs_name: str) -> str:
    return (
        f"Welcome to {bbs_name}.\n"
        f"Choose a display name ({NAME_MIN}-{NAME_MAX} chars, A-Z 0-9 _ -).\n"
        f"Reply: NAME <yourname>"
    )


def must_name_first() -> str:
    return "! Set a name first: NAME <yourname>"


def validate_name(name: str) -> str | None:
    """Return None if the name is acceptable, else a reason string."""
    if not (NAME_MIN <= len(name) <= NAME_MAX):
        return f"! Name too long (max {NAME_MAX})" if len(name) > NAME_MAX else "! Name too short"
    if not NAME_PATTERN.match(name):
        return "! Bad chars, use A-Z 0-9 _ -"
    if name.lower() in RESERVED_NAMES:
        return "! Reserved name"
    return None


async def try_set_name(
    db: Database, pubkey: str, name: str, bbs_cfg: BBSConfig
) -> tuple[bool, str]:
    """Attempt to assign `name` to `pubkey`. Returns (ok, reply_text).

    Caller is expected to have ensured the user row exists.
    """
    err = validate_name(name)
    if err:
        return False, err

    ok = await db.set_display_name(pubkey, name)
    if not ok:
        return False, "! Name taken"

    await db.audit(pubkey, "onboarded", f"name={name}")
    await db.mark_motd_sent(pubkey)
    return True, f"OK, you are {name}. Try HELP for commands."
