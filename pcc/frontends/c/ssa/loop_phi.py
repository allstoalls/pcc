"""Phase 5 SSA-backed loop-header phi analysis.

Classifies phi nodes at loop headers into:
  - dead: result not used anywhere,
  - invariant: all incoming values are the same,
  - induction: one incoming is constant, the other is `phi + constant_step`,
  - reduction: one incoming is a seed, the other is `phi op X` for some op,
  - other: doesn't fit the above shapes.

This is a read-only analysis; consumers use the classification to guide
rewrites (dead-phi elimination, IV canonicalization, reduction hoisting).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from pcc.frontends.c.ssa.ir import SSABinaryOp, SSABlock, SSABranch, SSACall, SSACast, SSAConstant, SSAFunction, SSAJump, SSALoad, SSAPhi, SSAReturn, SSAStore, SSASwitch, SSAUnaryOp, SSAValue


class LoopPhiKind(str, Enum):
    DEAD = "dead"
    INVARIANT = "invariant"
    INDUCTION = "induction"
    REDUCTION = "reduction"
    OTHER = "other"


@dataclass(slots=True)
class LoopPhiClassification:
    phi_name: str
    variable_name: str
    kind: LoopPhiKind
    # For INDUCTION: step value (constant int).
    step: int | None = None
    # For REDUCTION: operator name (+, -, *, |, &, ^, max, min).
    op: str | None = None
    # Seed incoming block name (the non-back-edge predecessor).
    seed_block: str | None = None
    # Seed incoming value (the value coming from outside the loop).
    seed_value: SSAValue | None = None


@dataclass(slots=True)
class SSALoopPhiResult:
    function_name: str
    header_blocks: set[str] = field(default_factory=set)
    classifications: list[LoopPhiClassification] = field(default_factory=list)

    def counts(self) -> dict[LoopPhiKind, int]:
        result = {kind: 0 for kind in LoopPhiKind}
        for c in self.classifications:
            result[c.kind] += 1
        return result


class SSALoopPhiAnalyzer:
    """Detect loop headers and classify their phi nodes."""

    _REDUCTION_OPS = frozenset({"+", "-", "*", "|", "&", "^"})

    def analyze(self, func: SSAFunction) -> SSALoopPhiResult:
        result = SSALoopPhiResult(function_name=func.name)
        block_by_name = {b.name: b for b in func.blocks}
        # Find loop headers: blocks with a back-edge (a predecessor
        # that is dominated by the block itself). Simpler heuristic:
        # a block is a loop header if its name starts with a known
        # loop-header prefix from the builder. This matches the
        # builder's naming convention and avoids a full dominator pass.
        header_prefixes = ("while.header", "dowhile.body", "for.header")
        for block in func.blocks:
            if any(block.name.startswith(p) for p in header_prefixes):
                result.header_blocks.add(block.name)

        # Compute use counts for dead-phi detection.
        use_counts = self._count_uses(func)

        for block in func.blocks:
            if block.name not in result.header_blocks:
                continue
            for instr in block.instructions:
                if not isinstance(instr, SSAPhi):
                    continue
                classification = self._classify_phi(
                    instr, block, block_by_name, use_counts,
                )
                result.classifications.append(classification)

        return result

    def _count_uses(self, func: SSAFunction) -> dict[str, int]:
        counts: dict[str, int] = {}

        def bump(value: SSAValue | None) -> None:
            if value is None:
                return
            name = getattr(value, "name", None)
            if not name:
                return
            counts[name] = counts.get(name, 0) + 1

        for block in func.blocks:
            for instr in block.instructions:
                if isinstance(instr, SSAPhi):
                    for _, val in instr.incomings:
                        bump(val)
                elif isinstance(instr, SSABinaryOp):
                    bump(instr.left)
                    bump(instr.right)
                elif isinstance(instr, SSAUnaryOp):
                    bump(instr.operand)
                elif isinstance(instr, SSACast):
                    bump(instr.operand)
                elif isinstance(instr, SSALoad):
                    bump(instr.base)
                    bump(getattr(instr, "index", None))
                elif isinstance(instr, SSAStore):
                    bump(instr.addr)
                    bump(instr.value)
                elif isinstance(instr, SSACall):
                    bump(instr.callee)
                    for a in instr.args:
                        bump(a)
            term = block.terminator
            if isinstance(term, SSABranch):
                bump(term.condition)
            elif isinstance(term, SSAReturn):
                bump(term.value)
            elif isinstance(term, SSASwitch):
                bump(term.value)
        return counts

    def _classify_phi(
        self,
        phi: SSAPhi,
        header: SSABlock,
        block_by_name: dict[str, SSABlock],
        use_counts: dict[str, int],
    ) -> LoopPhiClassification:
        # Dead: zero uses after analysis.
        if use_counts.get(phi.name, 0) == 0:
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.DEAD,
            )

        # Invariant: all incoming values equal.
        incomings = list(phi.incomings)
        if len(incomings) < 2:
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.OTHER,
            )
        first_value = incomings[0][1]
        if all(val == first_value for _, val in incomings[1:]):
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.INVARIANT,
                seed_value=first_value,
            )

        # Identify seed (pre-loop) vs back-edge (loop body).
        # The seed predecessor is the one that is NOT dominated by the header;
        # the builder typically lists the seed first when constructing phis.
        # Heuristic: the back-edge predecessor block name starts with a loop
        # body/latch prefix associated with the same loop.
        body_prefixes = (
            "while.body", "while.end",
            "dowhile.body", "dowhile.latch", "dowhile.end",
            "for.body", "for.continue", "for.end",
        )
        seed_idx = None
        back_idx = None
        for i, (pred_name, _) in enumerate(incomings):
            if any(pred_name.startswith(p) for p in body_prefixes):
                back_idx = i
            else:
                seed_idx = i
        if seed_idx is None or back_idx is None:
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.OTHER,
            )

        seed_block, seed_value = incomings[seed_idx]
        back_block, back_value = incomings[back_idx]

        # Induction pattern: back_value = phi + constant OR phi - constant.
        induction_step = self._match_induction(phi, back_value)
        if induction_step is not None:
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.INDUCTION,
                step=induction_step,
                seed_block=seed_block,
                seed_value=seed_value,
            )

        # Reduction pattern: back_value = phi op X (X any value, op in REDUCTION_OPS).
        reduction_op = self._match_reduction(phi, back_value)
        if reduction_op is not None:
            return LoopPhiClassification(
                phi_name=phi.name,
                variable_name=phi.variable_name,
                kind=LoopPhiKind.REDUCTION,
                op=reduction_op,
                seed_block=seed_block,
                seed_value=seed_value,
            )

        return LoopPhiClassification(
            phi_name=phi.name,
            variable_name=phi.variable_name,
            kind=LoopPhiKind.OTHER,
            seed_block=seed_block,
            seed_value=seed_value,
        )

    def _match_induction(
        self, phi: SSAPhi, back_value: SSAValue,
    ) -> int | None:
        if not isinstance(back_value, SSABinaryOp):
            return None
        if back_value.op not in ("+", "-"):
            return None
        left = back_value.left
        right = back_value.right
        # Pattern: phi + K or K + phi or phi - K.
        if back_value.op == "+":
            if left is phi and isinstance(right, SSAConstant):
                return right.value
            if right is phi and isinstance(left, SSAConstant):
                return left.value
        elif back_value.op == "-":
            if left is phi and isinstance(right, SSAConstant):
                return -right.value
        return None

    def _match_reduction(
        self, phi: SSAPhi, back_value: SSAValue,
    ) -> str | None:
        if not isinstance(back_value, SSABinaryOp):
            return None
        if back_value.op not in self._REDUCTION_OPS:
            return None
        # Pattern: phi op X or X op phi (for commutative ops).
        left = back_value.left
        right = back_value.right
        if back_value.op in {"-"}:
            # Only `phi - X` is a reduction (subtraction isn't commutative).
            if left is phi:
                return back_value.op
            return None
        # Commutative reduction ops.
        if left is phi or right is phi:
            return back_value.op
        return None
