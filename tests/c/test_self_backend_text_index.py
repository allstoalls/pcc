"""Generated suffixes must not turn sparse name indexes into dense probe runs."""

import pytest

from pcc.backend import self_backend_kernel as kernel_module
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


def _original_ensure_value_name_index(self):
    """The previous activation algorithm, retained as a host-only oracle."""
    if self.value_name_index_active:
        return
    old_index = self.value_name_index
    (
        self.value_name_index,
        self.value_name_index_capacity,
    ) = kernel_module._rebuilt_text_id_index(
        self.value_names,
        max(1, (len(self.value_names) + 1) * 2),
    )
    old_index.close()
    self.value_name_index_active = True


def _seed_logical_state(seed, queries):
    # Table capacity/physical probe placement may change; semantic columns
    # and lookup IDs must not. No native allocation, compiler or emitter runs.
    return (
        tuple(seed.value_names), tuple(seed.definition_blocks),
        tuple(seed.definition_positions), tuple(seed.value_type_ids),
        tuple(seed.alloca_type_ids), tuple(seed.value_is_used_flags),
        seed.first_duplicate_definition_value_id,
        tuple(seed.value_id(name) for name in queries), seed.complete,
    )


@pytest.mark.parametrize("activation", ("lookup", "intern", "finish"))
def test_empty_seed_keeps_zeroed_hint_table_on_activation(activation):
    for hint in (0, 1, 8, 64):
        seed = IndexedFunctionSeed(value_capacity_hint=hint)
        table, capacity = seed.value_name_index, seed.value_name_index_capacity
        assert seed.value_names == [] and not seed.value_name_index_active
        assert len(table) == capacity * 2
        assert all(table.get_unchecked(index) == 0 for index in range(len(table)))
        if activation == "lookup":
            assert seed.value_id("absent") == -1
        elif activation == "intern":
            assert seed.intern_value("first") == 0
        else:
            seed.finish()
            assert seed.complete
        assert seed.value_name_index_active and seed.value_name_index is table
        assert seed.value_name_index_capacity == capacity
        assert len(table) == capacity * 2 and not table._closed
        seed._ensure_value_name_index()
        assert seed.value_name_index is table
        assert seed.intern_value("first") == 0
        assert seed.value_id("first") == 0 and seed.value_id("absent") == -1


def test_hint_activation_matches_original_ids_aliases_duplicates_and_growth(monkeypatch):
    names = [".7", "%.7", "left.7", "right.7", "", "value.007"]
    names += ["value." + str(index) for index in range(40)]
    names += ["local." + str(index) + ".phi.1" for index in range(16)]
    queries = names + ["missing.7", "missing.phi.1", "%.99"]

    def exercise(hint):
        seed = IndexedFunctionSeed(value_capacity_hint=hint)
        ids = [seed.intern_value(name) for name in names]
        assert ids[0] == ids[1] and ids[2] != ids[3]
        assert ids[4] != ids[5]
        for index, name in enumerate(names):
            seed.define_value(name, index % 3, position=index)
        seed.define_value("right.7", 9, position=99)
        assert seed.first_duplicate_definition_value_id == ids[0]
        seed.finish()
        return ids, _seed_logical_state(seed, queries)

    for hint in (0, 4, 128):
        actual = exercise(hint)
        with monkeypatch.context() as control:
            control.setattr(IndexedFunctionSeed, "_ensure_value_name_index", _original_ensure_value_name_index)
            expected = exercise(hint)
        assert actual == expected


def test_preappended_names_keep_original_rebuild_and_close_boundary(monkeypatch):
    rebuild = kernel_module._rebuilt_text_id_index
    seen = []

    def counted(names, minimum_size):
        seen.append((tuple(names), minimum_size))
        return rebuild(names, minimum_size)

    monkeypatch.setattr(kernel_module, "_rebuilt_text_id_index", counted)
    for append in ("proven", "columns"):
        seed = IndexedFunctionSeed(value_capacity_hint=64)
        initial = seed.value_name_index
        for index, name in enumerate((".7", "left.7", "right.7")):
            value_id = (seed.append_proven_new_value(name) if append == "proven"
                        else seed._append_value_columns(name))
            assert value_id == index
        assert not seed.value_name_index_active and not initial._closed
        seed._ensure_value_name_index()
        assert seed.value_name_index is not initial and initial._closed
        assert not seed.value_name_index._closed and seed.value_name_index_active
        assert seen[-1] == ((".7", "left.7", "right.7"), 8)
        assert seed.value_id("%.7") == 0
        assert seed.intern_value("left.7") == 1
        assert seed.append_proven_new_value("next") == 3
        before = len(seen)
        seed.finish()
        assert len(seen) == before and seed.value_id("next") == 3
        actual = _seed_logical_state(seed, ("%.7", "left.7", "right.7", "next", "absent"))
        with monkeypatch.context() as control:
            control.setattr(IndexedFunctionSeed, "_ensure_value_name_index", _original_ensure_value_name_index)
            expected_seed = IndexedFunctionSeed(value_capacity_hint=64)
            for name in (".7", "left.7", "right.7"):
                if append == "proven":
                    expected_seed.append_proven_new_value(name)
                else:
                    expected_seed._append_value_columns(name)
            expected_seed._ensure_value_name_index()
            expected_seed.append_proven_new_value("next")
            expected_seed.finish()
            expected = _seed_logical_state(expected_seed, ("%.7", "left.7", "right.7", "next", "absent"))
        assert actual == expected
    assert len(seen) == 4


def test_hint_reuse_avoids_initial_allocation_and_reinsert_work(monkeypatch):
    allocate = kernel_module._new_text_id_index
    rebuild = kernel_module._rebuilt_text_id_index
    insert = kernel_module._text_id_index_insert

    def exercise(original):
        allocations, rebuilds, insertions = [], [], []
        def counted_allocate(size):
            result = allocate(size)
            allocations.append(result[1] * 2)
            return result
        def counted_rebuild(names, minimum_size):
            rebuilds.append(len(names))
            return rebuild(names, minimum_size)
        def counted_insert(index, capacity, names, name_id):
            insertions.append(name_id)
            return insert(index, capacity, names, name_id)
        with monkeypatch.context() as control:
            control.setattr(kernel_module, "_new_text_id_index", counted_allocate)
            control.setattr(kernel_module, "_rebuilt_text_id_index", counted_rebuild)
            control.setattr(kernel_module, "_text_id_index_insert", counted_insert)
            if original:
                control.setattr(IndexedFunctionSeed, "_ensure_value_name_index", _original_ensure_value_name_index)
            seed = IndexedFunctionSeed(value_capacity_hint=64)
            names = ["value." + str(index) for index in range(48)]
            for index, name in enumerate(names):
                assert seed.intern_value(name) == index
            seed.finish()
            state = _seed_logical_state(seed, names)
        return state, allocations, rebuilds, insertions

    actual, old = exercise(False), exercise(True)
    assert actual[0] == old[0]
    assert len(actual[1]) == 2 and actual[2] == [] and len(actual[3]) == 48
    assert old[2] == [0, 3, 7, 15, 31]
    assert len(old[1]) == 2 + len(old[2])
    assert len(old[3]) == 48 + sum(old[2])
    assert sum(actual[1]) < sum(old[1])


_SMALL_PARSER_INPUTS = (
    """define i64 @simple(i64 %value) {
entry:
  %slot = alloca i64
  store i64 %value, ptr %slot
  %loaded = load i64, ptr %slot
  %result = call i64 (i64) @callee(i64 %loaded)
  ret i64 %result
}
""",
    """define i64 @join(i64 %value, i64 %tag) {
entry:
  switch i64 %tag, label %other [ i64 0, label %join i64 1, label %join ]
other:
  br label %join
join:
  %result = phi i64 [ %value, %entry ], [ %value, %entry ], [ 9, %other ]
  ret i64 %result
dead:
  %unused = add i64 1, 2
  ret i64 %unused
}
""",
    """define i64 @bad(i64 %value) {
entry:
  %result = unsupported i64 %value
  ret i64 %result
}
""",
)


def _small_parser_result(text):
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_parse import parse_self_backend_module

    try:
        module = parse_self_backend_module(
            'target triple = "arm64-apple-macosx13.0.0"\n' + text,
        )
    except BackendUnavailable as error:
        return ("error", type(error), str(error))
    planes = (
        "block_facts", "instruction_facts", "instruction_kind_ids", "instruction_metadata",
        "instruction_record_dest_ids", "instruction_record_scalars", "gep_index_scalars",
        "gep_scalars", "instruction_overflow_use_ids", "call_arg_scalars", "call_scalars",
        "type_field_ids", "type_scalars", "terminator_case_scalars", "terminator_scalars",
        "block_phi_facts", "phi_incoming_scalars", "phi_scalars", "error_edge_scalars",
        "error_edge_spans", "error_landing_scalars", "value_scalars", "definition_positions",
        "used_value_ids",
    )
    functions = []
    for function in module.functions:
        kernel = function.indexed_kernel
        assert kernel is not None and function.indexed_seed is None
        records = tuple((name, tuple(getattr(kernel, name).get_unchecked(index)
                                    for index in range(len(getattr(kernel, name))))) for name in planes)
        functions.append((function.name, function.ret_type, function.args,
                          tuple(kernel.block_names), tuple(kernel.value_names), tuple(kernel.types),
                          tuple(kernel.call_texts), kernel.instruction_arithmetic_flag_values,
                          tuple(kernel.cold_instruction_data), kernel.first_duplicate_definition_value_id,
                          tuple(kernel.value_id(name) for name in kernel.value_names), records))
    return ("parsed", module.triple, functions)


@pytest.mark.parametrize("text", _SMALL_PARSER_INPUTS, ids=("calls-memory", "parallel-phi-unreachable", "invalid-instruction"))
def test_hint_reuse_preserves_small_parser_records_and_errors(monkeypatch, text):
    actual = _small_parser_result(text)
    with monkeypatch.context() as control:
        control.setattr(IndexedFunctionSeed, "_ensure_value_name_index", _original_ensure_value_name_index)
        expected = _small_parser_result(text)
    assert actual == expected
    assert actual[0] == ("error" if "unsupported" in text else "parsed")
