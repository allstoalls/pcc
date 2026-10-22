"""Bounded integer-result self recursion with explicit SSA parameter transfer.

This helper is not wired into the production pipeline. It uses the same
frame/root/exception legality checks as the void helper, and emits complete
parameter PHIs rather than depending on a nonexistent marker-consuming pass.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tailcall_ir import _rewrite_self_tailcalls


@dataclass(frozen=True)
class TailcallRewrite:
    function: str
    rewritten: bool
    reason: str


def rewrite_accumulator_tailcalls(ir_text: str) -> tuple[str, list[TailcallRewrite]]:
    result = _rewrite_self_tailcalls(
        ir_text, value_returns=True, marker="pcc.tailcall.accumulator",
    )
    return result.ir_text, [
        TailcallRewrite(candidate.function, candidate.rewritten, candidate.reason)
        for candidate in result.candidates
    ]
