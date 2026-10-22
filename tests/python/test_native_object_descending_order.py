"""The radix relocation order matches the keyed stable descending sort."""
import random

import pytest

from pcc.backend import native_object


def _expected(offsets):
    return sorted(range(len(offsets)), key=lambda index: offsets[index], reverse=True)


@pytest.mark.parametrize("offsets", [
    [], [0], [5, 5, 5], [1, 2, 3], [3, 2, 1], [4, 9, 4, 0, 9, 9, 1],
    [0x7FFFFFFF, 0, 0x7FFFFFFF], [0x80000000, 1, 0x80000000], [2**64 - 1, 7, 7],
])
def test_descending_stable_order_matches_sorted(offsets):
    assert native_object._descending_stable_order(list(offsets)) == _expected(offsets)


def test_descending_stable_order_matches_sorted_on_random_tables():
    rng = random.Random(1019)
    for _ in range(300):
        size = rng.randrange(0, 200)
        span = rng.choice([4, 64, 4096, 0x7FFFFFFF])
        offsets = [rng.randrange(0, span) for _ in range(size)]
        assert native_object._descending_stable_order(offsets) == _expected(offsets)
