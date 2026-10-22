"""Callee-saved register allocation (default; ``PCC_SELF_CALLEE_SAVED_REGISTERS=0`` is off).

Values that live across calls, call operands and scalar arguments may be held
in x19-x28.  These cases execute the emitted code against a C driver that
checks every result and whose own values sit in callee-saved registers across
each call, while the C callee trashes every caller-saved register and forces
x19-x28 saves of its own.  A missing save/restore, a value left in a
caller-saved register across a call, a wrong interval (the loop back edge, a
recursion) or a lost narrow-int mask shows up as a wrong result.  Every case
runs with the mode on and off; both must print the same, correct output.
"""

import subprocess

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm


IR = r'''
target triple = "arm64-apple-darwin25.5.0"

declare i64 @clobber(i64)

define i64 @pressure(i64 %a, i64 %b) {
entry:
  %v0 = add i64 %a, 1
  %v1 = add i64 %b, 2
  %v2 = mul i64 %a, %b
  %v3 = sub i64 %a, %b
  %v4 = xor i64 %a, 85
  %v5 = add i64 %b, 1000
  %v6 = mul i64 %v0, 3
  %v7 = add i64 %v1, %v2
  %v8 = sub i64 %v3, 17
  %v9 = add i64 %v4, %v5
  %v10 = mul i64 %v6, %v7
  %v11 = add i64 %v8, %v9
  %r0 = call i64 @clobber(i64 %v0)
  %s0 = add i64 %v0, %v1
  %s1 = add i64 %s0, %v2
  %s2 = add i64 %s1, %v3
  %s3 = add i64 %s2, %v4
  %s4 = add i64 %s3, %v5
  %s5 = add i64 %s4, %v6
  %s6 = add i64 %s5, %v7
  %s7 = add i64 %s6, %v8
  %s8 = add i64 %s7, %v9
  %s9 = add i64 %s8, %v10
  %s10 = add i64 %s9, %v11
  %r1 = call i64 @clobber(i64 %s10)
  %t0 = add i64 %s10, %r0
  %t1 = add i64 %t0, %r1
  %t2 = add i64 %t1, %a
  %t3 = add i64 %t2, %b
  ret i64 %t3
}

define i64 @loop_calls(i64 %n) {
entry:
  %base = add i64 %n, 7
  br label %head

head:
  %i = phi i64 [0, %entry], [%next, %body]
  %acc = phi i64 [0, %entry], [%acc2, %body]
  %cmp = icmp slt i64 %i, %n
  br i1 %cmp, label %body, label %exit

body:
  %t = call i64 @clobber(i64 %i)
  %u = add i64 %t, %base
  %acc2 = add i64 %acc, %u
  %next = add i64 %i, 1
  br label %head

exit:
  ret i64 %acc
}

define i64 @fib(i64 %n) {
entry:
  %small = icmp slt i64 %n, 2
  br i1 %small, label %leaf, label %rec

leaf:
  ret i64 %n

rec:
  %n1 = sub i64 %n, 1
  %f1 = call i64 @fib(i64 %n1)
  %n2 = sub i64 %n, 2
  %f2 = call i64 @fib(i64 %n2)
  %s = add i64 %f1, %f2
  ret i64 %s
}

define i32 @narrow(i8 %c, i16 %h, i1 %flag, i32 %w) {
entry:
  %cx = zext i8 %c to i32
  %t = call i64 @clobber(i64 1)
  %hx = sext i16 %h to i32
  %sum = add i32 %cx, %hx
  %fx = zext i1 %flag to i32
  %r = add i32 %sum, %fx
  %t2 = call i64 @clobber(i64 2)
  %tw = trunc i64 %t2 to i32
  %r2 = add i32 %r, %w
  %r3 = add i32 %r2, %tw
  ret i32 %r3
}

define i64 @tagged(ptr %p) {
entry:
  %bits = ptrtoint ptr %p to i64
  %tag = and i64 %bits, 1
  %isnull = icmp eq ptr %p, null
  br i1 %isnull, label %null, label %nonnull

null:
  ret i64 -1

nonnull:
  %t = call i64 @clobber(i64 %bits)
  %half = ashr i64 %bits, 1
  %shl = shl i64 %half, 3
  %lsr = lshr i64 %bits, 4
  %x = add i64 %shl, %lsr
  %y = add i64 %x, %tag
  %back = inttoptr i64 %bits to ptr
  %same = icmp eq ptr %back, %p
  %samex = zext i1 %same to i64
  %z = add i64 %y, %t
  %zz = add i64 %z, %samex
  %small = icmp ult i64 %zz, 4095
  %smallx = zext i1 %small to i64
  %out = add i64 %zz, %smallx
  ret i64 %out
}
'''


DRIVER = r'''
#include <stdint.h>
#include <stdio.h>

int64_t pressure(int64_t a, int64_t b);
int64_t loop_calls(int64_t n);
int64_t fib(int64_t n);
int32_t narrow(uint8_t c, int16_t h, _Bool flag, int32_t w);
int64_t tagged(void *p);

__attribute__((noinline)) int64_t clobber(int64_t x) {
    __asm__ volatile(
        "mov x1, #1\n mov x2, #2\n mov x3, #3\n mov x4, #4\n"
        "mov x5, #5\n mov x6, #6\n mov x7, #7\n mov x8, #8\n"
        "mov x9, #9\n mov x10, #9\n mov x11, #9\n mov x12, #9\n"
        "mov x13, #9\n mov x14, #9\n mov x15, #9\n mov x16, #9\n mov x17, #9\n"
        "mov x19, #7\n mov x20, #7\n mov x21, #7\n mov x22, #7\n"
        "mov x23, #7\n mov x24, #7\n mov x25, #7\n mov x26, #7\n"
        "mov x27, #7\n mov x28, #7\n"
        ::: "x1", "x2", "x3", "x4", "x5", "x6", "x7", "x8", "x9", "x10",
            "x11", "x12", "x13", "x14", "x15", "x16", "x17", "x19", "x20",
            "x21", "x22", "x23", "x24", "x25", "x26", "x27", "x28");
    return x * 3 + 1;
}

static int64_t ref_clobber(int64_t x) { return x * 3 + 1; }

static int64_t ref_pressure(int64_t a, int64_t b) {
    int64_t v0 = a + 1, v1 = b + 2, v2 = a * b, v3 = a - b, v4 = a ^ 85;
    int64_t v5 = b + 1000, v6 = v0 * 3, v7 = v1 + v2, v8 = v3 - 17;
    int64_t v9 = v4 + v5, v10 = v6 * v7, v11 = v8 + v9;
    int64_t r0 = ref_clobber(v0);
    int64_t s10 = v0 + v1 + v2 + v3 + v4 + v5 + v6 + v7 + v8 + v9 + v10 + v11;
    int64_t r1 = ref_clobber(s10);
    return s10 + r0 + r1 + a + b;
}

static int64_t ref_loop(int64_t n) {
    int64_t acc = 0, base = n + 7;
    for (int64_t i = 0; i < n; i++) acc += ref_clobber(i) + base;
    return acc;
}

static int64_t ref_fib(int64_t n) { return n < 2 ? n : ref_fib(n - 1) + ref_fib(n - 2); }

static int32_t ref_narrow(uint8_t c, int16_t h, _Bool flag, int32_t w) {
    int32_t r = (int32_t)c + (int32_t)h + (flag ? 1 : 0);
    return r + w + (int32_t)ref_clobber(2);
}

static int64_t ref_tagged(void *p) {
    if (!p) return -1;
    int64_t bits = (int64_t)(intptr_t)p;
    int64_t tag = bits & 1;
    int64_t x = ((bits >> 1) << 3) + (int64_t)((uint64_t)bits >> 4);
    int64_t zz = x + tag + ref_clobber(bits) + 1;
    return zz + ((uint64_t)zz < 4095 ? 1 : 0);
}

int main(void) {
    int failures = 0;
    /* Held across every call below; -O2 keeps these in x19-x28. */
    volatile int64_t seed = 12345;
    int64_t k1 = seed * 7, k2 = seed + 99, k3 = seed ^ 0x5a5a, k4 = seed * seed;
    int64_t k5 = k1 + k2, k6 = k3 - k4;
    int64_t pairs[][2] = {{3, 4}, {-5, 9}, {1000000, -77}, {0, 0}};
    for (int i = 0; i < 4; i++) {
        int64_t got = pressure(pairs[i][0], pairs[i][1]);
        int64_t want = ref_pressure(pairs[i][0], pairs[i][1]);
        if (got != want) { printf("pressure %d %lld %lld\n", i, got, want); failures++; }
    }
    for (int64_t n = 0; n < 40; n += 13) {
        if (loop_calls(n) != ref_loop(n)) { printf("loop %lld\n", n); failures++; }
    }
    if (fib(24) != ref_fib(24)) { printf("fib\n"); failures++; }
    uint8_t cs[] = {0, 1, 200, 255};
    int16_t hs[] = {0, -1, 300, -32768};
    for (int i = 0; i < 4; i++) {
        for (int f = 0; f < 2; f++) {
            if (narrow(cs[i], hs[i], f, -i * 1000) != ref_narrow(cs[i], hs[i], f, -i * 1000)) {
                printf("narrow %d %d\n", i, f); failures++;
            }
        }
    }
    static int64_t cell[4];
    void *ptrs[] = {0, &cell[0], (char *)&cell[1] + 1, (void *)(intptr_t)4096};
    for (int i = 0; i < 4; i++) {
        if (tagged(ptrs[i]) != ref_tagged(ptrs[i])) { printf("tagged %d\n", i); failures++; }
    }
    if (k1 != seed * 7 || k2 != seed + 99 || k3 != (seed ^ 0x5a5a) || k4 != seed * seed
        || k5 != k1 + k2 || k6 != k3 - k4) {
        printf("caller callee-saved values lost\n");
        failures++;
    }
    printf(failures ? "FAIL %d\n" : "OK\n", failures);
    return failures ? 1 : 0;
}
'''


def _build_and_run(tmp_path, asm_text):
    asm_path = tmp_path / "callee_saved.s"
    asm_path.write_text(asm_text, encoding="utf-8")
    driver_path = tmp_path / "driver.c"
    driver_path.write_text(DRIVER, encoding="utf-8")
    exe_path = tmp_path / "callee_saved"
    subprocess.run(
        ["cc", "-O2", str(driver_path), str(asm_path), "-o", str(exe_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return subprocess.run([str(exe_path)], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("mode", ["1", "0"])
def test_callee_saved_allocation_executes_correctly(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", mode)
    run = _build_and_run(tmp_path, emit_self_asm(IR))
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "OK\n"


def _function_body(asm_text, symbol):
    lines = asm_text.splitlines()
    start = lines.index(f"_{symbol}:")
    body = []
    for line in lines[start + 1:]:
        if line.startswith("_") and line.endswith(":"):
            break
        body.append(line.strip())
    return body


def test_callee_saved_registers_are_saved_and_restored_on_every_exit(monkeypatch):
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "1")
    body = _function_body(emit_self_asm(IR), "fib")
    text = "\n".join(body)
    # %n (an argument live across both calls) and %f1 live in x19-x28.
    assert "mov x19, x0" in text
    saves = [line for line in body if line.startswith(("str x19", "stp x19"))]
    restores = [line for line in body if line.startswith(("ldr x19", "ldp x19"))]
    returns = [line for line in body if line == "ret"]
    assert len(saves) == 1
    # Both returns (the leaf and the recursive sum) restore before returning.
    assert len(returns) == 2
    assert len(restores) == len(returns)


def test_mode_off_emits_no_callee_saved_registers(monkeypatch):
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "0")
    asm_text = emit_self_asm(IR)
    for register in range(19, 29):
        assert f"x{register}" not in asm_text


NAME_REFERENCED_IR = r'''
target triple = "arm64-apple-darwin25.5.0"

declare i64 @sink(i64, i64)

define i64 @forward(ptr %p) {
entry:
  %v = load i64, ptr %p
  %a = call i64 @sink(i64 1, i64 2)
  %b = call i64 @sink(i64 %v, i64 %a)
  ret i64 %b
}
'''


def test_name_referenced_operand_reads_its_allocated_register(monkeypatch):
    # The direct kernel records some call operands by name rather than by
    # value id (constructor-call arguments, for one).  Such an operand is the
    # same SSA value: once the allocator gave it a register, its slot is never
    # written, so the by-name path must read the register too.  Before the
    # fix it loaded the stale slot, and pcc1 crashed at startup iterating a
    # function object passed as a dataclass default factory's argument.
    from pcc.backend.self_backend_aarch64_darwin import (
        _aggregate_returned_indirect,
        _aggregate_returned_indirect_indexed,
    )
    from pcc.backend.self_backend_aarch64_darwin_materialize import (
        materialize_scalar_value_indexed,
    )
    from pcc.backend.self_backend_aarch64_darwin_regalloc import (
        allocate_aarch64_block_registers,
    )
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_prepare import prepare_module_for_target

    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "1")
    prepared = prepare_module_for_target(
        NAME_REFERENCED_IR,
        aggregate_returned_indirect=_aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=_aggregate_returned_indirect_indexed,
        materialize_legacy_slots=False,
    )
    func = [f for f in prepared.functions if f.name == "forward"][0]
    allocate_aarch64_block_registers(func)
    kernel = get_indexed_function_kernel(func)
    value_id = kernel.value_id("v")
    register = kernel.value_register(value_id)
    # %v lives across the first call and is an operand of the second.
    assert register is not None and register >= 19
    by_id = materialize_scalar_value_indexed(
        func, kernel, "v", kernel.value_type_id(value_id), 1,
        prepared.module_symbols, value_id=value_id,
    )
    by_name = materialize_scalar_value_indexed(
        func, kernel, "v", kernel.value_type_id(value_id), 1,
        prepared.module_symbols,
    )
    assert by_name == by_id == [f"  mov x1, x{register}"]


@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_scan_reuse_preserves_callee_saved_assembly_and_frames(monkeypatch, mode):
    from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
    from pcc.backend import self_backend_aarch64_darwin_regalloc as regalloc
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_asm
    from tests.aarch64_regalloc_scan_reference import (
        allocation_state, counted_function_facts, old_scan_allocator, select_mode,
    )

    from tests.scalar_arena_projection_ci_reference import disable_scalar_projections

    select_mode(monkeypatch, mode)
    outputs = []
    graph_counts = [dict(pops=0, edges=0, hits=0) for _ in range(2)]
    reference = old_scan_allocator(facts_counts=graph_counts[0])
    counted_facts = counted_function_facts(regalloc._function_level_facts, graph_counts[1])
    for index, allocator in enumerate((reference, regalloc.allocate_aarch64_block_registers)):
        states = {}

        def allocate(func):
            allocator(func)
            states[func.name] = allocation_state(func)

        with monkeypatch.context() as patch:
            patch.setattr(prologue, "allocate_aarch64_block_registers", allocate)
            if index == 0:
                disable_scalar_projections(patch, allocator)
            if index == 1:
                patch.setattr(regalloc, "_function_level_facts", counted_facts)
            assembly = emit_aarch64_darwin_asm(IR, optimize=False)
        assert set(states) == {"pressure", "loop_calls", "fib", "narrow", "tagged"}
        if mode == "default":
            assert states["fib"]["callee_saved"]
        else:
            assert not any(state["callee_saved"] for state in states.values())
        outputs.append((assembly, states))
    assert outputs[0] == outputs[1]
    if mode != "local":
        assert graph_counts[1]["hits"] > 0
        assert graph_counts[1]["pops"] < graph_counts[0]["pops"]
    else:
        assert graph_counts == [dict(pops=0, edges=0, hits=0)] * 2
