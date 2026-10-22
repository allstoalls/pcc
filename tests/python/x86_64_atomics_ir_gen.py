"""Shared IR generator for the x86_64-linux self-backend atomic tests.

Builds one module whose ``main`` exercises every atomic form the self
backend translates (load/store at each ordering, atomicrmw
add/sub/and/or/xchg, cmpxchg hit/miss including the success flag, the
i8 byte-flag lane, fence) and returns 0 when every observed old/loaded
value matches the LLVM semantics, or the 1-based step number of the
first mismatch.

Used by ``tests/python/test_unsafe_atomics_x86_64.py`` (host asm-shape
assertions) and by the docker differential in
``tests/integration/test_self_backend_x86_64_linux.py`` (clang oracle
vs self backend on real Linux). Not a pytest file itself.
"""
from __future__ import annotations

X86_64_LINUX_TRIPLE = "x86_64-unknown-linux-gnu"


def build_module() -> str:
    from pcc.ir import ir

    mod = ir.Module(name="atomics_x86_64_smoke")
    mod.triple = X86_64_LINUX_TRIPLE
    i1 = ir.IntType(1)
    i8 = ir.IntType(8)
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)

    g64 = ir.GlobalVariable(mod, i64, name="pcc_atomic_cell64")
    g64.initializer = ir.Constant(i64, 0)
    g32 = ir.GlobalVariable(mod, i32, name="pcc_atomic_cell32")
    g32.initializer = ir.Constant(i32, 0)
    g8 = ir.GlobalVariable(mod, i8, name="pcc_atomic_flag8")
    g8.initializer = ir.Constant(i8, 0)

    fn = ir.Function(mod, ir.FunctionType(i32, []), name="main")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    state = {"step": 0}

    def check(observed, expected_int, ty):
        state["step"] += 1
        step = state["step"]
        ok = fn.append_basic_block(f"ok{step}")
        fail = fn.append_basic_block(f"fail{step}")
        cond = builder.icmp_signed(
            "==", observed, ir.Constant(ty, expected_int), name=f"cmp{step}"
        )
        builder.cbranch(cond, ok, fail)
        builder.position_at_end(fail)
        builder.ret(ir.Constant(i32, step))
        builder.position_at_end(ok)

    # i64 lane
    builder.store_atomic(ir.Constant(i64, 41), g64, "release", 8)
    check(builder.load_atomic(g64, "acquire", 8, typ=i64), 41, i64)  # 1
    check(builder.atomic_rmw("add", g64, ir.Constant(i64, 1), "acq_rel"), 41, i64)  # 2
    check(builder.load_atomic(g64, "seq_cst", 8, typ=i64), 42, i64)  # 3
    pair = builder.cmpxchg(
        g64, ir.Constant(i64, 42), ir.Constant(i64, 100), "acq_rel", "acquire"
    )
    check(builder.extract_value(pair, 0), 42, i64)  # 4
    check(builder.extract_value(pair, 1), 1, i1)  # 5
    check(builder.load_atomic(g64, "monotonic", 8, typ=i64), 100, i64)  # 6
    pair = builder.cmpxchg(
        g64, ir.Constant(i64, 42), ir.Constant(i64, 7), "seq_cst", "monotonic"
    )
    check(builder.extract_value(pair, 0), 100, i64)  # 7
    check(builder.extract_value(pair, 1), 0, i1)  # 8
    check(builder.atomic_rmw("and", g64, ir.Constant(i64, 6), "seq_cst"), 100, i64)  # 9
    check(builder.atomic_rmw("or", g64, ir.Constant(i64, 9), "monotonic"), 4, i64)  # 10
    check(builder.load_atomic(g64, "acquire", 8, typ=i64), 13, i64)  # 11

    # i32 lane
    builder.store_atomic(ir.Constant(i32, -5), g32, "monotonic", 4)
    check(builder.atomic_rmw("sub", g32, ir.Constant(i32, 3), "seq_cst"), -5, i32)  # 12
    check(builder.load_atomic(g32, "acquire", 4, typ=i32), -8, i32)  # 13
    check(builder.atomic_rmw("xchg", g32, ir.Constant(i32, 9), "acq_rel"), -8, i32)  # 14
    pair = builder.cmpxchg(
        g32, ir.Constant(i32, 9), ir.Constant(i32, -1), "monotonic", "monotonic"
    )
    check(builder.extract_value(pair, 0), 9, i32)  # 15
    check(builder.extract_value(pair, 1), 1, i1)  # 16
    check(builder.load_atomic(g32, "monotonic", 4, typ=i32), -1, i32)  # 17

    # seq_cst store lowers to the implicitly locked xchg
    builder.store_atomic(ir.Constant(i64, 5), g64, "seq_cst", 8)
    check(builder.load_atomic(g64, "acquire", 8, typ=i64), 5, i64)  # 18

    # i8 byte-flag lane (the test_and_set/clear shapes)
    check(builder.atomic_rmw("xchg", g8, ir.Constant(i8, 1), "acquire"), 0, i8)  # 19
    check(builder.atomic_rmw("xchg", g8, ir.Constant(i8, 1), "seq_cst"), 1, i8)  # 20
    builder.store_atomic(ir.Constant(i8, 0), g8, "release", 1)
    check(builder.load(g8), 0, i8)  # 21

    builder.fence("seq_cst")
    builder.fence("acquire")
    builder.ret(ir.Constant(i32, 0))
    return str(mod)


def main() -> None:
    import argparse

    from pcc.backend.self_backend_dispatch import emit_self_asm

    parser = argparse.ArgumentParser()
    parser.add_argument("--out-ll", required=True)
    parser.add_argument("--out-s", required=True)
    args = parser.parse_args()
    ir_text = build_module()
    with open(args.out_ll, "w", encoding="utf-8") as fh:
        fh.write(ir_text)
    with open(args.out_s, "w", encoding="utf-8") as fh:
        fh.write(emit_self_asm(ir_text))


if __name__ == "__main__":
    main()
