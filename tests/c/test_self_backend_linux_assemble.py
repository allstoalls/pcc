"""x86_64-linux self-backend assemble-only gate (default suite).

The docker harness (tests/integration/test_self_backend_x86_64_linux.py)
builds AND runs on an emulated Linux, but is `-m integration` and rotted
unnoticed for weeks (docs/investigations/linux-x86-64-docker-harness-rot.md
No.4). This gate keeps a cheap signal in EVERY default run: the asm the
linux emitter produces must actually ASSEMBLE with a cross targeting
clang (text-shape assertions elsewhere cannot catch assembler-invalid
operand forms or directives). Skipped only when no clang is on PATH.

Run + execution semantics remain the docker harness's job; this gate
makes silent emitter rot visible, nothing more.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm

_CLANG = shutil.which("clang")

# Known-supported emitter shapes, borrowed from the text-shape tests in
# tests/c/test_self_backend.py so this gate never outruns the subset.
_IR_SNIPPETS = {
    "ret_const": """
target triple = "x86_64-unknown-linux-gnu"

define i32 @main() {
entry:
  ret i32 42
}
""",
    "direct_call_binop": """
target triple = "x86_64-unknown-linux-gnu"

define i32 @add(i32 %a, i32 %b) {
entry:
  %sum = add i32 %a, %b
  ret i32 %sum
}

define i32 @main() {
entry:
  %r = call i32 (i32, i32) @add(i32 40, i32 2)
  ret i32 %r
}
""",
    "global_double_compare_zext": """
target triple = "x86_64-unknown-linux-gnu"

@x = global double 1.000000e+02

define i32 @main() {
bb0:
  %.1 = load double, ptr @x
  %cmptmp = fcmp olt double %.1, 1.000000e+00
  %booltmp = zext i1 %cmptmp to i32
  ret i32 %booltmp
}
""",
    "initial_exec_tls": """
target triple = "x86_64-unknown-linux-gnu"

@tls_init = thread_local global i32 37, align 4
@tls_zero = thread_local(initialexec) global ptr null, align 8

define i32 @tls_read() {
entry:
  %value = load i32, ptr @tls_init, align 4
  ret i32 %value
}

define void @tls_write(i32 %value) {
entry:
  store i32 %value, ptr @tls_init, align 4
  ret void
}

define ptr @tls_zero_address() {
entry:
  ret ptr @tls_zero
}
""",
    "smul_with_overflow_i64": """
target triple = "x86_64-unknown-linux-gnu"

define i64 @main() {
entry:
  %r = call { i64, i1 } @llvm.smul.with.overflow.i64(i64 7, i64 6)
  %v = extractvalue { i64, i1 } %r, 0
  %o = extractvalue { i64, i1 } %r, 1
  %z = zext i1 %o to i64
  %sum = add i64 %v, %z
  ret i64 %sum
}

declare { i64, i1 } @llvm.smul.with.overflow.i64(i64, i64)
""",
    "unsigned_float_casts_and_fcmp_ord": """
target triple = "x86_64-unknown-linux-gnu"

define i64 @main() {
entry:
  %u32 = add i32 4000000000, 0
  %d32 = uitofp i32 %u32 to double
  %b32 = fptoui double %d32 to i32
  %f32 = uitofp i32 %b32 to float
  %r32 = fptoui float %f32 to i32
  %u64 = add i64 -1, 0
  %d64 = uitofp i64 %u64 to double
  %b64 = fptoui double %d64 to i64
  %ord = fcmp ord double %d32, %d32
  %zord = zext i1 %ord to i64
  %r32w = zext i32 %r32 to i64
  %s0 = add i64 %b64, %zord
  %sum = add i64 %s0, %r32w
  ret i64 %sum
}
""",
    # X-P0-SELFX86-DATA-VEC: struct globals carrying vector/aggregate fields
    # whose constant initializers use LLVM's angle-bracket vector-literal form
    # (`<i32 1, i32 2, ...>`). Before the data-emitter fix this raised
    # BackendUnavailable ("expected array initializer ... got '<...>'") because
    # the vector-as-array branch only accepted the `[...]` literal form.
    "vector_aggregate_globals": """
target triple = "x86_64-unknown-linux-gnu"

@s = global { <4 x i32>, i32 } { <4 x i32> <i32 1, i32 2, i32 3, i32 4>, i32 9 }
@fv = global { <2 x float>, i32 } { <2 x float> <float 1.5, float 2.5>, i32 7 }
@bv = global { <8 x i8>, i16 } { <8 x i8> <i8 1, i8 2, i8 3, i8 4, i8 5, i8 6, i8 7, i8 8>, i16 99 }
@av = global { [2 x <2 x i32>], i8 } { [2 x <2 x i32>] [<2 x i32> <i32 11, i32 22>, <2 x i32> <i32 33, i32 44>], i8 3 }

define i32 @main() {
entry:
  ret i32 0
}
""",
}


@pytest.mark.pcc_gate(unavailable=None if _CLANG is not None else "clang not on PATH")
@pytest.mark.parametrize("name", sorted(_IR_SNIPPETS))
def test_linux_emitted_asm_cross_assembles(tmp_path, name):
    asm_text = emit_x86_64_linux_asm(_IR_SNIPPETS[name].strip())
    asm_path = tmp_path / f"{name}.s"
    obj_path = tmp_path / f"{name}.o"
    asm_path.write_text(asm_text, encoding="utf-8")
    result = subprocess.run(
        [
            _CLANG,
            "-target",
            "x86_64-unknown-linux-gnu",
            "-c",
            str(asm_path),
            "-o",
            str(obj_path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"cross-assemble failed for {name}:\n{result.stderr}\n--- asm ---\n"
        f"{asm_text}"
    )
    magic = obj_path.read_bytes()[:4]
    assert magic == b"\x7fELF", magic


def test_unlowered_intrinsic_fails_fast_instead_of_emitting_call():
    """An un-lowered `llvm.*` intrinsic must raise BackendUnavailable at
    emit time — the previous behavior emitted a literal `call llvm.foo`,
    which ASSEMBLES (dotted ELF symbols are legal) and only fails at
    link time. (smul.with.overflow and later ctpop used to hit this; both
    lower natively now, so the probe uses bitreverse, which has no x86-64
    lowering.)"""
    from pcc.backend import BackendUnavailable

    ir = """
target triple = "x86_64-unknown-linux-gnu"

define i64 @main() {
entry:
  %r = call i64 @llvm.bitreverse.i64(i64 7)
  ret i64 %r
}

declare i64 @llvm.bitreverse.i64(i64)
""".strip()
    with pytest.raises(BackendUnavailable, match="no native lowering"):
        emit_x86_64_linux_asm(ir)


def test_smul_overflow_lowers_natively_with_imul_seto():
    asm = emit_x86_64_linux_asm(_IR_SNIPPETS["smul_with_overflow_i64"].strip())
    assert "imul r10, r11" in asm
    assert "seto r11b" in asm
    assert "call llvm." not in asm
    # Semantic check ran in the docker harness 2026-06-12: 7*6 (flag 0)
    # plus an overflowing 3037000500^2 (flag 1) exited 43 as expected.


def test_unsigned_float_casts_lower_natively_no_backend_unavailable():
    """uitofp/fptoui and `fcmp ord` used to raise BackendUnavailable
    ('... not translated yet'); they must now lower to native SSE
    conversion sequences with no un-translated raise reaching emit."""
    asm = emit_x86_64_linux_asm(
        _IR_SNIPPETS["unsigned_float_casts_and_fcmp_ord"].strip()
    )
    assert "not translated yet" not in asm
    # uitofp for a <64-bit source zero-extends into the full 64-bit GP reg,
    # then a plain signed cvtsi2sd is exact (clang's mov eax,edi / cvtsi2sd).
    assert "cvtsi2sd xmm10, r10" in asm
    # 64-bit uitofp takes the sign-branch fixup (halve-then-double) rather
    # than a naive signed convert of a possibly-negative-as-signed value.
    assert "js .Lmain_" in asm
    assert "addsd xmm10, xmm10" in asm
    # fptoui reuses the truncating-to-signed convert; the 64-bit path folds
    # in the 2^63 subtraction fixup constant.
    assert "cvttsd2si" in asm
    assert "0x43e0000000000000" in asm
    # fcmp ord => not-parity after ucomisd (ordered = neither operand NaN).
    assert "setnp al" in asm
    assert "call llvm." not in asm
    # Semantic values were confirmed against CPython/clang reference for this
    # slice (u32 4e9 -> double -> 4000000000; u64 -1 -> 1.8446744073709552e19
    # -> 18446744073709551615; fcmp ord of a finite double with itself = 1).


def test_vector_aggregate_globals_emit_lane_data_no_backend_unavailable():
    """X-P0-SELFX86-DATA-VEC: an LLVM vector-literal global initializer
    (`<i32 1, i32 2, ...>`) — including vectors nested inside struct/array
    globals — must emit each lane into the data section rather than raising
    BackendUnavailable ('expected array initializer ... got <...>').

    Reference (clang -S for the same IR) emits each lane as an individual
    .long/.byte/.float directive; pcc emits them into its own self-consistent
    layout. This asserts the lane data is present and un-translated raises no
    longer reach emit; the cross-assemble gate above proves the directives
    actually assemble."""
    asm = emit_x86_64_linux_asm(_IR_SNIPPETS["vector_aggregate_globals"].strip())
    assert "expected array initializer" not in asm
    # <4 x i32> lanes 1..4 followed by the trailing i32 9 field.
    for lane in (".long 1", ".long 2", ".long 3", ".long 4", ".long 9"):
        assert lane in asm, f"missing {lane!r} in:\n{asm}"
    # <2 x float> lanes.
    assert ".float 1.5" in asm
    assert ".float 2.5" in asm
    # <8 x i8> lanes emit as .byte directives (1..8), i16 field as .short 99.
    assert ".byte 1" in asm
    assert ".byte 8" in asm
    assert ".short 99" in asm
    # Nested [2 x <2 x i32>] flattens to all four lanes.
    for lane in (".long 11", ".long 22", ".long 33", ".long 44"):
        assert lane in asm, f"missing nested {lane!r} in:\n{asm}"


def test_vector_literal_element_count_mismatch_still_reported():
    """The <...> normalization must not silently mask a lane-count mismatch:
    a `<4 x i32>` typed field given only three lanes must still raise the
    array-item-count BackendUnavailable, not emit wrong-sized data."""
    from pcc.backend import BackendUnavailable

    ir = """
target triple = "x86_64-unknown-linux-gnu"

@s = global { <4 x i32>, i32 } { <4 x i32> <i32 1, i32 2, i32 3>, i32 9 }

define i32 @main() {
entry:
  ret i32 0
}
""".strip()
    with pytest.raises(BackendUnavailable, match="array items"):
        emit_x86_64_linux_asm(ir)
