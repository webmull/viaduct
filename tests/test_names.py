"""Unit tests for friendly subdomain name generation."""

from __future__ import annotations

import re

from viaduct import names

NAME_RE = re.compile(r"^[a-z]+-[a-z]+$")


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
