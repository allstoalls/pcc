"""GC indexes must grow without a fourfold allocation at half occupancy."""

import ast
import os
from pathlib import Path
import subprocess

import pytest


def _capacity_function():
    source = Path(__file__).resolve().parents[2] / "pcc/runtime/py/freestanding_gc_index_table.py"
    tree = ast.parse(source.read_text())
    names = {"pcc_gc_index_py_next_pow2", "pcc_gc_index_py_rehash_capacity"}
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == 2
    for node in functions:
        node.decorator_list = []
    namespace = {"i64": int, "logical_shift_right_i64": lambda value, shift: value >> shift}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["pcc_gc_index_py_rehash_capacity"]


@pytest.mark.parametrize("capacity", [256, 16384, 1 << 24])
def test_single_insert_at_load_limit_only_doubles_capacity(capacity):
    choose = _capacity_function()
    # The preceding insert filled exactly half the old table. One more key
    # must fit at <=50% load without allocating four times the old storage.
    assert choose(capacity, capacity // 2, 256) == capacity * 2


def test_capacity_compaction_keeps_minimum_and_lookup_headroom():
    choose = _capacity_function()
    assert choose(4096, 8, 256) == 256
    assert choose(4096, 1024, 256) == 4096


def test_slot_size_query_has_only_its_exact_raw_gc_signature():
    from pcc.frontends.python.pipeline_freestanding import freestanding_gc_cross_object_runtime_imports

    assert freestanding_gc_cross_object_runtime_imports(
        'size = extern("pcc_gc_index_slot_size", (), c_int64)\n'
    ) == {"pcc_gc_index_slot_size"}
    for parameters, result in [("(c_ptr,)", "c_int64"), ("()", "c_ptr")]:
        assert not freestanding_gc_cross_object_runtime_imports(
            f'size = extern("pcc_gc_index_slot_size", {parameters}, {result})\n'
        )


def test_native_index_growth_collision_and_deletion_budget(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "index_budget.py"
    source.write_text('''from pcc.extern import extern, c_ptr, c_rawptr, c_int64, c_void
from pcc.unsafe import calloc, free, ptr_add, ptr_eq, load_i64
size = extern("pcc_gc_index_slot_size", (), c_int64)
grow = extern("pcc_gc_index_py_rehash_capacity", (c_int64, c_int64, c_int64), c_int64)
insert = extern("pcc_gc_index_py_insert", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64, c_int64), c_int64)
find = extern("pcc_gc_index_py_find", (c_ptr, c_ptr, c_ptr, c_int64), c_rawptr)
remove = extern("pcc_gc_index_py_remove", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_int64), c_rawptr)
clear = extern("pcc_gc_index_py_clear", (c_ptr, c_ptr, c_ptr, c_ptr), c_void)
def main():
    assert size() == 16
    assert grow(16777216, 8388608, 256) == 33554432
    slots = calloc(1, 8)
    cap = calloc(1, 8)
    count = calloc(1, 8)
    used = calloc(1, 8)
    keys = calloc(256, 8)
    nodes = calloc(256, 8)
    for index in range(129):
        key = ptr_add(keys, index * 8)
        node = ptr_add(nodes, index * 8)
        assert insert(slots, cap, count, used, key, node, 1, 0, 256) == 1
        assert insert(slots, cap, count, used, key, node, 1, 0, 256) == 0
    assert load_i64(cap, 0) == 512
    assert load_i64(count, 0) == 129
    for index in range(0, 129, 2):
        key = ptr_add(keys, index * 8)
        node = ptr_add(nodes, index * 8)
        assert ptr_eq(remove(slots, cap, count, used, key, 1), node)
    for index in range(1, 129, 2):
        assert ptr_eq(find(slots, cap, ptr_add(keys, index * 8), 1), ptr_add(nodes, index * 8))
    for index in range(0, 129, 2):
        assert insert(slots, cap, count, used, ptr_add(keys, index * 8), ptr_add(nodes, index * 8), 1, 0, 256) == 1
    assert load_i64(count, 0) == 129
    clear(slots, cap, count, used)
    assert load_i64(cap, 0) == 0
    assert load_i64(count, 0) == 0
    free(slots)
    free(cap)
    free(count)
    free(used)
    free(keys)
    free(nodes)
    print("index-budget-ok")
main()
''')
    binary = tmp_path / "index_budget"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == "index-budget-ok\n"
