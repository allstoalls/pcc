"""HighTier Pass 2: Allocation Decision.

Reads escape analysis results from PassContext and decides each variable's
allocation strategy:

  SSA:            non-escaping + single-def scalar → no alloca needed
  REGISTER_HINT:  non-escaping + multi-def scalar → alloca with promotion hint
  ALLOCA:         everything else (address taken, aggregate, etc.)

This is the Graal PEA idea simplified for static C:
  - Graal: per-branch escape decisions with runtime deopt fallback
  - PCC:   per-function escape decisions (conservative but correct)
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import AllocStrategy, PassContext


class AllocDecisionPass(ASTPass):
    name = "alloc-decision"

    # Types that can be promoted to SSA values
    _SCALAR_TYPES = frozenset({
        "char", "signed char", "unsigned char",
        "short", "short int", "signed short", "unsigned short",
        "signed short int", "unsigned short int",
        "int", "signed int", "unsigned int", "signed",
        "long", "long int", "signed long", "unsigned long",
        "signed long int", "unsigned long int",
        "long long", "long long int", "signed long long",
        "unsigned long long", "signed long long int",
        "unsigned long long int",
        "float", "double", "long double",
        "_Bool",
    })

    def run(self, ast, ctx: PassContext):
        for func_info in ctx.functions.values():
            # Safety: skip functions with features that complicate analysis
            if func_info.has_setjmp or func_info.has_alloca_call:
                ctx.record(
                    self.name, "skip_function", func_info.name,
                    "has setjmp or dynamic alloca",
                )
                continue

            for var_info in func_info.var_infos.values():
                strategy = self._decide(var_info, func_info)
                var_info.alloc_strategy = strategy

                if strategy != AllocStrategy.ALLOCA:
                    ctx.record(
                        self.name, "promote",
                        f"{func_info.name}::{var_info.name}",
                        f"→ {strategy.name}",
                    )
                    ctx.bump(f"alloc_decision.{strategy.name.lower()}")

        return None  # analysis-only

    def _decide(self, var_info, func_info) -> AllocStrategy:
        # Must use alloca if address is taken
        if var_info.escapes:
            return AllocStrategy.ALLOCA

        # Must use alloca for aggregates (struct, union, array)
        type_base = var_info.type_name.rstrip("*").strip()
        if type_base.startswith(("struct ", "union ")):
            return AllocStrategy.ALLOCA

        # Must use alloca for arrays
        if "[]" in var_info.type_name:
            return AllocStrategy.ALLOCA

        # Pointer types: keep alloca (could point to stack, complex alias)
        if var_info.type_name.endswith("*"):
            # Exception: single-def pointer that's never written through
            if var_info.single_def and not var_info.address_taken:
                return AllocStrategy.SSA
            return AllocStrategy.ALLOCA

        # Non-scalar types: keep alloca
        if type_base not in self._SCALAR_TYPES:
            return AllocStrategy.ALLOCA

        # Scalar, non-escaping, single-def → SSA
        if var_info.single_def:
            return AllocStrategy.SSA

        # Scalar, non-escaping, multi-def → hint for register promotion
        # (LLVM's mem2reg will handle this, but the hint helps)
        return AllocStrategy.REGISTER_HINT
