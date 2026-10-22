"""Expression-level control-flow lowering for the C frontend."""

from __future__ import annotations

from pcc.ir.compat import ir_c as ir

from pcc.frontends.c.codegen.c_types import int64_t


class CExpressionFlowMixin:
    def _codegen_short_circuit_and(self, node):
        """Short-circuit &&: if lhs is false, skip rhs."""
        lhs, _ = self.codegen(node.left)
        lhs_bool = self._to_bool(lhs, "and_lhs")

        rhs_bb = self.builder.function.append_basic_block("and_rhs")
        merge_bb = self.builder.function.append_basic_block("and_merge")
        lhs_bb = self.builder.block

        self.builder.cbranch(lhs_bool, rhs_bb, merge_bb)

        self.builder.position_at_end(rhs_bb)
        rhs, _ = self.codegen(node.right)
        rhs_bool = self._to_bool(rhs, "and_rhs_bool")
        rhs_result = self.builder.zext(rhs_bool, int64_t, "and_rhs_ext")
        rhs_bb_end = self.builder.block
        self.builder.branch(merge_bb)

        self.builder.position_at_end(merge_bb)
        phi = self.builder.phi(int64_t, "and_result")
        phi.add_incoming(ir.Constant(int64_t, 0), lhs_bb)
        phi.add_incoming(rhs_result, rhs_bb_end)
        return phi, None

    def _codegen_short_circuit_or(self, node):
        """Short-circuit ||: if lhs is true, skip rhs."""
        lhs, _ = self.codegen(node.left)
        lhs_bool = self._to_bool(lhs, "or_lhs")

        rhs_bb = self.builder.function.append_basic_block("or_rhs")
        merge_bb = self.builder.function.append_basic_block("or_merge")
        lhs_bb = self.builder.block

        self.builder.cbranch(lhs_bool, merge_bb, rhs_bb)

        self.builder.position_at_end(rhs_bb)
        rhs, _ = self.codegen(node.right)
        rhs_bool = self._to_bool(rhs, "or_rhs_bool")
        rhs_result = self.builder.zext(rhs_bool, int64_t, "or_rhs_ext")
        rhs_bb_end = self.builder.block
        self.builder.branch(merge_bb)

        self.builder.position_at_end(merge_bb)
        phi = self.builder.phi(int64_t, "or_result")
        phi.add_incoming(ir.Constant(int64_t, 1), lhs_bb)
        phi.add_incoming(rhs_result, rhs_bb_end)
        return phi, None


