"""Ratchet on the atomics surface of the remaining C and the pcc-Python ports.

The C runtime is retired; the C left under ``pcc/runtime/src`` is the
CPython bridge (``py_libpython.c``) and the C ABI headers, which the scans
below still cover.

History: until 2026-08-02 `pcc.unsafe` had no atomic intrinsic, so a port
reached atomics only through seven fixed C helpers in
`py_runtime_high_substrate.c` that pick memory ordering BY OPERAND WIDTH,
not by use (measured in
docs/goal/evidence/2026-08-01-atomics-mirror-gap-measured.md).

The intrinsic half of LIBC-P1-PRIMITIVES landed: `pcc.unsafe` now has
ordering-explicit `atomic_load/store/rmw/cas_{i32,i64}` plus `atomic_fence`
(tests/python/test_unsafe_atomics.py proves both backends). What this file
ratchets now:

1. the C runtime must not grow an atomic operation kind or a memory
   ordering the intrinsic surface cannot express;
2. the i8 byte-flag ops gained their intrinsic mirror on 2026-08-02
   (`atomic_test_and_set`/`atomic_clear` lower to `atomicrmw xchg i8` /
   `store atomic i8` on both backends) — every C atomic op kind is now
   expressible through `pcc.unsafe`, and the gap set must stay empty;
3. the legacy width-ordered C helpers were removed after the ports moved to
   ordering-explicit intrinsics; they must not reappear on either side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


def _repo_root() -> Path:
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found above " + __file__)


RUNTIME = _repo_root() / "pcc" / "runtime"
SRC = RUNTIME / "src"
PORT = RUNTIME / "py"

# Measured 2026-08-01, re-checked at intrinsic landing 2026-08-02. Everything
# here except test_and_set/clear is expressible through pcc.unsafe atomics
# (rmw returns the OLD value; *_fetch variants are old-value plus arithmetic).
KNOWN_C_ATOMIC_OPS = {
    "add_fetch",
    "and_fetch",
    "clear",
    "compare_exchange_n",
    "exchange_n",
    "fetch_add",
    "load_n",
    "or_fetch",
    "store_n",
    "sub_fetch",
    "test_and_set",
    "thread_fence",
}

# Every C atomic op kind now has an intrinsic mirror (i8 lanes landed
# 2026-08-02); this set must stay empty.
C_OPS_WITHOUT_INTRINSIC_MIRROR = set()

KNOWN_C_ATOMIC_ORDERINGS = {"RELAXED", "ACQUIRE", "RELEASE", "ACQ_REL"}

# The ordering-explicit intrinsic surface (pcc/unsafe/__init__.py). The
# frontend maps relaxed->monotonic and fails closed on anything else; see
# pcc/frontends/python/codegen/unsafe_lowering.py.
EXPECTED_UNSAFE_ATOMIC_INTRINSICS = {
    "atomic_load_i32",
    "atomic_load_i64",
    "atomic_store_i32",
    "atomic_store_i64",
    "atomic_rmw_i32",
    "atomic_rmw_i64",
    "atomic_cas_i32",
    "atomic_cas_i64",
    "atomic_fence",
    "atomic_test_and_set",
    "atomic_clear",
}

# The legacy width-ordered C helper mirror is intentionally empty.
KNOWN_PORT_HELPERS = set()


def _scan(paths, pattern):
    found = set()
    for path in paths:
        found.update(re.findall(pattern, path.read_text(errors="replace")))
    return found


def _c_sources():
    return sorted(SRC.glob("*.c")) + sorted(SRC.glob("*.h"))


def test_c_runtime_uses_no_atomic_operation_outside_the_pinned_set():
    ops = _scan(_c_sources(), r"__atomic_(\w+)")
    new = sorted(ops - KNOWN_C_ATOMIC_OPS)
    assert not new, (
        "the C runtime gained atomic operations; check each against the "
        "pcc.unsafe atomic intrinsics and extend the surface (or this pin) "
        "deliberately:\n  " + ", ".join(new)
    )


def test_every_c_atomic_op_has_an_intrinsic_mirror():
    """The whole remaining C atomic surface is expressible through
    pcc.unsafe. A new C op kind must land with its intrinsic (or a
    deliberate entry here) in the same change."""
    assert C_OPS_WITHOUT_INTRINSIC_MIRROR == set()
    ops = _scan(_c_sources(), r"__atomic_(\w+)")
    assert ops <= KNOWN_C_ATOMIC_OPS


def test_c_runtime_uses_no_memory_ordering_outside_the_known_set():
    orderings = _scan(_c_sources(), r"__ATOMIC_(\w+)")
    new = sorted(orderings - KNOWN_C_ATOMIC_ORDERINGS)
    assert not new, (
        "new memory orderings in the C runtime; extend the intrinsic "
        "ordering table and its fail-closed tests first:\n  " + ", ".join(new)
    )


def test_the_removed_port_helper_mirror_stays_absent():
    """Width-selected C helpers must not return after intrinsic migration."""
    defined = _scan(_c_sources(), r"\b(pcc_py_atomic_\w+)\s*\(")
    declared = _scan(sorted(PORT.glob("*.py")), r'"(pcc_py_atomic_\w+)"')
    assert defined == KNOWN_PORT_HELPERS, sorted(defined ^ KNOWN_PORT_HELPERS)
    assert declared == KNOWN_PORT_HELPERS, sorted(declared ^ KNOWN_PORT_HELPERS)


def test_pcc_unsafe_atomic_surface_is_pinned():
    """The intrinsic surface this ratchet is written against.

    Growing it (an i8 lane, a new rmw op, a new ordering) is welcome —
    update EXPECTED_UNSAFE_ATOMIC_INTRINSICS, the gap set above, and the
    fail-closed tests in test_unsafe_atomics.py in the same change.
    """
    unsafe = (_repo_root() / "pcc" / "unsafe" / "__init__.py").read_text()
    names = set(re.findall(r"^def (\w+)", unsafe, re.M))
    atomic_names = {n for n in names if "atomic" in n or "fence" in n}
    assert atomic_names == EXPECTED_UNSAFE_ATOMIC_INTRINSICS, sorted(
        atomic_names ^ EXPECTED_UNSAFE_ATOMIC_INTRINSICS
    )


@pytest.mark.parametrize(
    "port_name, required_ir",
    [
        (
            "py_gc_backend.py",
            (
                "load atomic i32",
                "store atomic i32",
                "atomicrmw add ptr",
                "load atomic i64",
                "store atomic i64",
                "cmpxchg ptr",
                " monotonic",
                " acquire",
                " release",
                " acq_rel acquire",
            ),
        ),
        ("py_gc_telemetry.py", ("load atomic i32", " monotonic")),
    ],
)
def test_gc_ports_emit_ordering_explicit_intrinsics_not_c_helpers(
    tmp_path, monkeypatch, port_name, required_ir
):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = PORT / port_name
    output = tmp_path / (port_name + ".ll")
    compile_python(
        str(source),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    ir_text = output.read_text(encoding="utf-8")
    assert "@pcc_py_atomic_" not in ir_text
    for needle in required_ir:
        assert needle in ir_text, (port_name, needle)
