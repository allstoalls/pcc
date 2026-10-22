"""Final stack-map record ordering must not depend on the arena's storage form.

`_sort_final_stack_map_records` orders four-word (pc, safepoint id, index,
exceptional offset) records by final PC then safepoint id.  On pcc1 the arena
holds native scalar storage and the heapsort runs on the raw words; under
CPython the arena keeps a list and the arena-method heapsort runs.  Both must
produce the same order, so the host oracle here pins the contract that the
pcc1 replays check byte-for-byte on emitted stack maps.
"""

from __future__ import annotations

import random

from pcc.backend import self_backend_precise_stackmaps as stackmaps
from pcc.backend.self_backend_value_arena import CompilerIntArena


def _records(arena: CompilerIntArena) -> list[tuple[int, int, int, int]]:
    count = len(arena) // 4
    return [
        tuple(arena.get_unchecked(index * 4 + word) for word in range(4))
        for index in range(count)
    ]


def _unique_rows(seed: int, count: int) -> list[tuple[int, int, int, int]]:
    rng = random.Random(seed)
    seen: set[tuple[int, int]] = set()
    rows: list[tuple[int, int, int, int]] = []
    for index in range(count):
        pc = rng.randrange(0, 48) * 4
        safepoint_id = rng.randrange(0, 1 << 20) if rng.random() < 0.7 else index
        while (pc, safepoint_id) in seen:
            safepoint_id += 1
        seen.add((pc, safepoint_id))
        rows.append((pc, safepoint_id, index, rng.randrange(-1, 1 << 20)))
    return rows


def test_sort_orders_by_pc_then_id_and_keeps_every_payload_word():
    rows = _unique_rows(20260906, 1201)
    arena = CompilerIntArena()
    for row in rows:
        arena.append4(*row)
    stackmaps._sort_final_stack_map_records(arena)
    assert _records(arena) == sorted(rows, key=lambda row: (row[0], row[1]))
    # This process is the CPython oracle: the list storage path ran here and
    # the native kernel is exercised by pcc1 replays of real modules.
    assert not arena.uses_native_storage
    assert arena.native_address() == 0


def test_sort_handles_empty_single_and_already_sorted_inputs():
    empty = CompilerIntArena()
    stackmaps._sort_final_stack_map_records(empty)
    assert _records(empty) == []

    single = CompilerIntArena()
    single.append4(16, 3, 0, -1)
    stackmaps._sort_final_stack_map_records(single)
    assert _records(single) == [(16, 3, 0, -1)]

    rows = sorted(_unique_rows(7, 257), key=lambda row: (row[0], row[1]))
    ordered = CompilerIntArena()
    for row in rows:
        ordered.append4(*row)
    stackmaps._sort_final_stack_map_records(ordered)
    assert _records(ordered) == rows


def test_already_ordered_records_need_no_swaps(monkeypatch):
    rows = sorted(_unique_rows(19, 4096), key=lambda row: (row[0], row[1]))
    arena = CompilerIntArena()
    for row in rows:
        arena.append4(*row)

    def unexpected_swap(*args):
        raise AssertionError("already ordered records must not be heap-sorted")

    monkeypatch.setattr(stackmaps, "_swap_final_stack_map_records", unexpected_swap)
    stackmaps._sort_final_stack_map_records(arena)
    assert _records(arena) == rows


def test_equal_keys_retain_the_original_heap_order():
    # Equal (PC, id) pairs are not strictly ordered. Keep the old sequence of
    # swaps, including its tie ordering, until semantic validation rejects them.
    arena = CompilerIntArena()
    arena.append4(4, 11, 0, -1)
    arena.append4(4, 11, 1, 17)
    stackmaps._sort_final_stack_map_records(arena)
    assert _records(arena) == [(4, 11, 1, 17), (4, 11, 0, -1)]


def test_ordered_fast_path_and_heap_fallback_execute_under_all_collectors(
    tmp_path, monkeypatch, python_program_compiler, pcc_runtime_archive,
):
    import inspect
    import os
    from pathlib import Path
    import subprocess
    from pcc.backend import self_backend_value_arena

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    rows = [(index // 3 * 4, (1 << 60) + index, index, -1) for index in range(129)]
    reverse = list(reversed(rows))
    duplicate = [(4, 11, 0, -1), (4, 11, 1, 17)]
    cases = [(rows, rows, False), (reverse, rows, True), (duplicate, list(reversed(duplicate)), True)]
    source = tmp_path / "stackmap_sort.py"
    functions = (
        stackmaps._final_stack_map_record_greater, stackmaps._swap_final_stack_map_records,
        stackmaps._swap_final_stack_map_records_native,
        stackmaps._sift_down_final_stack_map_records_native, stackmaps._sort_final_stack_map_records,
    )
    text = Path(self_backend_value_arena.__file__).read_text() + '''
from pcc.unsafe import load_i64 as _record_load_i64
from pcc.unsafe import store_i64 as _record_store_i64
from pcc.unsafe import int_to_ptr as _record_int_to_ptr
'''
    for function in functions:
        body = inspect.getsource(function)
        if function is stackmaps._swap_final_stack_map_records_native:
            body = body.replace("def _swap_final_stack_map_records_native(", "def _original_native_swap(", 1)
        text += "\n" + body
    text += '''
swap_count = 0
def _swap_final_stack_map_records_native(address: int, left: int, right: int) -> None:
    global swap_count
    swap_count += 1
    _original_native_swap(address, left, right)
'''
    text += "\ncases = " + repr(cases) + '''
def main():
    global swap_count
    for rows, expected, should_swap in cases:
        arena = CompilerIntArena()
        assert arena.uses_native_storage
        for row in rows:
            arena.append4(row[0], row[1], row[2], row[3])
        swap_count = 0
        _sort_final_stack_map_records(arena)
        assert (swap_count > 0) == should_swap
        for index in range(len(expected)):
            for word in range(4):
                assert arena.get_unchecked(index * 4 + word) == expected[index][word]
        arena.close()
    print("stackmap-sort-ok")
main()
'''
    source.write_text(text)
    binary = tmp_path / "stackmap_sort"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout.strip() == "stackmap-sort-ok"


def test_native_sift_down_matches_arena_method_sift_down_on_a_fake_heap():
    """Drive the native kernel through a raw-memory stand-in on the host.

    `pcc.unsafe` loads raise under CPython, so the kernel is exercised with a
    bytearray-backed stand-in for its three raw operations and compared with
    the arena-method heapsort on identical input.
    """
    import struct

    rows = _unique_rows(99, 513)
    reference = CompilerIntArena()
    for row in rows:
        reference.append4(*row)
    stackmaps._sort_final_stack_map_records(reference)
    expected = _records(reference)

    memory = bytearray()
    for row in rows:
        memory += struct.pack("<qqqq", *row)
    address = 0x1000  # the kernel receives the arena address as an exact int

    def to_ptr(value):
        assert value == address
        return ("ptr", value)

    def load(base, offset):
        assert base == ("ptr", address)
        return struct.unpack_from("<q", memory, offset)[0]

    def store(base, offset, value):
        assert base == ("ptr", address)
        struct.pack_into("<q", memory, offset, value)

    saved = (
        stackmaps._record_int_to_ptr,
        stackmaps._record_load_i64,
        stackmaps._record_store_i64,
    )
    stackmaps._record_int_to_ptr = to_ptr
    stackmaps._record_load_i64 = load
    stackmaps._record_store_i64 = store
    try:
        count = len(rows)
        start = count // 2 - 1
        while start >= 0:
            stackmaps._sift_down_final_stack_map_records_native(address, start, count)
            start -= 1
        end = count - 1
        while end > 0:
            stackmaps._swap_final_stack_map_records_native(address, 0, end * 32)
            stackmaps._sift_down_final_stack_map_records_native(address, 0, end)
            end -= 1
    finally:
        (
            stackmaps._record_int_to_ptr,
            stackmaps._record_load_i64,
            stackmaps._record_store_i64,
        ) = saved
    native = [
        struct.unpack_from("<qqqq", memory, index * 32) for index in range(len(rows))
    ]
    assert native == expected
