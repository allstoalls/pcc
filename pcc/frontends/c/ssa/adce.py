"""Minimal SSA-based ADCE over the bootstrap SSA form."""

from __future__ import annotations

from dataclasses import dataclass, field

from pcc.frontends.c.ssa.ir import SSABinaryOp, SSABinding, SSABranch, SSACast, SSACall, SSAFieldAddr, SSAFunction, SSAStore, SSAPhi, SSAReturn, SSALoad, SSAUnaryOp, SSAValue


@dataclass(slots=True)
class SSAADCEResult:
    live_value_names: set[str] = field(default_factory=set)
    dead_bindings: dict[str, str] = field(default_factory=dict)


class SSAADCEAnalyzer:
    """Discover dead SSA-backed bindings that do not reach observable uses."""

    def analyze(self, function: SSAFunction) -> SSAADCEResult:
        reachable = function.reachable_blocks()
        # `value_live_ids` = instructions whose *return value* is
        # consumed. `effect_live_ids` = instructions that must stay in
        # the IR because of side effects (calls, stores). A binding
        # `x = call()` whose result is immediately shadowed is
        # value-dead even when the call is effect-live — DSE converts
        # it to a standalone expression statement (see ssa-dse pass).
        value_live_ids: set[int] = set()
        effect_live_ids: set[int] = set()
        live_value_names: set[str] = set()
        worklist: list[SSAValue] = []

        for block in function.blocks:
            if block.name not in reachable:
                continue
            for instruction in block.instructions:
                if isinstance(instruction, SSACall):
                    effect_live_ids.add(id(instruction))
                    # Walk operands through the value-liveness set so
                    # arg computations stay alive.
                    if instruction.callee is not None:
                        worklist.append(instruction.callee)
                    worklist.extend(instruction.args)
                elif isinstance(instruction, SSAStore):
                    effect_live_ids.add(id(instruction))
                    if instruction.addr is not None:
                        worklist.append(instruction.addr)
                    if instruction.value is not None:
                        worklist.append(instruction.value)
            terminator = block.terminator
            if isinstance(terminator, SSABranch):
                worklist.append(terminator.condition)
            elif isinstance(terminator, SSAReturn) and terminator.value is not None:
                worklist.append(terminator.value)

        while worklist:
            value = worklist.pop()
            value_id = id(value)
            if value_id in value_live_ids:
                continue
            value_live_ids.add(value_id)
            live_value_names.add(value.name)

            if isinstance(value, SSAUnaryOp) and value.operand is not None:
                worklist.append(value.operand)
                continue
            if isinstance(value, SSACast) and value.operand is not None:
                worklist.append(value.operand)
                continue
            if isinstance(value, SSALoad):
                if value.base is not None:
                    worklist.append(value.base)
                if value.index is not None:
                    worklist.append(value.index)
                continue
            if isinstance(value, SSAFieldAddr):
                if value.base is not None:
                    worklist.append(value.base)
                continue
            if isinstance(value, SSACall):
                if value.callee is not None:
                    worklist.append(value.callee)
                worklist.extend(value.args)
                continue
            if isinstance(value, SSABinaryOp):
                if value.left is not None:
                    worklist.append(value.left)
                if value.right is not None:
                    worklist.append(value.right)
                continue
            if isinstance(value, SSAPhi):
                for _, incoming in value.incomings:
                    worklist.append(incoming)

        dead_bindings: dict[str, str] = {}
        for binding in function.bindings:
            if binding.block_name not in reachable or not binding.source_coord:
                continue
            # Value-dead means the binding's stored RESULT isn't
            # consumed anywhere — even if the underlying instruction
            # has side effects (call) that must be preserved.
            if id(binding.value) in value_live_ids:
                continue
            dead_bindings.setdefault(binding.source_coord, binding.kind)

        return SSAADCEResult(
            live_value_names=live_value_names,
            dead_bindings=dead_bindings,
        )
