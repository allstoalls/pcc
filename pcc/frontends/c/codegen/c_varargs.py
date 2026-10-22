"""Structured varargs textual lowering for the C frontend.

llvmlite cannot directly emit LLVM ``va_arg`` in the subset pcc uses, so the C
frontend historically rewrote helper calls with a regex.  This module keeps the
rewrite but gives it a structured report surface.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Iterable, Optional

_PCC_VAARG_MARKER = "__pcc_va_arg_"


@dataclass(frozen=True)
class VarargsRewrite:
    helper: str
    lhs: str
    arg_type: str
    arg_value: str
    result_type: str

    def to_json(self) -> dict[str, str]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class VarargsRewriteReport:
    rewrites: tuple[VarargsRewrite, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema": "pcc.c.varargs_rewrite.v1",
            "count": len(self.rewrites),
            "rewrites": [r.to_json() for r in self.rewrites],
        }

    def format_json(self) -> str:
        # No ``indent=``: this module is inside the stage1 self-host closure
        # (generation_lowering imports postprocess_varargs_ir), and the native
        # json.dumps lowering supports only a literal ``sort_keys`` kwarg —
        # any other kwarg drags the whole closure onto the CPython bridge
        # (fallback baseline pins the closure at 0 py_cpy calls).
        return json.dumps(self.to_json(), sort_keys=True)


def _vaarg_helper_in_line(line: str) -> str:
    start = line.find(_PCC_VAARG_MARKER)
    if start < 0:
        return ""
    end = start + len(_PCC_VAARG_MARKER)
    digit_start = end
    while end < len(line) and "0" <= line[end] <= "9":
        end = end + 1
    if end == digit_start:
        return ""
    return line[start:end]


def _rewrite_vaarg_call_line(line: str):
    """Return ``(rewritten_line, report_entry, is_declaration)``.

    This deliberately parses only the helper grammar emitted by pcc.  Other
    LLVM lines are returned byte-for-byte rather than being interpreted as a
    broader LLVM grammar.
    """
    helper = _vaarg_helper_in_line(line)
    if not helper:
        return line, None, False
    stripped = line.strip()
    if stripped.startswith("declare "):
        return "", None, True

    equal = line.find("=")
    if equal < 0:
        return line, None, False
    call_start = line.find("call ", equal + 1)
    if call_start < 0:
        return line, None, False
    symbol_start = line.find("@", call_start + 5)
    helper_start = line.find(helper, symbol_start)
    if symbol_start < 0 or helper_start < 0:
        return line, None, False
    open_paren = line.find("(", helper_start + len(helper))
    close_paren = line.rfind(")")
    if open_paren < 0 or close_paren <= open_paren:
        return line, None, False

    call_prefix = line[call_start + 5 : symbol_start].strip()
    result_type_end = call_prefix.find(" ")
    if result_type_end < 0:
        result_type = call_prefix
    else:
        result_type = call_prefix[:result_type_end]
    argument = line[open_paren + 1 : close_paren].strip()
    value_start = argument.find("%")
    if not result_type or value_start <= 0:
        return line, None, False
    arg_type = argument[:value_start].strip()
    arg_value = argument[value_start:].strip()
    lhs = line[:equal].strip()
    if not lhs or not arg_type or not arg_value:
        return line, None, False

    entry = VarargsRewrite(
        helper=helper,
        lhs=lhs,
        arg_type=arg_type,
        arg_value=arg_value,
        result_type=result_type,
    )
    rewritten = (
        f"{entry.lhs} = va_arg {entry.arg_type} "
        f"{entry.arg_value}, {entry.result_type}"
    )
    return rewritten, entry, False


def postprocess_varargs_ir(
    text: str, *, report: Optional[list[VarargsRewrite]] = None
) -> str:
    if _PCC_VAARG_MARKER not in text:
        return text
    output: list[str] = []
    for line in text.split("\n"):
        rewritten, entry, is_declaration = _rewrite_vaarg_call_line(line)
        if is_declaration:
            continue
        output.append(rewritten)
        if entry is not None and report is not None:
            report.append(entry)
    return "\n".join(output)


def build_report(rewrites: Iterable[VarargsRewrite]) -> VarargsRewriteReport:
    return VarargsRewriteReport(tuple(rewrites))
