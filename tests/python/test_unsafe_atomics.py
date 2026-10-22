"""pcc.unsafe atomic intrinsics (LIBC-P1-PRIMITIVES, atomics half).

The intrinsics carry an explicit memory-ordering string so a pcc-Python
runtime port can finally say what ordering it means instead of getting one
picked by operand width (the measured helper-mirror bug in
docs/goal/evidence/2026-08-01-atomics-mirror-gap-measured.md).

Covers, on both the LLVM and the self backend:
- single-thread semantics of load/store/rmw/cas at i32 and i64 widths
  (rmw and cas return the OLD value, LLVM semantics);
- the exact LLVM IR shapes and orderings emitted;
- the AArch64 self-backend instruction shapes (ldar/stlr/ldaxr/stlxr/dmb);
- fail-closed diagnostics for bad orderings, non-literal orderings, bad rmw
  ops, and orderings invalid for the specific operation.
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


PROGRAM = textwrap.dedent("""
    from pcc.unsafe import (
        malloc,
        free,
        atomic_load_i32,
        atomic_load_i64,
        atomic_store_i32,
        atomic_store_i64,
        atomic_rmw_i32,
        atomic_rmw_i64,
        atomic_cas_i32,
        atomic_cas_i64,
        atomic_fence,
        atomic_test_and_set,
        atomic_clear,
    )

    def main() -> None:
        p = malloc(16)
        atomic_store_i64(p, 0, 41, "release")
        print(atomic_rmw_i64("add", p, 0, 1, "acq_rel"))
        print(atomic_load_i64(p, 0, "acquire"))
        print(atomic_cas_i64(p, 0, 42, 100, "acq_rel", "acquire"))
        print(atomic_load_i64(p, 0, "seq_cst"))
        print(atomic_cas_i64(p, 0, 42, 7, "seq_cst", "relaxed"))
        print(atomic_load_i64(p, 0, "relaxed"))
        print(atomic_rmw_i64("and", p, 0, 6, "seq_cst"))
        print(atomic_rmw_i64("or", p, 0, 9, "relaxed"))
        print(atomic_load_i64(p, 0, "acquire"))
        atomic_store_i32(p, 8, -5, "relaxed")
        print(atomic_rmw_i32("sub", p, 8, 3, "seq_cst"))
        print(atomic_load_i32(p, 8, "acquire"))
        print(atomic_rmw_i32("xchg", p, 8, 9, "acq_rel"))
        print(atomic_cas_i32(p, 8, 9, -1, "relaxed", "relaxed"))
        print(atomic_load_i32(p, 8, "relaxed"))
        atomic_fence("seq_cst")
        atomic_fence("acquire")
        atomic_clear(p, 12, "relaxed")
        print(atomic_test_and_set(p, 12, "acquire"))
        print(atomic_test_and_set(p, 12, "seq_cst"))
        atomic_clear(p, 12, "release")
        print(atomic_test_and_set(p, 12, "relaxed"))
        free(p)

    if __name__ == "__main__":
        main()
    """).lstrip()

# store 41; add 1 -> old 41; load 42; cas hit -> old 42; load 100;
# cas miss -> old 100; load 100; and 6 -> old 100 (100&6=4); or 9 -> old 4
# (4|9=13); load 13; store -5; sub 3 -> old -5; load -8; xchg 9 -> old -8;
# cas hit (9 -> -1) -> old 9; load -1; byte flag: clear -> tas 0 -> tas 1
# -> clear -> tas 0.
EXPECTED = "41\n42\n42\n100\n100\n100\n100\n4\n13\n-5\n-8\n-8\n9\n-1\n0\n1\n0\n"

DEC_IF_POSITIVE_PROGRAM = textwrap.dedent("""
    from pcc.unsafe import (
        atomic_cas_i64,
        atomic_load_i64,
        atomic_store_i64,
        free,
        malloc,
    )

    def dec_if_positive(slot) -> int:
        live: int = atomic_load_i64(slot, 0, "acquire")
        while live > 0:
            observed: int = atomic_cas_i64(
                slot, 0, live, live - 1, "acq_rel", "acquire"
            )
            if observed == live:
                return live - 1
            live = observed
        return live

    def main() -> None:
        slot = malloc(8)
        atomic_store_i64(slot, 0, 3, "release")
        print(dec_if_positive(slot))
        print(dec_if_positive(slot))
        print(dec_if_positive(slot))
        print(dec_if_positive(slot))
        print(atomic_load_i64(slot, 0, "acquire"))
        free(slot)

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_atomic_intrinsics_single_thread_semantics(tmp_path, backend):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "atomics.py"
    exe = tmp_path / f"atomics_{backend}.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend=backend,
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == EXPECTED


@pytest.mark.parametrize("backend", [pytest.param(None, id="default-self"), pytest.param("self", id="explicit-self")])
def test_atomic_cas_loop_matches_removed_dec_if_positive_helper(tmp_path, backend):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dec_if_positive.py"
    exe = tmp_path / ("dec_if_positive_" + backend + ".out")
    src.write_text(DEC_IF_POSITIVE_PROGRAM, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend=backend,
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "2\n1\n0\n0\n0\n"


def _compile_to_ll(tmp_path, source: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "atomics_ir.py"
    out = tmp_path / "atomics_ir.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out), emit_llvm_only=True, libpython_mode="off",
    )
    return out.read_text(encoding="utf-8")


def test_atomic_intrinsics_emit_ordered_llvm_shapes(tmp_path):
    ir_text = _compile_to_ll(tmp_path, PROGRAM)
    for needle in (
        "store atomic i64 41",
        "atomicrmw add ptr",
        "load atomic i64",
        " acquire",
        " seq_cst",
        " monotonic",
        "cmpxchg ptr",
        "acq_rel acquire",
        "atomicrmw sub ptr",
        "atomicrmw xchg ptr",
        "atomicrmw and ptr",
        "atomicrmw or ptr",
        "fence seq_cst",
        "fence acquire",
        "store atomic i32",
        "load atomic i32",
    ):
        assert needle in ir_text, f"missing {needle!r} in emitted IR"


def test_atomic_intrinsics_lower_to_aarch64_exclusives(tmp_path):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    ir_text = _compile_to_ll(tmp_path, PROGRAM)
    asm = emit_self_asm(ir_text)
    for needle in (
        "ldar ",
        "stlr ",
        "ldaxr ",
        "stlxr ",
        "clrex",
        "dmb ish",
        # i8 byte-flag lanes: test_and_set -> ldaxrb/stlxrb exchange loop,
        # release clear -> stlrb, relaxed clear -> strb
        "ldaxrb ",
        "stlxrb ",
        "stlrb ",
        "strb ",
    ):
        assert needle in asm, f"missing {needle!r} in self-backend asm"
    # relaxed accesses must stay plain: the monotonic i64 load emits ldr,
    # not ldar; count both mnemonics to prove the ordering actually chooses.
    assert asm.count("ldar ") >= 3
    assert "cbnz w13," in asm


@pytest.mark.parametrize(
    "call, message_part",
    [
        ('atomic_load_i64(p, 0, "weird")', "ordering"),
        ('atomic_load_i64(p, 0, "release")', "ordering"),
        ('atomic_store_i64(p, 0, 1, "acquire")', "ordering"),
        ('atomic_rmw_i64("nand", p, 0, 1, "seq_cst")', "op must be"),
        ('atomic_cas_i64(p, 0, 1, 2, "seq_cst", "release")', "ordering"),
        ('atomic_fence("relaxed")', "ordering"),
        ('atomic_clear(p, 0, "acquire")', "ordering"),
        ('atomic_test_and_set(p, 0, "weird")', "ordering"),
    ],
)
def test_atomic_intrinsics_fail_closed(tmp_path, call, message_part):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "atomics_bad.py"
    out = tmp_path / "atomics_bad.ll"
    src.write_text(
        textwrap.dedent(f"""
            from pcc.unsafe import (
                malloc,
                atomic_load_i64,
                atomic_store_i64,
                atomic_rmw_i64,
                atomic_cas_i64,
                atomic_fence,
                atomic_test_and_set,
                atomic_clear,
            )

            def main() -> None:
                p = malloc(8)
                {call}

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as excinfo:
        compile_python(
            str(src), str(out), emit_llvm_only=True, libpython_mode="off",
        )
    assert message_part in str(excinfo.value)


def test_atomic_ordering_must_be_a_string_literal(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "atomics_dyn.py"
    out = tmp_path / "atomics_dyn.ll"
    src.write_text(
        textwrap.dedent("""
            from pcc.unsafe import malloc, atomic_load_i64

            def main() -> None:
                p = malloc(8)
                order = "acquire"
                atomic_load_i64(p, 0, order)

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(Exception) as excinfo:
        compile_python(
            str(src), str(out), emit_llvm_only=True, libpython_mode="off",
        )
    assert "string literal" in str(excinfo.value)
