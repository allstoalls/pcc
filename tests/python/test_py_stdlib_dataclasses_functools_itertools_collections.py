from __future__ import annotations

from pcc.stdlib import dataclasses
from pcc.stdlib import functools
from pcc.stdlib import itertools
from pcc.stdlib import collections


def test_dataclasses_no_exec_defaults_asdict_replace_make():
    @dataclasses.dataclass(unsafe_hash=True)
    class Point:
        x: int
        y: int = 2
        z: list = dataclasses.field(default_factory=list, repr=False, compare=False)

    p = Point(1)
    q = Point(1, 2)
    assert p == q
    assert "z=" not in repr(p)
    assert dataclasses.asdict(p)["x"] == 1
    assert dataclasses.astuple(p)[:2] == (1, 2)
    assert dataclasses.replace(p, y=5).y == 5
    assert dataclasses.is_dataclass(Point)
    assert dataclasses.is_dataclass(p)
    assert hash(p) == hash((1, 2))

    C = dataclasses.make_dataclass("C", [("a", int), ("b", int, 3)])
    c = C(1)
    assert c.a == 1 and c.b == 3


def test_functools_wraps_lru_cache_cached_property_total_ordering_cmp_to_key():
    def original(x):
        "doc"
        return x + 1

    @functools.wraps(original)
    def wrapper(x):
        return original(x)

    assert wrapper.__name__ == "original"
    assert wrapper.__doc__ == "doc"

    calls = {"n": 0}

    @functools.lru_cache(maxsize=2)
    def fib(n):
        calls["n"] += 1
        if n < 2:
            return n
        return fib(n - 1) + fib(n - 2)

    assert fib(5) == 5
    info = fib.cache_info()
    assert info[1] > 0
    fib.cache_clear()
    assert fib.cache_info()[3] == 0

    class C:
        def __init__(self):
            self.calls = 0

        @functools.cached_property
        def value(self):
            self.calls += 1
            return 7

    c = C()
    assert c.value == 7 and c.value == 7
    assert c.calls == 1

    @functools.total_ordering
    class N:
        def __init__(self, v):
            self.v = v
        def __lt__(self, other):
            return self.v < other.v
        def __eq__(self, other):
            return self.v == other.v

    assert N(1) <= N(2)
    assert N(2) >= N(2)

    key = functools.cmp_to_key(lambda a, b: a - b)
    assert sorted([3, 1, 2], key=key) == [1, 2, 3]


def test_itertools_expanded_subset():
    assert list(itertools.chain.from_iterable([[1], [2, 3]])) == [1, 2, 3]
    assert list(itertools.accumulate([1, 2, 3])) == [1, 3, 6]
    assert list(itertools.takewhile(lambda x: x < 3, [1, 2, 3, 1])) == [1, 2]
    assert list(itertools.dropwhile(lambda x: x < 3, [1, 2, 3, 1])) == [3, 1]
    assert list(itertools.starmap(lambda a, b: a + b, [(1, 2), (3, 4)])) == [3, 7]
    assert list(itertools.compress("abcd", [1, 0, 1, 0])) == ["a", "c"]
    assert list(itertools.zip_longest([1, 2], ["a"], fillvalue=None)) == [(1, "a"), (2, None)]
    a, b = itertools.tee([1, 2])
    assert list(a) == [1, 2]
    assert list(b) == [1, 2]
    assert list(itertools.permutations([1, 2, 3], 2))[:2] == [(1, 2), (1, 3)]


def test_collections_expanded_subset():
    c = collections.Counter("aba")
    c.update({"a": 2})
    assert c["a"] == 4
    c.subtract("b")
    assert c["b"] == 0
    assert list(c.elements()).count("a") == 4

    d = collections.deque([1, 2], maxlen=3)
    d.append(3)
    d.append(4)
    assert list(d) == [2, 3, 4]
    d.appendleft(1)
    assert list(d) == [1, 2, 3]
    d.rotate(1)
    assert list(d) == [3, 1, 2]

    cm = collections.ChainMap({"a": 1}, {"b": 2})
    assert cm["a"] == 1
    assert cm["b"] == 2
    assert cm.new_child({"c": 3})["c"] == 3
