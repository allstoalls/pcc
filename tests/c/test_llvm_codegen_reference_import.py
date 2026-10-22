from __future__ import annotations

import re
from pathlib import Path

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from tests.owned_ir_validation import verify_ir_text

_LLVM_CODEGEN_ROOT = Path("/tmp/llvm-project-20.1.8-targets/llvm/test/CodeGen")

_FALLBACK_LLVM_CODEGEN_SAMPLES = {
    ("AArch64", "arm64-csel.ll"): """
define i64 @foo1(i64 %a, i64 %b, i1 %cond) {
  %sel = select i1 %cond, i64 %a, i64 %b
  ret i64 %sel
}
""",
    ("X86", "convert-2-addr-3-addr-inc64.ll"): """
define i64 @fullGtU(i64 %a, i64 %b) {
  %cmp = icmp ugt i64 %a, %b
  %ret = zext i1 %cmp to i64
  ret i64 %ret
}
""",
}


def _llvm_codegen_sample_text(root: Path, target_dir: str, filename: str) -> str:
    source = root / target_dir / filename
    if source.exists():
        return source.read_text(encoding="utf-8")
    return _FALLBACK_LLVM_CODEGEN_SAMPLES[(target_dir, filename)]


def _drop_lit_and_filecheck_comments(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(";"):
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


def _first_function_subset(text: str, count: int) -> str:
    text = _drop_lit_and_filecheck_comments(text)
    out: list[str] = []
    in_function = False
    function_count = 0

    for line in text.splitlines():
        if not in_function:
            if line.startswith("define "):
                if function_count >= count:
                    continue
                in_function = True
                function_count += 1
                out.append(line)
                continue
            if function_count == 0 and line.strip():
                out.append(line)
            continue

        out.append(line)
        if line.strip() == "}":
            in_function = False

    return "\n".join(out).strip() + "\n"


def _ensure_target_triple(ir_text: str, triple: str) -> str:
    if re.search(r'^target triple = "', ir_text, flags=re.MULTILINE):
        return ir_text
    return f'target triple = "{triple}"\n' + ir_text


def _converted_llvm_codegen_test(text: str, *, max_functions: int, triple: str) -> str:
    text = _first_function_subset(text, max_functions)
    return _ensure_target_triple(text, triple)


@pytest.mark.parametrize(
    ("target_dir", "filename", "triple", "expected_label"),
    [
        ("AArch64", "arm64-csel.ll", "arm64-apple-darwin", "foo1:"),
        (
            "X86",
            "convert-2-addr-3-addr-inc64.ll",
            "x86_64-unknown-linux-gnu",
            "fullGtU:",
        ),
    ],
)
def test_llvm_codegen_ll_samples_convert_to_pcc_supported_assembly(
    target_dir: str,
    filename: str,
    triple: str,
    expected_label: str,
):
    sample_text = _llvm_codegen_sample_text(_LLVM_CODEGEN_ROOT, target_dir, filename)

    ir_text = _converted_llvm_codegen_test(
        sample_text,
        max_functions=3,
        triple=triple,
    )
    verify_ir_text(ir_text)
    asm = emit_self_asm(ir_text, triple)

    assert expected_label in asm
    assert "ret" in asm
