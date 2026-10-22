"""SysV x86-64 sets %al only for variadic calls.

%al carries the number of vector registers a variadic call uses. The
emitter wrote ``xor eax, eax`` before every call: 37,295 of 257,687
instructions in the x86-64 object of ``worker_resource_plan``. A
non-variadic call ignores %rax, and the call site's own type says whether
it is variadic (calling a variadic function through a non-variadic type is
undefined), so only variadic call sites keep the count.
"""
from __future__ import annotations

from pcc.backend.self_backend_x86_64_linux import emit_x86_64_linux_asm

_IR = """target triple = "x86_64-unknown-linux-gnu"
@fmt = internal constant [4 x i8] c"%d\\0A\\00"
declare i64 @fixed(i64, i64)
declare i64 @vary(ptr, ...)
define i64 @probe(i64 %a) {
entry:
  %x = call i64 @fixed(i64 %a, i64 2)
  %y = call i64 (ptr, ...) @vary(ptr @fmt, i64 %x)
  %z = call i64 (ptr, ...) @vary(ptr @fmt, double 1.5, i64 %y)
  %w = call i64 (ptr, ...) @vary(ptr @fmt)
  ret i64 %w
}
"""


def _lines_before_calls(asm: str) -> dict[str, list[str]]:
    lines = [line.strip() for line in asm.splitlines()]
    calls = {}
    for index, line in enumerate(lines):
        if line.startswith("call "):
            calls.setdefault(line.split()[1], []).append(lines[index - 1])
    return calls


def test_only_variadic_calls_set_al():
    calls = _lines_before_calls(emit_x86_64_linux_asm(_IR))
    assert len(calls["fixed"]) == 1 and len(calls["vary"]) == 3
    assert calls["fixed"][0] not in ("xor eax, eax",) and not calls["fixed"][0].startswith("mov al,")
    assert calls["vary"] == ["xor eax, eax", "mov al, 1", "xor eax, eax"]


def test_non_variadic_calls_never_write_al():
    asm = emit_x86_64_linux_asm(_IR)
    body = asm[asm.index("probe:"):]
    assert body.count("xor eax, eax") == 2
    assert body.count("mov al, ") == 1
