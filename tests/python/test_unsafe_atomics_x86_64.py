"""x86_64-linux self-backend atomic lowering (LIBC-P1-PRIMITIVES).

The aarch64-darwin lowering is covered by ``test_unsafe_atomics.py``;
this file pins the x86-TSO mapping the x86_64-linux emitter uses:

- ``load atomic``  -> plain ``mov`` (every aligned x86 load is acquire)
- ``store atomic`` -> ``mov``; seq_cst -> implicitly locked ``xchg``
- ``atomicrmw``    -> ``lock xadd`` (add/sub via ``neg``), ``xchg``,
                      and a ``lock cmpxchg`` retry loop for and/or
- ``cmpxchg``      -> ``lock cmpxchg`` + ``sete`` into the {T, i1} pair
- ``fence``        -> ``mfence`` only for seq_cst

Real-machine semantics are proven by the docker differential in
``tests/integration/test_self_backend_x86_64_linux.py`` via the shared
``x86_64_atomics_ir_gen.py`` module (clang oracle vs self backend, 18
checked steps).
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest


def _load_ir_gen():
    path = Path(__file__).with_name("x86_64_atomics_ir_gen.py")
    spec = importlib.util.spec_from_file_location("x86_64_atomics_ir_gen", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("x86_64_atomics_ir_gen", module)
    spec.loader.exec_module(module)
    return module


def _emit_asm() -> str:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    return emit_self_asm(_load_ir_gen().build_module())


def test_atomics_lower_to_x86_64_tso_shapes():
    asm = _emit_asm()
    for needle in (
        "  lock xadd QWORD PTR [r11], r10",
        "  lock xadd DWORD PTR [r11], r10d",
        "  lock cmpxchg QWORD PTR [r11], r10",
        "  lock cmpxchg DWORD PTR [r11], r10d",
        "  xchg DWORD PTR [r11], r10d",
        "  xchg QWORD PTR [r11], r10",
        "  xchg BYTE PTR [r11], r10b",
        "  mov BYTE PTR [r11], r10b",
        "  neg r10d",
        "  sete r10b",
        "  mfence",
    ):
        assert needle in asm, f"missing {needle!r} in x86_64 asm"
    # the and/or retry loop re-enters through a local label
    assert ".Lat_main_" in asm
    assert "  jne .Lat_main_" in asm
    # only the seq_cst fence emits mfence; the acquire fence is free on
    # x86-TSO, so exactly one mfence must appear for the two fences.
    assert asm.count("mfence") == 1


def test_atomicrmw_op_outside_the_set_fails_closed():
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.ir import ir

    gen = _load_ir_gen()
    mod = ir.Module(name="atomics_bad_op")
    mod.triple = gen.X86_64_LINUX_TRIPLE
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    cell = ir.GlobalVariable(mod, i64, name="cell")
    cell.initializer = ir.Constant(i64, 0)
    fn = ir.Function(mod, ir.FunctionType(i32, []), name="main")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    builder.atomic_rmw("nand", cell, ir.Constant(i64, 1), "seq_cst")
    builder.ret(ir.Constant(i32, 0))
    # the shared parser already rejects ops outside add/sub/and/or/xchg
    with pytest.raises(BackendUnavailable, match="atomicrmw"):
        emit_self_asm(str(mod))


@pytest.mark.parametrize("kind", ("load", "store", "rmw", "cmpxchg"))
def test_atomic_width_outside_machine_byte_widths_fails_closed(kind):
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.ir import ir

    gen = _load_ir_gen()
    mod = ir.Module(name="atomics_bad_width")
    mod.triple = gen.X86_64_LINUX_TRIPLE
    i1 = ir.IntType(1)
    i32 = ir.IntType(32)
    cell = ir.GlobalVariable(mod, i1, name="cell")
    cell.initializer = ir.Constant(i1, 0)
    fn = ir.Function(mod, ir.FunctionType(i32, []), name="main")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    if kind == "load":
        builder.load_atomic(cell, "acquire", 1, typ=i1)
    elif kind == "store":
        builder.store_atomic(ir.Constant(i1, 1), cell, "release", 1)
    elif kind == "rmw":
        builder.atomic_rmw("xchg", cell, ir.Constant(i1, 1), "seq_cst")
    else:
        builder.cmpxchg(
            cell, ir.Constant(i1, 0), ir.Constant(i1, 1),
            "acq_rel", "acquire",
        )
    builder.ret(ir.Constant(i32, 0))
    with pytest.raises(BackendUnavailable, match="only supports i8/i16/i32/i64"):
        emit_self_asm(str(mod))


@pytest.mark.parametrize(
    "width, memory, value_reg, accumulator",
    ((8, "BYTE PTR", "r10b", "al"), (16, "WORD PTR", "r10w", "ax")),
    ids=("i8", "i16"),
)
def test_narrow_atomic_family_uses_width_correct_tso_instructions(
    width, memory, value_reg, accumulator,
):
    from pcc.backend.self_backend_dispatch import emit_self_asm

    alignment = width // 8
    ir_text = textwrap.dedent(f'''
        target triple = "x86_64-unknown-linux-gnu"
        define i32 @main() {{
        entry:
          %cell = alloca i{width}, align {alignment}
          store atomic i{width} -5, ptr %cell release, align {alignment}
          %loaded = load atomic i{width}, ptr %cell acquire, align {alignment}
          store atomic i{width} %loaded, ptr %cell seq_cst, align {alignment}
          %add = atomicrmw add ptr %cell, i{width} 7 acq_rel
          %sub = atomicrmw sub ptr %cell, i{width} 3 release
          %and = atomicrmw and ptr %cell, i{width} 54 seq_cst
          %or = atomicrmw or ptr %cell, i{width} 65 monotonic
          %exchange = atomicrmw xchg ptr %cell, i{width} -2 acquire
          %pair = cmpxchg ptr %cell, i{width} -2, i{width} -7 acq_rel acquire
          %old = extractvalue {{ i{width}, i1 }} %pair, 0
          %success = extractvalue {{ i{width}, i1 }} %pair, 1
          %result = zext i{width} %old to i32
          ret i32 %result
        }}
    ''')
    asm = emit_self_asm(ir_text)
    for instruction in (
        f"mov {value_reg}, {memory} [r11]",
        f"mov {memory} [r11], {value_reg}",
        f"xchg {memory} [r11], {value_reg}",
        f"neg {value_reg}",
        f"lock xadd {memory} [r11], {value_reg}",
        f"mov {accumulator}, {memory} [r11]",
        f"and {value_reg}, {accumulator}",
        f"or {value_reg}, {accumulator}",
        f"lock cmpxchg {memory} [r11], {value_reg}",
        "sete r10b",
    ):
        assert "  " + instruction in asm, instruction
    assert asm.count("lock xadd") == 2
    assert asm.count("lock cmpxchg") == 3
    assert asm.count("jne .Lat_main_") == 2
    assert "mfence" not in asm
