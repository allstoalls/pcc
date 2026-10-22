from __future__ import annotations

import ast
from pathlib import Path

from pcc.frontends.python.codegen.unsafe_lowering import (
    UNSAFE_INTRINSICS,
    _UNSAFE_INTRINSIC_FAMILIES,
    _unsafe_intrinsic_family,
)


REPO = Path(__file__).absolute().parents[2]
SOURCE = REPO / "pcc" / "frontends" / "python" / "codegen" / "unsafe_lowering.py"

HELPERS = (
    "_emit_unsafe_va_numeric_f64",
    "_emit_unsafe_page_global_alloc",
    "_emit_unsafe_ptr_memory",
    "_emit_unsafe_read_write_rss",
    "_emit_unsafe_file_mutation",
    "_emit_unsafe_socket_control",
    "_emit_unsafe_socket_io_poll",
    "_emit_unsafe_system_info",
    "_emit_unsafe_time_process_control",
    "_emit_unsafe_spawn_process_pipe",
    "_emit_unsafe_spawn_process",
    "_emit_unsafe_env_access_stat",
    "_emit_unsafe_indirect_calls_a",
    "_emit_unsafe_indirect_calls_b",
    "_emit_unsafe_loader_waitset_gc",
)


# These routes return before the bounded family dispatcher. Keep an explicit
# inventory rather than accepting every name left over from the family table.
DIRECT_DISPATCH_INTRINSICS = frozenset({
    "c_abi_alignof",
    "c_abi_sizeof",
    "call_c_abi",
    "call_void_i32",
    "directory_close",
    "directory_entry_type",
    "directory_error",
    "directory_next",
    "directory_open",
    "windows_full_path",
    "windows_real_path",
    "windows_tempdir_create",
    "windows_tempdir_remove_owned",
    "windows_tempdir_close_owned",
})


def _unsafe_mixin_methods() -> dict[str, ast.FunctionDef]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    mixin = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "UnsafeIntrinsicMixin"
    )
    return {
        node.name: node
        for node in mixin.body
        if isinstance(node, ast.FunctionDef)
    }


def _direct_dispatch_intrinsics() -> tuple[str, ...]:
    names = []
    dispatcher = _unsafe_mixin_methods()["_emit_unsafe_intrinsic_call"]
    for statement in dispatcher.body:
        if (isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Call)
                and isinstance(statement.value.func, ast.Name)
                and statement.value.func.id == "_unsafe_intrinsic_family"):
            return tuple(names)
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if not (isinstance(test, ast.Compare)
                and isinstance(test.left, ast.Name)
                and test.left.id == "intrinsic" and len(test.ops) == 1):
            continue
        operand = test.comparators[0]
        if isinstance(test.ops[0], ast.Eq):
            literals = [operand]
        elif isinstance(test.ops[0], ast.In) and isinstance(operand, ast.Tuple):
            literals = operand.elts
        else:
            raise AssertionError("unclassified direct intrinsic guard")
        assert all(isinstance(item, ast.Constant) and isinstance(item.value, str)
                   for item in literals)
        assert isinstance(statement.body[-1], ast.Return)
        names.extend(item.value for item in literals)
    raise AssertionError("unsafe dispatcher lost its bounded family lookup")


def test_unsafe_intrinsic_families_are_complete_unique_and_fail_closed() -> None:
    flattened = tuple(
        intrinsic
        for family in _UNSAFE_INTRINSIC_FAMILIES
        for intrinsic in family
    )
    assert len(_UNSAFE_INTRINSIC_FAMILIES) == len(HELPERS)
    assert len(flattened) == len(set(flattened))

    specialized = {
        intrinsic
        for intrinsic in UNSAFE_INTRINSICS
        if intrinsic.startswith("atomic_") or intrinsic.startswith("define_")
    }
    direct = _direct_dispatch_intrinsics()
    assert len(direct) == len(set(direct))
    assert set(direct) == DIRECT_DISPATCH_INTRINSICS
    assert set(flattened).isdisjoint(specialized | DIRECT_DISPATCH_INTRINSICS)
    assert specialized.isdisjoint(DIRECT_DISPATCH_INTRINSICS)
    assert set(flattened) | specialized | DIRECT_DISPATCH_INTRINSICS == set(UNSAFE_INTRINSICS)

    for expected_family, family in enumerate(_UNSAFE_INTRINSIC_FAMILIES):
        for intrinsic in family:
            assert _unsafe_intrinsic_family(intrinsic) == expected_family
    for intrinsic in specialized | DIRECT_DISPATCH_INTRINSICS:
        assert _unsafe_intrinsic_family(intrinsic) == -1
    assert _unsafe_intrinsic_family("not_a_real_unsafe_intrinsic") == -1


def test_unsafe_intrinsic_dispatch_and_helpers_stay_bounded() -> None:
    methods = _unsafe_mixin_methods()
    dispatcher = methods["_emit_unsafe_intrinsic_call"]
    assert dispatcher.end_lineno is not None
    assert dispatcher.end_lineno - dispatcher.lineno + 1 <= 85

    for helper_name in HELPERS:
        helper = methods[helper_name]
        assert helper.end_lineno is not None
        assert helper.end_lineno - helper.lineno + 1 <= 430, helper_name
