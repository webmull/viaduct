"""Unit tests for friendly subdomain name generation."""

from __future__ import annotations

import re

from viaduct import names

NAME_RE = re.compile(r"^[a-z]+-[a-z]+$")
PINNED_RE = re.compile(r"^[a-z]+-[a-z]+-[0-9a-f]{12}$")


def test_random_name_shape() -> None:
    for _ in range(100):
        name = names.random_name()
        assert NAME_RE.fullmatch(name), name
        adj, animal = name.split("-")
        assert adj in names.ADJECTIVES
        assert animal in names.ANIMALS


def test_unique_name_avoids_taken() -> None:
    taken = {names.random_name() for _ in range(200)}
    for _ in range(200):
        assert names.unique_name(taken) not in taken


def test_unique_name_falls_back_when_space_exhausted() -> None:
    # Pretend every plain adjective-animal is taken: the fallback must still
    # return something outside the set (a suffixed name).
    everything = {f"{a}-{b}" for a in names.ADJECTIVES for b in names.ANIMALS}
    name = names.unique_name(everything, attempts=5)
    assert name not in everything
    assert name.count("-") >= 2  # got a random suffix


def test_derived_name_shape_and_wordlists() -> None:
    for seed in ("a", "seed-1", "x" * 64, ""):
        name = names.derived_name(seed)
        assert PINNED_RE.fullmatch(name), name
        adjective, animal, _suffix = name.split("-")
        assert adjective in names.ADJECTIVES
        assert animal in names.ANIMALS


def test_derived_name_is_deterministic() -> None:
    assert names.derived_name("hunter2") == names.derived_name("hunter2")


def test_derived_name_varies_by_seed() -> None:
    names_seen = {names.derived_name(f"seed-{i}") for i in range(500)}
    # Distinct seeds almost never collide over the widened space.
    assert len(names_seen) >= 495


def test_validate_custom_accepts_clean_labels() -> None:
    for good in ("myfriendlysubdomain", "adam-blog", "acme-staging", "app123", "a-b-c"):
        assert names.validate_custom(good) == good


def test_validate_custom_normalises_case_and_whitespace() -> None:
    assert names.validate_custom("  My-App  ") == "my-app"


def test_validate_custom_rejects_bad_syntax() -> None:
    for bad in (
        "ab",              # too short
        "x" * 64,          # too long
        "-lead",           # leading hyphen
        "trail-",          # trailing hyphen
        "dou--ble",        # double hyphen
        "under_score",     # illegal char
        "space name",      # space
        "12345",           # all-numeric
        "Bad!",            # punctuation
    ):
        assert names.validate_custom(bad) is None, bad


def test_validate_custom_rejects_reserved() -> None:
    for reserved in ("nyc", "lon", "www", "api", "admin", "viaduct"):
        assert names.validate_custom(reserved) is None, reserved
