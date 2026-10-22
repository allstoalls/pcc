
"""Small structural IR diff helper for pcc2/pcc3 and self-backend debugging."""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Iterable


_DEFINE_RE = re.compile(r"^\s*define\b.*@([A-Za-z0-9_.$-]+)\s*\(")
_CALL_RE = re.compile(r"\bcall\b.*@([A-Za-z0-9_.$-]+)\s*\(")
_GLOBAL_RE = re.compile(r"^\s*@([A-Za-z0-9_.$-]+)\s*=")


@dataclass(frozen=True)
class FunctionSummary:
    name: str
    instruction_count: int = 0
    calls: tuple[str, ...] = ()


@dataclass
class IrSummary:
    functions: dict[str, FunctionSummary] = field(default_factory=dict)
    globals: set[str] = field(default_factory=set)

    @staticmethod
    def parse(ir_text: str) -> "IrSummary":
        funcs: dict[str, FunctionSummary] = {}
        globals_: set[str] = set()
        cur_name: str | None = None
        cur_count = 0
        cur_calls: list[str] = []
        for raw in ir_text.splitlines():
            line = raw.strip()
            m_global = _GLOBAL_RE.match(line)
            if m_global:
                globals_.add(m_global.group(1))
            m_def = _DEFINE_RE.match(line)
            if m_def:
                if cur_name is not None:
                    funcs[cur_name] = FunctionSummary(cur_name, cur_count, tuple(cur_calls))
                cur_name = m_def.group(1)
                cur_count = 0
                cur_calls = []
                continue
            if cur_name is not None:
                if line == "}":
                    funcs[cur_name] = FunctionSummary(cur_name, cur_count, tuple(cur_calls))
                    cur_name = None
                    cur_count = 0
                    cur_calls = []
                    continue
                if line and not line.endswith(":") and not line.startswith(";"):
                    cur_count += 1
                    m_call = _CALL_RE.search(line)
                    if m_call:
                        cur_calls.append(m_call.group(1))
        if cur_name is not None:
            funcs[cur_name] = FunctionSummary(cur_name, cur_count, tuple(cur_calls))
        return IrSummary(functions=funcs, globals=globals_)


@dataclass(frozen=True)
class IrDiff:
    missing_functions: tuple[str, ...]
    extra_functions: tuple[str, ...]
    changed_instruction_counts: tuple[tuple[str, int, int], ...]
    changed_calls: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]

    def is_empty(self) -> bool:
        return not (
            self.missing_functions
            or self.extra_functions
            or self.changed_instruction_counts
            or self.changed_calls
        )

    def to_text(self) -> str:
        lines: list[str] = []
        for name in self.missing_functions:
            lines.append(f"missing function: {name}")
        for name in self.extra_functions:
            lines.append(f"extra function: {name}")
        for name, lhs, rhs in self.changed_instruction_counts:
            lines.append(f"instruction count differs: {name} {lhs}->{rhs}")
        for name, lhs, rhs in self.changed_calls:
            lines.append(f"calls differ: {name} {list(lhs)}->{list(rhs)}")
        return "\n".join(lines)


def diff_ir(lhs_text: str, rhs_text: str) -> IrDiff:
    lhs = IrSummary.parse(lhs_text)
    rhs = IrSummary.parse(rhs_text)
    lhs_names = set(lhs.functions)
    rhs_names = set(rhs.functions)
    common = sorted(lhs_names & rhs_names)
    count_changes: list[tuple[str, int, int]] = []
    call_changes: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for name in common:
        lfn = lhs.functions[name]
        rfn = rhs.functions[name]
        if lfn.instruction_count != rfn.instruction_count:
            count_changes.append((name, lfn.instruction_count, rfn.instruction_count))
        if lfn.calls != rfn.calls:
            call_changes.append((name, lfn.calls, rfn.calls))
    return IrDiff(
        missing_functions=tuple(sorted(lhs_names - rhs_names)),
        extra_functions=tuple(sorted(rhs_names - lhs_names)),
        changed_instruction_counts=tuple(count_changes),
        changed_calls=tuple(call_changes),
    )
