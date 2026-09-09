"""Generated suffixes must not turn sparse name indexes into dense probe runs."""

from pcc.backend.self_backend_kernel import IndexedFunctionSeed
from pcc.backend.self_backend_value_arena import CompilerIntArena


def test_repeated_phi_suffixes_do_not_scan_the_numeric_name_cluster(monkeypatch):
    reads = 0
    original = CompilerIntArena.get2_unchecked

    def counted(self, record_index):
        nonlocal reads
        reads += 1
        return original(self, record_index)

    monkeypatch.setattr(CompilerIntArena, "get2_unchecked", counted)
    seed = IndexedFunctionSeed(value_capacity_hint=3000)
    names = ["value." + str(i) for i in range(2048)]
    # mem2reg gives different bases their own phi ordinal. The final numeric
    # component therefore cannot be assumed unique within the function.
    names += ["local." + str(i) + ".phi.1024" for i in range(80)]
    for index, name in enumerate(names):
        assert seed.intern_value(name) == index
    for index, name in enumerate(names):
        assert seed.value_id(name) == index
        assert seed.intern_value(name) == index
    assert seed.value_id("missing.phi.1024") == -1
    # A work bound, not a clock threshold: this sparse table must not scan
    # thousands of neighboring numeric names for each repeated suffix.
    bound = len(names) * 20
    assert reads < bound, (reads, bound)


def test_name_indexes_preserve_numeric_aliases_collisions_and_growth():
    seed = IndexedFunctionSeed()
    assert seed.intern_value(".7") == seed.intern_value("%.7")
    first = seed.intern_value("left.7")
    second = seed.intern_value("right.7")
    assert first != second
    for i in range(128):
        seed.register_block("block." + str(i))
        seed.intern_value("value." + str(i))
    assert seed.value_id(".7") == seed.value_id("%.7")
    assert seed.value_id("left.7") == first
    assert seed.value_id("right.7") == second
    assert seed.value_id("absent.7") == -1
    for i in range(128):
        assert seed.block_id("block." + str(i)) == i
    assert seed.block_id("absent.7") == -1
