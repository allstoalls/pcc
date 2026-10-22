"""Statement control-flow lowering for the C frontend."""

from __future__ import annotations

from pcc.ir.compat import ir_c as ir


class CControlFlowMixin:
    def codegen_If(self, node):

        cond_val, _ = self.codegen(node.cond)
        cmp = self._to_bool(cond_val)

        then_bb = self.builder.function.append_basic_block("then")
        else_bb = self.builder.function.append_basic_block("else")
        merge_bb = self.builder.function.append_basic_block("ifend")

        self.builder.cbranch(cmp, then_bb, else_bb)

        with self.new_scope():
            self.builder.position_at_end(then_bb)
            self.codegen(node.iftrue)
            if not self.builder.block.is_terminated:
                self.builder.branch(merge_bb)

        with self.new_scope():
            self.builder.position_at_end(else_bb)
            if node.iffalse:
                self.codegen(node.iffalse)
            if not self.builder.block.is_terminated:
                self.builder.branch(merge_bb)
        self.builder.position_at_end(merge_bb)
        # self.builder.block = merge_bb

        return None, None

    def codegen_For(self, node):

        saved_block = self.builder.block
        self.builder.position_at_end(saved_block)  # why the save_block at the end

        if node.init is not None:
            self.codegen(node.init)

        # The builder is what? loop is a block which begin with loop
        test_bb = self.builder.function.append_basic_block("__pcc_for_test")
        loop_bb = self.builder.function.append_basic_block("__pcc_for_loop")
        next_bb = self.builder.function.append_basic_block("__pcc_for_next")

        # append by name nor just add it
        after_loop_label = self.new_label("__pcc_for_afterloop")
        after_bb = self.builder.function.append_basic_block(after_loop_label)

        self.builder.branch(test_bb)
        self.builder.position_at_end(test_bb)

        if node.cond is not None:
            endcond, _ = self.codegen(node.cond)
            cmp = self._to_bool(endcond, "loopcond")
            self.builder.cbranch(cmp, loop_bb, after_bb)
        else:
            # for(;;) - infinite loop, always branch to body
            self.builder.branch(loop_bb)

        with self.new_scope():
            self.define("break", after_bb)
            self.define("continue", next_bb)
            self.builder.position_at_end(loop_bb)
            body_val, _ = self.codegen(node.stmt)  # if was ready codegen
            if not self.builder.block.is_terminated:
                self.builder.branch(next_bb)
            self.builder.position_at_end(next_bb)
            if node.next is not None:
                self.codegen(node.next)
            self.builder.branch(test_bb)
        self.builder.position_at_end(after_bb)

        return ir.values.Constant(ir.DoubleType(), 0.0), None

    def codegen_While(self, node):

        saved_block = self.builder.block
        id_name = node.__class__.__name__
        self.builder.position_at_end(saved_block)
        # The builder is what? loop is a block which begin with loop
        test_bb = self.builder.function.append_basic_block(
            "test"
        )  # just create some block need to be filled
        loop_bb = self.builder.function.append_basic_block("loop")
        after_bb = self.builder.function.append_basic_block("afterloop")

        self.builder.branch(test_bb)
        self.builder.position_at_start(test_bb)
        endcond, _ = self.codegen(node.cond)
        cmp = self._to_bool(endcond, "loopcond")
        self.builder.cbranch(cmp, loop_bb, after_bb)

        with self.new_scope():
            self.define("break", after_bb)
            self.define("continue", test_bb)
            self.builder.position_at_end(loop_bb)
            body_val, _ = self.codegen(node.stmt)
            # after eval body we need to goto test_bb
            # New code will be inserted into after_bb
            if not self.builder.block.is_terminated:
                self.builder.branch(test_bb)
            self.builder.position_at_end(after_bb)

        # The 'for' expression always returns 0
        return ir.values.Constant(ir.DoubleType(), 0.0), None

    def codegen_Break(self, node):
        target = self.lookup("break")
        if isinstance(target, tuple):
            target = target[1]
        self.builder.branch(target)
        return None, None

    def codegen_Continue(self, node):
        target = self.lookup("continue")
        if isinstance(target, tuple):
            target = target[1]
        self.builder.branch(target)
        return None, None

    def codegen_DoWhile(self, node):

        saved_block = self.builder.block
        self.builder.position_at_end(saved_block)

        loop_bb = self.builder.function.append_basic_block("dowhile_body")
        test_bb = self.builder.function.append_basic_block("dowhile_test")
        after_bb = self.builder.function.append_basic_block("dowhile_end")

        self.builder.branch(loop_bb)

        with self.new_scope():
            self.define("break", after_bb)
            self.define("continue", test_bb)
            self.builder.position_at_end(loop_bb)
            self.codegen(node.stmt)
            if not self.builder.block.is_terminated:
                self.builder.branch(test_bb)

        self.builder.position_at_end(test_bb)
        endcond, _ = self.codegen(node.cond)
        cmp = self._to_bool(endcond, "loopcond")
        self.builder.cbranch(cmp, loop_bb, after_bb)

        self.builder.position_at_end(after_bb)
        return ir.values.Constant(ir.DoubleType(), 0.0), None

