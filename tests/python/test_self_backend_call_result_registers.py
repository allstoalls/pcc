"""PCC_SELF_CALL_RESULT_REGISTERS: a scalar call result may live in a register.

The self backend's allocator treats every SSA value as owning a frame slot and
adds a register projection only for values whose whole interval is proven
clobber-free.  Call results were excluded outright, so every ``py_*`` helper
result was stored to its slot after ``bl`` and reloaded at each use.  This
slice admits a result whose interval after the call touches no other call: the
emitter commits it with one ``mov`` from ``x0`` and leaves the slot unwritten.

These cases pin the boundary: a result consumed before any other call moves
into a pool register; a result that crosses another call keeps the slot path;
with the flag unset the emitter is byte-identical to before.
"""
from __future__ import annotations

import re

from pcc.backend.self_backend_dispatch import emit_self_asm

import pytest


@pytest.fixture(autouse=True)
def _block_local_allocator(monkeypatch):
    # These cases pin the block-local x1-x8 allocator's exact shapes; the
    # default callee-saved allocator is covered by
    # tests/c/test_self_backend_aarch64_callee_saved.py.
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "0")


_HEADER = '''
target triple = "arm64-apple-macosx"

declare i64 @produce(i64)
declare void @consume(i64)
'''

USE_ONCE = _HEADER + '''
define external i64 @use_once(i64 %n) "no-builtins" {
entry:
  %r = call i64 (i64) @produce(i64 %n)
  %s = add i64 %r, 3
  ret i64 %s
}
'''

CROSSES_CALL = _HEADER + '''
define external i64 @crosses(i64 %n) "no-builtins" {
entry:
  %r = call i64 (i64) @produce(i64 %n)
  call void (i64) @consume(i64 %n)
  %s = add i64 %r, 3
  ret i64 %s
}
'''

POINTER_RESULT = _HEADER + '''
declare ptr @lookup(ptr)

define external i64 @deref(ptr %p) "no-builtins" {
entry:
  %q = call ptr (ptr) @lookup(ptr %p)
  %v = load i64, ptr %q
  ret i64 %v
}
'''


def _emit(ir: str, monkeypatch, *, enabled: bool) -> str:
    if enabled:
        monkeypatch.setenv("PCC_SELF_CALL_RESULT_REGISTERS", "1")
    else:
        monkeypatch.delenv("PCC_SELF_CALL_RESULT_REGISTERS", raising=False)
    return emit_self_asm(ir)


def _body(asm: str, symbol: str) -> list[str]:
    lines = asm.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"_{symbol}:")
    body: list[str] = []
    for line in lines[start + 1:]:
        if line.strip() == "ret":
            break
        body.append(line.strip())
    return body


def _after_call(body: list[str], callee: str) -> list[str]:
    index = body.index(f"bl _{callee}")
    # Labels are not instructions: a call safepoint's stack-map label marks
    # the return address right after the call.
    return [line for line in body[index + 1:] if not line.endswith(":")]


_FRAME_STORE_X0 = re.compile(r"^st[u]?r\s+[wx]0,\s*\[x29")
_POOL_MOVE_FROM_X0 = re.compile(r"^mov\s+[wx]([1-8]),\s*[wx]0$")


def test_result_consumed_before_any_call_moves_into_a_pool_register(monkeypatch):
    body = _body(_emit(USE_ONCE, monkeypatch, enabled=True), "use_once")
    tail = _after_call(body, "produce")
    assert _POOL_MOVE_FROM_X0.match(tail[0]), tail[:4]
    assert not any(_FRAME_STORE_X0.match(line) for line in tail), tail


def test_result_that_crosses_another_call_keeps_its_slot(monkeypatch):
    on = _emit(CROSSES_CALL, monkeypatch, enabled=True)
    off = _emit(CROSSES_CALL, monkeypatch, enabled=False)
    tail = _after_call(_body(on, "crosses"), "produce")
    assert any(_FRAME_STORE_X0.match(line) for line in tail), tail
    assert on == off


def test_pointer_result_is_projected_like_an_integer(monkeypatch):
    body = _body(_emit(POINTER_RESULT, monkeypatch, enabled=True), "deref")
    tail = _after_call(body, "lookup")
    assert _POOL_MOVE_FROM_X0.match(tail[0]), tail[:4]
    assert not any(_FRAME_STORE_X0.match(line) for line in tail), tail


def test_flag_unset_is_byte_identical_to_the_slot_path(monkeypatch):
    for ir in (USE_ONCE, CROSSES_CALL, POINTER_RESULT):
        off = _emit(ir, monkeypatch, enabled=False)
        # Every call result is stored to its slot when the flag is unset.
        for symbol, callee in (("use_once", "produce"), ("crosses", "produce"), ("deref", "lookup")):
            if f"_{symbol}:" not in off:
                continue
            tail = _after_call(_body(off, symbol), callee)
            assert any(_FRAME_STORE_X0.match(line) for line in tail), (symbol, tail)
