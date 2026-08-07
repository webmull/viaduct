"""Server-side profanity screen for custom subdomains (``--name``).

Words come from a public multi-language list
(github.com/censor-text/profanity-list). Only *hashes* of the words are shipped
(regenerate with ``tools/build_profanity.py``) so a plaintext slur list never
lands in this repo; the hashing is obfuscation, not security. A requested name
is "exploded" per hyphen segment: each segment is checked as a whole (any
length) and for embedded substrings (of at least ``MIN_SUBSTRING``). Matching
never spans a hyphen, so ``adam-name`` is fine while ``adamname`` is caught.

Substring matching is deliberately blunt and can still flag an innocent name
whose segment contains a bad word (the "Scunthorpe problem"); the minimum
substring length and the per-segment scope curb the worst of it. This runs
server-side only: the client ships no word list and the server is authoritative.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

#: mixed into every hash so the file is not a plain sha256 rainbow-table lookup.
#: Not a secret (it ships here); hashing only keeps plaintext slurs out of the repo.
PEPPER = b"viaduct-profanity-v1"

#: substrings shorter than this are not matched, to curb false positives
#: ("class" -> "ass"); shorter words are still caught as whole hyphen segments.
MIN_SUBSTRING = 4

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DATA = Path(__file__).with_name("data") / "profanity.txt"


def _norm(text: str) -> str:
    """Lowercase and strip everything but ``[a-z0-9]`` (so ``f-u.c_k`` -> ``fuck``)."""
    return _NON_ALNUM.sub("", text.lower())


def hash_word(word: str) -> str:
    return hashlib.sha256(PEPPER + word.encode()).hexdigest()[:16]


class Screen:
    """A loaded set of word hashes plus the longest word length in it."""

    def __init__(self, hashes: frozenset[str], maxlen: int) -> None:
        self._hashes = hashes
        self._maxlen = max(maxlen, MIN_SUBSTRING)

    def blocks(self, name: str) -> bool:
        if not self._hashes:
            return False
        # Matching is scoped to each hyphen segment, so a word never spans a
        # hyphen ("adam-name" is fine; "adamname" is not). Within a segment:
        # an exact whole-segment match (catches short words), plus embedded
        # substrings of at least MIN_SUBSTRING (catches "xxfrobyy").
        for seg in name.lower().split("-"):
            seg = _norm(seg)
            n = len(seg)
            if not n:
                continue
            if hash_word(seg) in self._hashes:
                return True
            for i in range(n):
                end = min(i + self._maxlen, n)
                for j in range(i + MIN_SUBSTRING, end + 1):
                    if hash_word(seg[i:j]) in self._hashes:
                        return True
        return False


def load(path: Path = _DATA) -> Screen:
    hashes: set[str] = set()
    maxlen = 0
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                m = re.search(r"maxlen=(\d+)", line)
                if m:
                    maxlen = int(m.group(1))
                continue
            hashes.add(line)
    return Screen(frozenset(hashes), maxlen)


_default: Screen | None = None


def default() -> Screen:
    global _default
    if _default is None:
        _default = load()
    return _default


def blocks(name: str) -> bool:
    """True if *name* contains a screened word. Uses the shipped list."""
    return default().blocks(name)
