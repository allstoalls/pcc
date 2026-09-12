"""A module whose last line has no newline must survive a round trip.

`declare i64 @strlen(ptr)` at end of file -- exactly how a lazily emitted libc
declaration lands -- was stored without its newline and re-emitted *before* the
functions, producing

    declare i64 @strlen(ptr)define i32 @fib(...)

so `fib` no longer existed for the asm emitter, the assembler or the linker.  A
C program calling `strlen` then linked against an undefined `_fib` and died in
dyld at startup, with every stage in between reporting success.
"""

from __future__ import annotations

from pcc.native_ir.ir_mutator import MutableModule
from pcc.native_ir.mem2reg import mem2reg_text


_MODULE_WITH_UNTERMINATED_TAIL = (
    'target triple = "arm64-apple-darwin"\n'
    "\n"
    "define i32 @fib(i32 %n) {\n"
    "bb0:\n"
    "  %slot = alloca i32\n"
    "  store i32 %n, ptr %slot, align 4\n"
    "  %v = load i32, ptr %slot, align 4\n"
    "  ret i32 %v\n"
    "}\n"
    "\n"
    "define i32 @main() {\n"
    "bb0:\n"
    "  %r = call i32 @fib(i32 10)\n"
    "  ret i32 %r\n"
    "}\n"
    "\n"
    "declare i64 @strlen(ptr)"  # deliberately no trailing newline
)


def _defined_functions(ir_text: str) -> list[str]:
    return [
        line.split("@")[1].split("(")[0]
        for line in ir_text.splitlines()
        if line.startswith("define ")
    ]


def test_round_trip_keeps_every_definition_on_its_own_line():
    module = MutableModule.parse(_MODULE_WITH_UNTERMINATED_TAIL)
    assert [fn.name for fn in module.functions] == ["fib", "main"]

    serialized = module.serialize()
    assert _defined_functions(serialized) == ["fib", "main"]
    assert "@strlen(ptr)define" not in serialized


def test_mem2reg_does_not_drop_a_function_after_an_unterminated_declare():
    promoted, changed = mem2reg_text(_MODULE_WITH_UNTERMINATED_TAIL)
    assert changed, "the alloca should have been promoted"
    assert _defined_functions(promoted) == ["fib", "main"]


def test_serialized_declaration_keeps_its_own_line():
    module = MutableModule.parse(_MODULE_WITH_UNTERMINATED_TAIL)
    serialized = module.serialize()
    declare_lines = [
        line for line in serialized.splitlines() if line.startswith("declare ")
    ]
    assert declare_lines == ["declare i64 @strlen(ptr)"]
