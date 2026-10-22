
from __future__ import annotations

from pcc.library.persistent import PMap, PVector


def test_pvector_updates_are_persistent():
    v0 = PVector()
    v1 = v0.append(1)
    v2 = v1.set(0, 2)
    assert list(v0) == []
    assert list(v1) == [1]
    assert list(v2) == [2]


def test_pmap_updates_are_persistent_and_ordered():
    m0 = PMap()
    m1 = m0.set("a", 1)
    m2 = m1.set("a", 2).set("b", 3)
    assert m0.get("a") is None
    assert m1.get("a") == 1
    assert m2.items() == (("a", 2), ("b", 3))
