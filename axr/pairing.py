"""Pairing: checking a typed code before it is sent, and turning a code into a permanent device identity."""
from __future__ import annotations

import asyncio
import difflib
import re
from pathlib import Path

from .identity import Identity
from .transport import RelayError, Session

# Same list as WORDS in relay/relay.mjs (a test keeps them in sync). Codes are six of these joined by dashes.
WORDS = ("amber arch aspen basil birch bloom brook cedar cinder clay cloud comet coral crane delta dune ember fern "
         "finch flint fog frost gale glade grove harbor hazel heron iris ivory jade juniper kelp lark lilac linen lotus maple "
         "marsh meadow mint moss nectar oak olive onyx opal orchid pearl pine plume quartz raven reed ridge river robin sage "
         "shale slate sparrow spruce stone storm sun thistle tide vale willow wren").split()
CODE_WORDS = 6


def check_code(text: str) -> tuple[str | None, list[str]]:
    """Returns (normalised code, []) when it looks right, else (None, [what is wrong, with suggestions])."""
    words = [w for w in re.split(r"[\s\-_.,]+", (text or "").strip().lower()) if w]
    problems = []
    for w in words:
        if w not in WORDS:
            near = difflib.get_close_matches(w, WORDS, n=1, cutoff=0.6)
            problems.append(f"'{w}' is not a code word" + (f": did you mean '{near[0]}'?" if near else ""))
    if not problems and len(words) != CODE_WORDS:
        problems.append(f"a code has {CODE_WORDS} words, this has {len(words)}")
    return ("-".join(words), []) if not problems else (None, problems)


def fix_code(text: str) -> str | None:
    """Auto-corrects single obvious typos (one close match per word). None if it cannot be fixed."""
    words = [w for w in re.split(r"[\s\-_.,]+", (text or "").strip().lower()) if w]
    fixed = []
    for w in words:
        if w in WORDS:
            fixed.append(w)
            continue
        near = difflib.get_close_matches(w, WORDS, n=2, cutoff=0.75)
        if len(near) != 1:
            return None
        fixed.append(near[0])
    return "-".join(fixed) if len(fixed) == CODE_WORDS else None


async def pair(home: Path, code: str, relay: str, *, name: str | None = None, sandbox: str = "process", max_jobs: int | None = None) -> Identity:
    """Makes this device's keys, redeems the code at the relay and saves the identity. Raises RelayError/OSError."""
    ident = Identity.fresh(Path(home))
    sess = await Session.open(ident, join_code=code, name=name, relay=relay)
    try:
        ident.relay = relay
        if ident.role == "worker":
            ident.extra["worker"] = {"sandbox": sandbox, "max_jobs": max_jobs}
        ident.save()
    finally:
        await sess.close()
    return ident


def pair_sync(*a, **kw) -> Identity:
    return asyncio.run(pair(*a, **kw))


__all__ = ["WORDS", "check_code", "fix_code", "pair", "pair_sync", "RelayError"]
