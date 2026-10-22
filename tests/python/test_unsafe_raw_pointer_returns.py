"""Unsafe intrinsics that return raw C pointers never enter managed ownership.

Codegen decides "raw pointer, not an owned object" from
``_UNSAFE_RAW_POINTER_RETURNS``.  An intrinsic missing from that set is lowered
as a managed reference wherever its result flows through a join: a conditional
expression retained it with ``pcc_gc_retain``, rooted it, and released it at
function exit.  On Windows ``os.path.abspath`` then released the 128 KiB path
buffer after ``free`` had returned it to the system and faulted (0xC0000005).
"""
from __future__ import annotations

from pathlib import Path
import re

from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen import ownership_lowering
from pcc.frontends.python.owned_runtime_build import _compile_runtime_module
from pcc.frontends.python.types import DynType


ROOT = Path(__file__).resolve().parents[2]

# Intrinsics whose lowering returns a raw pointer although type inference
# leaves their result dynamic (they are not in _POINTER_INTRINSICS).
_DYNAMIC_RAW_POINTER_INTRINSICS = frozenset({
    "windows_full_path", "windows_real_path", "directory_open", "directory_next",
})


def test_every_pointer_producing_intrinsic_is_a_raw_pointer_return():
    raw = ownership_lowering._UNSAFE_RAW_POINTER_RETURNS
    returns = type_infer._UNSAFE_INTRINSIC_RETURN_TYPES
    pointer_producers = {
        name for name in type_infer._POINTER_INTRINSICS
        # A declared non-dynamic result (dynamic_library_close -> int) is not
        # a pointer even though the name is listed as pointer-related.
        if name not in returns or isinstance(returns[name], DynType)
    }
    assert sorted(pointer_producers - raw) == []
    assert sorted(_DYNAMIC_RAW_POINTER_INTRINSICS - raw) == []
    for name in _DYNAMIC_RAW_POINTER_INTRINSICS:
        assert isinstance(returns[name], DynType), name


def _function_ir(text: str, name: str) -> str:
    match = re.search(r"^define [^\n]*@" + re.escape(name) + r"\(.*?^}", text, re.M | re.S)
    assert match, name
    return match.group(0)


def test_windows_abspath_does_not_manage_the_full_path_buffer(tmp_path):
    runtime = ROOT / "pcc" / "runtime"
    output = tmp_path / "py_os_path.ll"
    _compile_runtime_module(
        "py_os_path", str(runtime / "py" / "py_os_path.py"), str(output),
        "x86_64-pc-windows-msvc",
    )
    body = _function_ir(output.read_text(), "user_py_os_path__path_abspath_build")
    assert "@pcc_win_full_path(" in body
    # The buffer pointer is raw memory: no retain, no root slot, no barriers.
    assert "ternary.then.retain" not in body
    assert "ternary.else.retain" not in body
    assert "@pcc_gc_resolve_owned_ptr(" not in body
    assert "%result.addr" not in body
