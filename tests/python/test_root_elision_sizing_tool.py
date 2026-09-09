"""Focused contract for scripts/pcc_root_elision_sizing.py.

The window semantics these tests pin were each wrong once during sizing:

* readers are `pcc_gc_load_ptr` calls carrying the slot as an argument — root
  slots are never read by a plain `load`, so a load-based scan reports zero
  windows and a value-based scan reports a vacuous 100%;
* a later `store_root` to the same slot ends the window — slots are reused, so
  whole-slot reasoning has an empty domain (400 single-def slots out of 9,201
  stores on the representative module);
* one dirty path to a read kills the window — a value living only in a
  register while a GC point runs on any path is a use-after-free on the
  earliest-moving backend.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from pcc1_gate import repo_root
from pcc.backend.self_backend_ir import (
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    TypeDesc,
)

SCRIPT = repo_root() / "scripts" / "pcc_root_elision_sizing.py"


def _tool():
    spec = importlib.util.spec_from_file_location("root_elision_sizing_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_PTR = TypeDesc(kind="ptr", width=0, pointee=None, count=0, elem=None, name="", fields=())
_VOID = TypeDesc(kind="void", width=0, pointee=None, count=0, elem=None, name="", fields=())


def _call(callee, args=(), dest=None):
    return ParsedInstr(
        kind="call",
        data=(dest, _VOID, callee, False, tuple((_PTR, a) for a in args)),
    )


def _store_root(slot, value):
    return _call("pcc_gc_store_root", (slot, value))


def _read(slot, dest):
    return _call("pcc_gc_load_ptr", ("owner", slot), dest=dest)


def _block(name, instructions, terminator):
    return ParsedBlock(
        name=name, raw_lines=[], phis=[], instructions=instructions,
        terminator=terminator,
    )


def _br(label):
    return ParsedInstr(kind="br", data=(label,))


def _br_cond(cond, then_label, else_label):
    return ParsedInstr(kind="br_cond", data=(cond, then_label, else_label))


def _ret():
    return ParsedInstr(kind="ret_void", data=())


def _function(blocks):
    return ParsedFunction(
        name="probe", ret_type=_VOID, args=[], is_global=False,
        is_vararg=False, blocks=blocks,
    )


def test_clean_single_window_is_elidable():
    func = _function([
        _block("entry", [_store_root("slot", "v"), _read("slot", "%r")], _ret()),
    ])
    row = _tool().function_sizing(func)
    assert row == {"stores": 1, "single_def": 1, "elidable": 1}


def test_unsafe_call_before_the_read_kills_the_window():
    func = _function([
        _block(
            "entry",
            [_store_root("slot", "v"), _call("py_list_new"), _read("slot", "%r")],
            _ret(),
        ),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 0
    assert row["single_def"] == 1  # the read is still inside the window


def test_one_dirty_path_kills_even_when_a_clean_path_exists():
    func = _function([
        _block("entry", [_store_root("slot", "v")], _br_cond("c", "clean", "dirty")),
        _block("clean", [], _br("join")),
        _block("dirty", [_call("py_list_new")], _br("join")),
        _block("join", [_read("slot", "%r")], _ret()),
    ])
    row = _tool().function_sizing(func)
    assert row == {"stores": 1, "single_def": 1, "elidable": 0}


def test_restore_ends_the_window_and_windows_count_separately():
    func = _function([
        _block(
            "entry",
            [
                _store_root("slot", "v1"),
                _call("py_list_new"),
                _store_root("slot", "v2"),
                _read("slot", "%r"),
            ],
            _ret(),
        ),
    ])
    row = _tool().function_sizing(func)
    # Window 1 has no read of its own (the unsafe call would have killed it
    # anyway); window 2 is clean and elidable.
    assert row == {"stores": 2, "single_def": 1, "elidable": 1}


def test_read_barrier_is_whitelisted_inside_the_window():
    func = _function([
        _block(
            "entry",
            [
                _store_root("a", "v"),
                _read("a", "%r1"),
                _call("pcc_gc_load_ptr", ("owner", "other")),
                _read("a", "%r2"),
            ],
            _ret(),
        ),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 1


def test_release_is_not_whitelisted():
    # py_decref reaching zero dispatches __del__, and a finalizer can allocate.
    func = _function([
        _block(
            "entry",
            [_store_root("slot", "v"), _call("pcc_gc_release", ("x",)), _read("slot", "%r")],
            _ret(),
        ),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 0


def _switch(value, default_label, cases):
    return ParsedInstr(
        kind="switch",
        data=(_PTR, value, default_label, tuple(cases)),
    )


def test_other_slot_store_root_kills_the_window():
    # store_root decrefs the slot's OLD value: a finalizer (and thus a GC)
    # can run, so a store_root to a DIFFERENT slot is a GC point (P1-6).
    func = _function([
        _block(
            "entry",
            [
                _store_root("slot", "v"),
                _store_root("other", "w"),
                _read("slot", "%r"),
            ],
            _ret(),
        ),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 0
    assert row["single_def"] == 1


def test_switch_successors_are_not_dropped():
    # The old `_all_labels` fallback returned () for switch terminators and
    # silently dropped the dirty path behind a case edge (P1-6).
    func = _function([
        _block("entry", [_store_root("slot", "v")], _switch("x", "clean", [(1, "dirty")])),
        _block("clean", [], _br("join")),
        _block("dirty", [_call("py_list_new")], _br("join")),
        _block("join", [_read("slot", "%r")], _ret()),
    ])
    row = _tool().function_sizing(func)
    assert row == {"stores": 1, "single_def": 1, "elidable": 0}


def test_dirty_path_without_a_reload_still_vetoes():
    # The frame release at exit uses the slot value, so a GC point on a path
    # with no reload still leaves a stale register copy on a moving backend.
    func = _function([
        _block("entry", [_store_root("slot", "v")], _br_cond("c", "clean", "dirty")),
        _block("clean", [_read("slot", "%r")], _ret()),
        _block("dirty", [_call("py_list_new")], _ret()),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 0
    assert row["single_def"] == 1


def test_unknown_terminator_vetoes_instead_of_dropping_paths():
    unknown = ParsedInstr(kind="indirectbr", data=("x",))
    func = _function([
        _block("entry", [_store_root("slot", "v"), _read("slot", "%r")], unknown),
    ])
    row = _tool().function_sizing(func)
    assert row["elidable"] == 0


def test_a_parsed_module_reaches_function_sizing_at_all(tmp_path):
    """The tool must see real bodies, not silently report zero.

    Every test above hands `function_sizing` a hand-built ParsedFunction, so
    none of them exercised the path from IR text to sizing.  The parser stopped
    populating `ParsedFunction.blocks` when the indexed kernel landed -- bodies
    live in the packed representation and a consumer asks for the legacy
    projection through `materialize_legacy_blocks`.  The tool's
    `if not func.blocks: continue` therefore skipped every function of every
    real module and printed `store_root=0` for all of them, which reads as "no
    opportunity" rather than "not measured".

    This asserts the end-to-end path finds the store_root sites that are
    plainly in the text.
    """
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module

    source = """
target triple = "arm64-apple-macosx15.0.0"

define void @probe(ptr %owner, ptr %v) {
entry:
  %slot = alloca ptr, align 8
  call void @pcc_gc_store_root(ptr %slot, ptr %v)
  %r = call ptr @pcc_gc_load_ptr(ptr %owner, ptr %slot)
  ret void
}

declare void @pcc_gc_store_root(ptr, ptr)
declare ptr @pcc_gc_load_ptr(ptr, ptr)
"""
    path = tmp_path / "probe.ll"
    path.write_text(source.lstrip(), encoding="utf-8")
    module = parse_self_backend_module(path.read_text(encoding="utf-8"))
    functions = [f for f in module.functions if f.name == "probe"]
    assert len(functions) == 1, [f.name for f in module.functions]
    func = functions[0]
    # The defect: a freshly parsed function carries no legacy blocks.
    assert not func.blocks
    get_indexed_function_kernel(func).materialize_legacy_blocks(func)
    assert func.blocks, "materialize_legacy_blocks must project the body"
    row = _tool().function_sizing(func)
    assert row["stores"] == 1, row
