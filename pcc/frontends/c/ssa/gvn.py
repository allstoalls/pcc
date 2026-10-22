"""Minimal dominator-aware GVN over the bootstrap SSA form."""

from __future__ import annotations

from dataclasses import dataclass, field

from pcc.frontends.c.ssa.ir import SSABinaryOp, SSACast, SSAConstant, SSAFieldAddr, SSAFunction, SSAInstruction, SSALoad, SSAStore, SSAUnaryOp, SSAUndef, SSAValue


@dataclass(slots=True)
class SSAGVNResult:
    redundant_values: dict[str, str] = field(default_factory=dict)
    value_leaders: dict[str, str] = field(default_factory=dict)
    expressions_seen: int = 0

    def redundant_value_names(self) -> dict[str, str]:
        return dict(self.redundant_values)


class SSAGVNAnalyzer:
    """Discover redundant pure SSA expressions in dominator order."""

    _COMMUTATIVE_BINOPS = frozenset({"*", "+", "==", "!=", "&", "|", "^", "&&", "||"})

    def analyze(self, function: SSAFunction) -> SSAGVNResult:
        order = {block.name: index for index, block in enumerate(function.blocks)}
        dom_children: dict[str, list[str]] = {block.name: [] for block in function.blocks}
        for block_name, idom in function.immediate_dominators.items():
            if idom is not None:
                dom_children.setdefault(idom, []).append(block_name)
        for children in dom_children.values():
            children.sort(key=order.get)

        result = SSAGVNResult()
        self._walk_block(
            function,
            function.entry_block,
            dom_children,
            result,
            expr_leaders={},
            value_leaders={},
        )
        return result

    def _walk_block(
        self,
        function: SSAFunction,
        block_name: str,
        dom_children: dict[str, list[str]],
        result: SSAGVNResult,
        *,
        expr_leaders: dict[tuple, SSAInstruction],
        value_leaders: dict[str, SSAValue],
    ) -> None:
        block = function.block(block_name)
        local_expr_leaders = dict(expr_leaders)
        local_value_leaders = dict(value_leaders)

        for instruction in block.instructions:
            local_value_leaders.setdefault(instruction.name, instruction)
            expr_key = self._expr_key(instruction, local_value_leaders)
            if expr_key is None:
                result.value_leaders[instruction.name] = instruction.name
                continue

            result.expressions_seen += 1
            leader = local_expr_leaders.get(expr_key)
            if leader is not None:
                result.redundant_values[instruction.name] = leader.name
                local_value_leaders[instruction.name] = local_value_leaders.get(
                    leader.name,
                    leader,
                )
                result.value_leaders[instruction.name] = leader.name
                continue

            local_expr_leaders[expr_key] = instruction
            result.value_leaders[instruction.name] = instruction.name

        for child_name in dom_children.get(block_name, ()):
            self._walk_block(
                function,
                child_name,
                dom_children,
                result,
                expr_leaders=local_expr_leaders,
                value_leaders=local_value_leaders,
            )

    def _expr_key(
        self,
        instruction: SSAInstruction,
        value_leaders: dict[str, SSAValue],
    ) -> tuple | None:
        if isinstance(instruction, SSAUnaryOp):
            return (
                "unary",
                instruction.type_name,
                instruction.op,
                self._value_key(instruction.operand, value_leaders),
            )
        if isinstance(instruction, SSACast):
            return (
                "cast",
                instruction.type_name,
                self._value_key(instruction.operand, value_leaders),
            )
        if isinstance(instruction, SSALoad):
            return None
        if isinstance(instruction, SSAFieldAddr):
            return None
        if isinstance(instruction, SSAStore):
            return None
        if isinstance(instruction, SSABinaryOp):
            left_key = self._value_key(instruction.left, value_leaders)
            right_key = self._value_key(instruction.right, value_leaders)
            if instruction.op in self._COMMUTATIVE_BINOPS and right_key < left_key:
                left_key, right_key = right_key, left_key
            return (
                "binary",
                instruction.type_name,
                instruction.op,
                left_key,
                right_key,
            )
        return None

    def _value_key(
        self,
        value: SSAValue | None,
        value_leaders: dict[str, SSAValue],
    ) -> tuple:
        if value is None:
            return ("none",)
        leader = value_leaders.get(value.name, value)
        if isinstance(leader, SSAConstant):
            return ("const", leader.type_name, leader.value)
        if isinstance(leader, SSAUndef):
            return ("undef", leader.type_name, leader.source_name)
        return ("value", leader.type_name, leader.name)
