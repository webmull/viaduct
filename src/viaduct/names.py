"""Friendly random subdomain names, e.g. ``funny-otter``.

Names are ``adjective-animal`` pairs drawn from curated wordlists. The server
assigns one to each tunnel at connect time and frees it when the tunnel drops,
so names are ephemeral, never persisted. ``secrets`` is used for selection so
names are not predictable from one another.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Container

ADJECTIVES: tuple[str, ...] = (
    "amber",
    "bold",
    "brave",
    "breezy",
    "bright",
    "brisk",
    "calm",
    "clever",
    "cosmic",
    "cozy",
    "crisp",
    "curious",
    "dapper",
    "daring",
    "eager",
    "fancy",
    "fluffy",
    "fresh",
    "funny",
    "gentle",
    "glossy",
    "hardy",
    "humble",
    "jolly",
    "keen",
    "lively",
    "lucky",
    "mellow",
    "merry",
    "mighty",
    "nimble",
    "noble",
    "peppy",
    "plucky",
    "quiet",
    "quirky",
    "rapid",
    "rustic",
    "scrappy",
    "shiny",
    "sleepy",
    "snappy",
    "spry",
    "sunny",
    "swift",
    "tidy",
    "vivid",
    "witty",
)

ANIMALS: tuple[str, ...] = (
    "otter",
    "lynx",
    "heron",
    "panda",
    "koala",
    "gecko",
    "finch",
    "tapir",
    "ibis",
    "marten",
    "quokka",
    "fennec",
    "badger",
    "beaver",
    "falcon",
    "ferret",
    "walrus",
    "wombat",
    "robin",
    "raven",
    "salmon",
    "seal",
    "sparrow",
    "stoat",
    "toad",
    "weasel",
    "wren",
    "bison",
    "crane",
    "dingo",
    "egret",
    "gibbon",
    "hare",
    "jackal",
    "lemur",
    "moose",
    "newt",
    "ocelot",
    "puffin",
    "mole",
    "pika",
    "civet",
    "coati",
    "macaw",
    "manta",
    "narwhal",
    "osprey",
    "shrew",
)


def random_name() -> str:
    """Return a fresh ``adjective-animal`` name."""
    return f"{secrets.choice(ADJECTIVES)}-{secrets.choice(ANIMALS)}"


def derived_name(seed: str) -> str:
    """Deterministic ``adjective-animal-xxxx`` name for a ``--pin`` seed.

    The same seed always yields the same name, with no server-side state, so a
    pinned tunnel keeps its URL across reconnects. The 48-bit hex suffix makes
    the full name space ~2^59, so distinct seeds effectively never collide and
    brute-forcing a seed that reproduces a specific target name is infeasible
    (finding a preimage over that space). The seed is opaque, so the name is
    still server-assigned rather than user-chosen.
    """
    digest = hashlib.sha256(seed.encode()).digest()
    n = int.from_bytes(digest, "big")
    adjective = ADJECTIVES[n % len(ADJECTIVES)]
    n //= len(ADJECTIVES)
    animal = ANIMALS[n % len(ANIMALS)]
    return f"{adjective}-{animal}-{digest[:6].hex()}"


def unique_name(taken: Container[str], attempts: int = 50) -> str:
    """Return a name not in *taken*.

    With thousands of combinations and only a handful of live tunnels, the
    first pick almost always lands. As a guaranteed fallback (in case the small
    space is somehow saturated), a short random suffix is appended.
    """
    for _ in range(attempts):
        name = random_name()
        if name not in taken:
            return name
    while True:
        name = f"{random_name()}-{secrets.token_hex(2)}"
        if name not in taken:
            return name
