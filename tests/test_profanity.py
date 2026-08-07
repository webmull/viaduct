"""Unit tests for the profanity screen used by --name validation.

Uses benign fixture tokens ("frob", "wibble", "cat") to exercise the matching
logic, so no real slurs live in the test source. One sanity check confirms the
shipped list does not false-positive our own example names.
"""

from __future__ import annotations

from viaduct import profanity


def _screen(*words: str) -> profanity.Screen:
    hashes = frozenset(profanity.hash_word(w) for w in words)
    return profanity.Screen(hashes, max(len(w) for w in words))


def test_blocks_whole_hyphen_segment() -> None:
    s = _screen("wibble", "cat")
    assert s.blocks("my-wibble-app")
    assert s.blocks("cat")  # the whole name is the word
    assert not s.blocks("my-app")


def test_blocks_embedded_substring() -> None:
    s = _screen("frob")
    assert s.blocks("xxfrobyy")  # embedded, length >= MIN_SUBSTRING
    assert s.blocks("frobnicate")  # substring at the start


def test_short_substring_not_matched_but_segment_is() -> None:
    # 'cat' (3 chars) is below MIN_SUBSTRING (4), so it is caught only as a whole
    # hyphen segment, never as an embedded substring: 'scatter' must pass.
    s = _screen("cat")
    assert not s.blocks("scatter")
    assert not s.blocks("locate")
    assert s.blocks("my-cat")


def test_embedded_matching_scoped_but_split_word_still_caught() -> None:
    s = _screen("frob", "damn")
    # a compound that merely embeds a word across a hyphen is allowed...
    assert not s.blocks("adam-name")  # 'adamname' is not itself a listed word
    # ...but a listed word split by hyphens is caught via the compact form
    assert s.blocks("f-r-o-b")  # de-hyphenated -> 'frob'
    assert s.blocks("fr-ob")  # de-hyphenated -> 'frob'
    # embedded within a single segment, and un-hyphenated, are both caught
    assert s.blocks("xxfrobyy")
    assert s.blocks("adamname")  # 'damn' embedded in one segment


def test_empty_screen_blocks_nothing() -> None:
    s = profanity.Screen(frozenset(), 0)
    assert not s.blocks("anything-goes")


def test_shipped_list_passes_clean_names() -> None:
    for clean in ("myfriendlysubdomain", "sunny-otter", "acme-staging", "adam-blog", "port3000"):
        assert not profanity.blocks(clean), clean
